"""CSRGraph with KGX archive loading support.

Extends the CSRGraph from csrgraph_test1.py with methods to:
  - Load graphs from compressed KGX archives (.tar.zst / .tar.gz / .tar)
    containing nodes.jsonl and edges.jsonl (streaming, low-memory).
  - Strip the constant ``biolink:`` prefix from predicates and categories
    internally to save memory; add it back transparently when reporting.
  - Report memory usage of CSR matrices and auxiliary graph structures.
  - Serialize / deserialize the graph to a local file (pickle + zstd).

Requires Python 3.14+ (uses ``compression.zstd``) **or** the third-party
``zstandard`` package for older Python versions.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import struct
import sys
import tarfile
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Tuple, overload

import numpy as np
from metadata_db import (
    MetadataBackend,  # type: ignore[import]  # no circular import: metadata_db does not import csrgraph_kgx
)
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.sparse.csgraph import shortest_path as csgraph_shortest_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Available-memory probe (used by set_memory_limit("auto"))
# ---------------------------------------------------------------------------

def _available_memory() -> int:
    """Return an estimate of available physical memory in bytes.

    Probe order: psutil → /proc/meminfo (Linux) → sysctl hw.memsize (macOS)
    → 4 GiB hard fallback.
    """
    try:
        import psutil  # optional
        return int(psutil.virtual_memory().available)
    except ImportError:
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        return int(out.strip())
    except Exception:
        pass
    return 4 * 1024 ** 3

# ---------------------------------------------------------------------------
# Zstandard: prefer the built-in module shipped with Python >= 3.14,
# fall back to the third-party ``zstandard`` package.
# ---------------------------------------------------------------------------
_BUILTIN_ZSTD = False
try:
    import compression.zstd as _zstd  # Python >= 3.14

    _BUILTIN_ZSTD = True
except ImportError:
    try:
        import zstandard as _zstd  # type: ignore[no-redef]
    except ImportError:
        _zstd = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BIOLINK_PREFIX = "biolink:"

Triple = Tuple[str, str, str]
PathEdge = Tuple[str, Optional[str], str]

# Path-pattern spec types used by CSRGraph.match_path()
#   NodeSpec: str = exact CURIE | dict = metadata filter | None = wildcard
#   EdgeSpec: str = exact predicate | dict = metadata filter | None = wildcard
NodeSpec = str | dict | None
#: EdgeSpec: str = one exact predicate | collection of str = any of those
#: predicates | dict = edge metadata filter | None = wildcard
EdgeSpec = str | dict | list | tuple | set | frozenset | None

#: Default hop bound for :meth:`CSRGraph.all_paths`.  Simple-path enumeration is
#: exponential in depth, so an unbounded default is a footgun on a real KG: five
#: hops already covers the Translator query shapes this library targets (2–3 hops
#: typically, rarely beyond four).  Pass ``max_depth=None`` to opt out explicitly.
DEFAULT_ALL_PATHS_MAX_DEPTH = 5


@dataclass
class MatchStats:
    """Completeness report for one :meth:`CSRGraph.match_path` call.

    ``match_path`` bounds each hop, so a result set may be a *subset* of the
    matching paths.  For "find all matching paths" use cases that distinction
    matters: a truncated result is not a negative result.  Request this via
    ``return_stats=True`` to find out, or watch for the logged warning.

    Attributes
    ----------
    truncated:
        True when at least one hop hit its cap, meaning matching paths were
        discarded and the returned set is incomplete.
    truncated_hops:
        Zero-based indices of the hops that hit their cap.
    frontier_sizes:
        Surviving frontier size after each hop, useful for spotting which hop
        exploded.
    hop_caps:
        The cap applied at each hop (``limit`` on the final hop, the larger
        intermediate bound before it).
    """

    truncated: bool = False
    truncated_hops: List[int] = field(default_factory=list)
    frontier_sizes: List[int] = field(default_factory=list)
    hop_caps: List[int] = field(default_factory=list)


_EMPTY_I64 = np.empty(0, dtype=np.int64)
_EMPTY_I32 = np.empty(0, dtype=np.int32)


def _csr_ragged_gather(
    indptr: np.ndarray,
    indices: np.ndarray,
    rows: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """Gather every CSR entry of *rows* in one vectorized pass.

    Returns ``(src_pos, cols)``: for each gathered entry, the position in *rows*
    it came from and the column it points at.  Entries come back grouped by row
    in the order *rows* lists them, and within a row in CSR order — the same
    order a per-row Python loop would produce.

    This replaces the "loop over rows, slice each one" pattern, which costs a
    Python iteration per row; here the whole frontier is one set of numpy ops.
    """
    starts = indptr[rows].astype(np.int64, copy=False)
    counts = indptr[rows + 1].astype(np.int64, copy=False) - starts
    total = int(counts.sum())
    if total == 0:
        return _EMPTY_I64, _EMPTY_I64
    src_pos = np.repeat(np.arange(rows.size, dtype=np.int64), counts)
    first_out = np.cumsum(counts) - counts          # where each row starts in the output
    within = np.arange(total, dtype=np.int64) - np.repeat(first_out, counts)
    cols = indices[np.repeat(starts, counts) + within]
    return src_pos, cols


def _strip_biolink(value: str) -> str:
    """Remove the leading ``biolink:`` prefix if present."""
    if value.startswith(BIOLINK_PREFIX):
        return value[len(BIOLINK_PREFIX) :]
    return value


def _add_biolink(value: str) -> str:
    """Restore the ``biolink:`` prefix on a value stripped by ``_strip_biolink``.

    Only bare values (no namespace) get the prefix.  Values that already carry
    a namespace — ``biolink:...`` *or* another CURIE prefix such as
    ``rdfs:subClassOf`` — are returned unchanged, so non-biolink predicates are
    not corrupted into ``biolink:rdfs:subClassOf``.
    """
    if ":" in value:
        return value
    return BIOLINK_PREFIX + value


def _normalize_relation(relation: str | None) -> str | None:
    """Normalize a user-supplied relation to internal (no-prefix) form."""
    if relation is None:
        return None
    return _strip_biolink(relation)


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


# ===================================================================
# Lazy per-predicate CSR dict (backed by np.memmap files)
# ===================================================================


class _LazyCSRDict:
    """Lazy dict-like container that memory-maps per-predicate CSR matrices on first access.

    Drop-in replacement for ``dict[str, csr_matrix]`` so all existing CSRGraph
    traversal code works unchanged.  Each predicate's three arrays (indptr /
    indices / data) are mmap'd from flat ``.bin`` files on first access.
    Subsequent accesses return the cached matrix.
    """

    def __init__(
        self,
        memmap_dir: Path,
        relations_meta: dict,
        num_nodes: int,
    ) -> None:
        self._dir = memmap_dir
        self._relations_meta: dict = relations_meta
        self._num_nodes = num_nodes
        self._cache: dict[str, csr_matrix] = {}

    @staticmethod
    def _stem(rel: str) -> str:
        """Sanitize a relation name to a filename-safe stem."""
        return rel.replace("/", "__").replace(":", "_").replace(" ", "_")

    def _mmap_one(self, rel: str) -> csr_matrix:
        m = self._relations_meta[rel]
        s = self._stem(rel)
        d = self._dir
        indptr  = np.memmap(d / f"{s}.indptr.bin",  dtype=m["indptr_dtype"],  mode="r")
        indices = np.memmap(d / f"{s}.indices.bin", dtype=m["indices_dtype"], mode="r")
        data    = np.memmap(d / f"{s}.data.bin",    dtype=m["data_dtype"],    mode="r")
        n = self._num_nodes
        return csr_matrix((data, indices, indptr), shape=(n, n))

    # ---- dict-like interface -----------------------------------------------

    def __contains__(self, key: object) -> bool:
        return key in self._relations_meta

    def __getitem__(self, key: str) -> csr_matrix:
        if key not in self._relations_meta:
            raise KeyError(key)
        if key not in self._cache:
            self._cache[key] = self._mmap_one(key)
        return self._cache[key]

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        if key not in self._relations_meta:
            return default
        return self[key]

    def keys(self):  # type: ignore[override]
        return self._relations_meta.keys()

    def values(self):  # type: ignore[override]
        return [self[k] for k in self._relations_meta]

    def items(self):  # type: ignore[override]
        return [(k, self[k]) for k in self._relations_meta]

    def __iter__(self):
        return iter(self._relations_meta)

    def __len__(self) -> int:
        return len(self._relations_meta)


# ===================================================================
# CSRGraph
# ===================================================================


class CSRGraph:
    """A sparse graph backed by CSR matrices with KGX loading support.

    Predicates and node categories are stored **without** the ``biolink:``
    prefix internally.  All public methods accept parameters with or without
    the prefix and always *report* values with the prefix.
    """

    # Global memory limit (bytes).  None = always load into RAM.
    # When set and a graph's RAM footprint exceeds this value, ``load()``
    # uses (or builds) a memmap directory for zero-copy, lazy loading.
    _memory_limit: int | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, triples: List[Triple]):
        """Build the graph from *(subject, predicate, object)* triples.

        Predicates are stored with the ``biolink:`` prefix **stripped**.
        """
        # Strip biolink: from predicates at ingestion time. We do not retain
        # the raw triples after construction because path-finding queries only
        # need the derived sparse matrices, label map, node maps, and metadata.
        normalized_triples: List[Triple] = [
            (s, _strip_biolink(p), o) for s, p, o in triples
        ]
        self.edge_count: int = len(normalized_triples)
        self.predicate_counts: Dict[str, int] = dict(
            Counter(p for _, p, _ in normalized_triples)
        )

        # Encode nodes and relations as integer IDs
        self.nodes: List[str] = sorted(
            {s for s, _, _ in normalized_triples}
            | {o for _, _, o in normalized_triples}
        )
        self.relations: List[str] = sorted(self.predicate_counts)

        self.node_to_id: Dict[str, int] = {n: i for i, n in enumerate(self.nodes)}
        self.rel_to_id: Dict[str, int] = {r: i for i, r in enumerate(self.relations)}

        self.num_nodes: int = len(self.nodes)

        # Optional node metadata (populated by from_kgx_archive).
        # Categories inside metadata are stored *without* biolink: prefix.
        self.node_metadata: Dict[str, dict] = {}

        # Optional edge metadata (populated by from_kgx_archive).
        # Keyed by (subject_curie, predicate_stripped, object_curie).
        # Empty by default; populated when edge_metadata_fields is specified.
        self.edge_metadata: Dict[Tuple[str, str, str], dict] = {}

        # Optional metadata backend (set via load(db=...) or set_db()).
        self.db: MetadataBackend | None = None

        self.edge_predicate_ids: np.ndarray

        # ---- per-relation CSR matrices ------------------------------------
        rel_edges: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for s, p, o in normalized_triples:
            rel_edges[p].append((self.node_to_id[s], self.node_to_id[o]))

        self.csr_by_relation: Dict[str, csr_matrix] = {}
        for rel, edges in rel_edges.items():
            rows = [u for u, _ in edges]
            cols = [v for _, v in edges]
            data = np.ones(len(edges), dtype=np.uint8)
            self.csr_by_relation[rel] = csr_matrix(
                (data, (rows, cols)),
                shape=(self.num_nodes, self.num_nodes),
                dtype=np.uint8,
            )

        # ---- representative edge predicate map ---------------------------
        edge_predicate: Dict[int, int] = {}
        for s, p, o in normalized_triples:
            u = self.node_to_id[s]
            v = self.node_to_id[o]
            edge_key = self._edge_key(u, v)
            if edge_key not in edge_predicate:
                edge_predicate[edge_key] = self.rel_to_id[p]

        # ---- merged adjacency --------------------------------------------
        self.csr_merged = self._build_merged_csr(self.csr_by_relation)
        self.edge_predicate_ids = self._build_edge_predicate_ids(edge_predicate)

        # ---- weakly connected components ----------------------------------
        self._n_components: int
        self._component_labels: np.ndarray
        self._n_components, self._component_labels = connected_components(
            self.csr_merged,
            directed=True,
            connection="weak",
            return_labels=True,
        )
        self._relation_component_cache: Dict[str, np.ndarray] = {}

    def __getstate__(self) -> dict:
        """Serialize only data needed for queries and metadata lookups."""
        # Materialize _LazyCSRDict → plain dict so pickle always gets regular arrays
        cbr = self.csr_by_relation
        if isinstance(cbr, _LazyCSRDict):
            cbr = {rel: cbr[rel] for rel in cbr}
        return {
            "nodes": self.nodes,
            "relations": self.relations,
            "csr_by_relation": cbr,
            "edge_predicate_ids": np.asarray(self.edge_predicate_ids),
            "_component_labels": np.asarray(self._component_labels),
            "node_metadata": self.node_metadata,
            "edge_metadata": self.edge_metadata,
            "edge_count": self.edge_count,
            "predicate_counts": self.predicate_counts,
        }

    def __setstate__(self, state: dict) -> None:
        """Restore from a compact cache state or an older full-object pickle."""
        self.nodes = state["nodes"]
        self.relations = state.get("relations", sorted(state["csr_by_relation"]))
        self.node_to_id = {n: i for i, n in enumerate(self.nodes)}
        self.rel_to_id = {r: i for i, r in enumerate(self.relations)}
        self.num_nodes = len(self.nodes)

        self.node_metadata = state.get("node_metadata", {})
        self.edge_metadata = state.get("edge_metadata", {})
        self.csr_by_relation = state["csr_by_relation"]
        self.csr_merged = self._build_merged_csr(self.csr_by_relation)

        if "edge_predicate_ids" in state:
            self.edge_predicate_ids = state["edge_predicate_ids"]
        elif "edge_predicate" in state:
            self.edge_predicate_ids = self._build_edge_predicate_ids(
                state["edge_predicate"]
            )
        elif "edge_labels" in state:
            edge_predicate: Dict[int, int] = {}
            for (u, v), labels in state["edge_labels"].items():
                if labels:
                    edge_predicate[self._edge_key(u, v)] = self.rel_to_id[labels[0]]
            self.edge_predicate_ids = self._build_edge_predicate_ids(edge_predicate)
        else:
            self.edge_predicate_ids = np.zeros(
                self.csr_merged.nnz, dtype=self._relation_id_dtype()
            )

        if "component_labels" in state:
            self._component_labels = state["component_labels"]
        elif "_component_labels" in state:
            self._component_labels = state["_component_labels"]
        else:
            _, self._component_labels = connected_components(
                self.csr_merged,
                directed=True,
                connection="weak",
                return_labels=True,
            )
        self._n_components = (
            int(np.max(self._component_labels)) + 1 if self.num_nodes else 0
        )

        if "edge_count" in state:
            self.edge_count = state["edge_count"]
        elif "triples" in state:
            self.edge_count = len(state["triples"])
        else:
            self.edge_count = sum(self.predicate_counts.values())

        if "predicate_counts" in state:
            self.predicate_counts = state["predicate_counts"]
        elif "triples" in state:
            self.predicate_counts = dict(Counter(p for _, p, _ in state["triples"]))
        else:
            self.predicate_counts = {}

        self._relation_component_cache: Dict[str, np.ndarray] = {}

    def _edge_key(self, u: int, v: int) -> int:
        return u * self.num_nodes + v

    def _expansion_plan(self) -> List[Tuple[np.ndarray, np.ndarray, str]]:
        """Per-relation ``(indptr, indices, biolink_label)`` tuples, built once.

        Wildcard neighbour expansion has to visit every relation to keep each
        edge's true predicate (``csr_merged`` cannot serve this — see
        ``edge_predicate_ids``, which stores only one representative predicate
        per node pair).  Precomputing the plan hoists the dict iteration, the
        ``indptr``/``indices`` attribute lookups, and the ``biolink:`` label
        construction out of the per-node hot loop.
        """
        plan = getattr(self, "_expand_plan_cache", None)
        if plan is None:
            plan = [
                (csr.indptr, csr.indices, _add_biolink(rel))
                for rel, csr in self.csr_by_relation.items()
            ]
            self._expand_plan_cache = plan
        return plan

    def _reverse_expansion_plan(self) -> List[Tuple[np.ndarray, np.ndarray, str]]:
        """Like :meth:`_expansion_plan` but over transposed per-relation matrices.

        Answers "which nodes point *at* this one, and by which predicate" — needed
        whenever a query pins the object end of an edge and leaves the subject
        open ("what treats X?").  ``csr_merged``'s transpose cannot serve this,
        because ``edge_predicate_ids`` keeps only one representative predicate per
        node pair.

        Built lazily and cached: the transposes cost roughly what
        ``csr_by_relation`` does (~550 MB on translator_kg), so graphs that never
        run a reverse hop never pay for it.
        """
        plan = getattr(self, "_reverse_plan_cache", None)
        if plan is None:
            plan = [
                (csr_t.indptr, csr_t.indices, _add_biolink(rel))
                for rel, csr_t in (
                    (rel, csr.T.tocsr()) for rel, csr in self.csr_by_relation.items()
                )
            ]
            self._reverse_plan_cache = plan
        return plan

    def _relation_id_dtype(self) -> np.dtype:
        n = max(len(self.relations), 1)
        if n <= np.iinfo(np.uint8).max:
            return np.dtype(np.uint8)
        if n <= np.iinfo(np.uint16).max:
            return np.dtype(np.uint16)
        return np.dtype(np.uint32)

    def _build_edge_predicate_ids(self, edge_predicate: Dict[int, int]) -> np.ndarray:
        rel_ids = np.empty(self.csr_merged.nnz, dtype=self._relation_id_dtype())
        for u in range(self.num_nodes):
            start = self.csr_merged.indptr[u]
            end = self.csr_merged.indptr[u + 1]
            for pos in range(start, end):
                v = int(self.csr_merged.indices[pos])
                rel_ids[pos] = edge_predicate[self._edge_key(u, v)]
        return rel_ids

    def _edge_relation_id(self, u: int, v: int) -> Optional[int]:
        start = self.csr_merged.indptr[u]
        end = self.csr_merged.indptr[u + 1]
        cols = self.csr_merged.indices[start:end]
        idx = int(np.searchsorted(cols, v))
        if idx >= len(cols) or int(cols[idx]) != v:
            return None
        return int(self.edge_predicate_ids[start + idx])

    @staticmethod
    def _build_merged_csr(csr_by_relation: Dict[str, csr_matrix]) -> csr_matrix:
        iterator = iter(csr_by_relation.values())
        try:
            merged = next(iterator).copy()
        except StopIteration:
            return csr_matrix((0, 0), dtype=np.uint8)

        for mat in iterator:
            merged = merged + mat
        merged.data[:] = 1
        merged = merged.astype(np.uint8)
        merged.sum_duplicates()
        return merged

    # ------------------------------------------------------------------
    # KGX archive loading (streaming, low-memory)
    # ------------------------------------------------------------------

    @classmethod
    def from_kgx_archive(
        cls,
        archive_path: str,
        predicate_filter: Optional[List[str]] = None,
        node_metadata_fields: Optional[List[str]] = None,
        edge_metadata_fields: Optional[List[str]] = None,
    ) -> CSRGraph:
        """Load a graph from a compressed KGX archive.

        Supported formats: ``.tar.zst``, ``.tar.gz``, ``.tar``.

        The archive must contain ``edges.jsonl`` (required) and optionally
        ``nodes.jsonl``.  Files are streamed line-by-line so that the full
        JSONL text is never held in memory at once.

        Parameters
        ----------
        archive_path : str
            Path to the compressed KGX archive file.
        predicate_filter : list of str, optional
            If given, only edges whose predicate matches one of these values
            are loaded.  Values may include or omit the ``biolink:`` prefix.
        node_metadata_fields : list of str, optional
            Node metadata fields to retain from ``nodes.jsonl``.

            - ``None`` (**the default**) — skip ``nodes.jsonl`` entirely and keep
              **no** node metadata: a topology-only load. This is what a serving
              snapshot wants, since metadata lives in a
              :class:`~metadata_db.MetadataBackend`.
            - ``["all"]`` — retain every field present on each node.
            - a list of names, e.g. ``["name"]`` — retain those fields.

            ``id`` and ``category`` are always retained whenever a list is given,
            so ``[]`` still stores one record per node (on a ~1.7M-node graph that
            is ~2.2 GB resident) — pass ``None``, not ``[]``, to skip metadata.
        edge_metadata_fields : list of str, optional
            Edge metadata fields to retain from ``edges.jsonl`` beyond the
            required ``subject``, ``predicate``, and ``object``.
            Defaults to ``None`` (no edge metadata stored).
            Pass ``["all"]`` to keep all fields, or a list such as
            ``["knowledge_level", "agent_type", "sources"]`` for specific ones.
            Stored in ``graph.edge_metadata`` keyed by
            ``(subject, predicate_stripped, object)``.
        """
        archive = Path(archive_path)
        if not archive.exists():
            raise FileNotFoundError(f"Archive not found: {archive}")

        # Normalise predicate filter to stripped form for matching
        pred_filter_set: set[str] | None = None
        if predicate_filter:
            pred_filter_set = {_strip_biolink(p) for p in predicate_filter}

        load_node_metadata = node_metadata_fields is not None
        keep_all_node_metadata = node_metadata_fields == ["all"]
        metadata_fields: set[str] | None = (
            None
            if keep_all_node_metadata
            else set(node_metadata_fields)
            if load_node_metadata
            else None
        )

        # Edge metadata configuration
        _EDGE_CORE = {"subject", "predicate", "object"}
        load_edge_metadata = edge_metadata_fields is not None
        keep_all_edge_metadata = edge_metadata_fields == ["all"]
        edge_fields: set[str] = (
            set(edge_metadata_fields) - _EDGE_CORE
            if edge_metadata_fields and not keep_all_edge_metadata
            else set()
        )

        suffix = "".join(archive.suffixes)

        tar, _closeables = cls._open_tar(archive, suffix)

        # ------------------------------------------------------------------
        # Streaming parse – we iterate over tar members once.  For the
        # streaming mode (``r|``) the members come in archive order and we
        # **cannot** call ``tar.getmembers()`` or seek back.  Instead we
        # iterate with ``for member in tar`` which works with ``r|`` mode.
        # ------------------------------------------------------------------
        triples: List[Triple] = []
        node_meta: Dict[str, dict] = {}
        edge_meta: Dict[Tuple[str, str, str], dict] = {}

        try:
            for member in tar:
                basename = Path(member.name).name
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue

                if basename == "edges.jsonl":
                    for raw_line in fobj:
                        line = raw_line.strip()
                        if not line:
                            continue
                        edge = json.loads(line)
                        subj = edge.get("subject", "")
                        pred = edge.get("predicate", "")
                        obj = edge.get("object", "")
                        if not (subj and pred and obj):
                            continue
                        pred_stripped = _strip_biolink(pred)
                        if pred_filter_set and pred_stripped not in pred_filter_set:
                            continue
                        triples.append((subj, pred_stripped, obj))

                        if load_edge_metadata:
                            if keep_all_edge_metadata:
                                meta = {
                                    k: v for k, v in edge.items() if k not in _EDGE_CORE
                                }
                            else:
                                meta = {k: edge[k] for k in edge_fields if k in edge}
                            if meta:
                                edge_meta[(subj, pred_stripped, obj)] = meta

                elif basename == "nodes.jsonl":
                    if not load_node_metadata:
                        continue  # topology-only load — skip nodes.jsonl entirely
                    for raw_line in fobj:
                        line = raw_line.strip()
                        if not line:
                            continue
                        node = json.loads(line)
                        nid = node.get("id", "")
                        if not nid:
                            continue
                        filtered_node = {"id": nid}
                        # Strip biolink: from categories
                        if "category" in node:
                            filtered_node["category"] = [
                                _strip_biolink(c) for c in node["category"]
                            ]
                        if keep_all_node_metadata:
                            for key, value in node.items():
                                if key == "id":
                                    continue
                                if key == "category":
                                    continue
                                filtered_node[key] = value
                        else:
                            for key in metadata_fields or ():
                                if key in {"id", "category"}:
                                    continue
                                if key in node:
                                    filtered_node[key] = node[key]
                        node_meta[nid] = filtered_node
        finally:
            tar.close()
            for _c in _closeables:
                try:
                    _c.close()
                except Exception:
                    pass

        if not triples:
            raise ValueError(
                "No valid triples extracted from edges.jsonl"
                + (
                    f" (predicate_filter={predicate_filter})"
                    if predicate_filter
                    else ""
                )
            )

        graph = cls(triples)
        graph.node_metadata = {
            nid: meta for nid, meta in node_meta.items() if nid in graph.node_to_id
        }
        graph.edge_metadata = edge_meta

        # Summary
        edge_meta_count = len(graph.edge_metadata)
        edge_meta_note = (
            f", {edge_meta_count:,} edge metadata entries" if edge_meta_count else ""
        )
        print(
            f"Loaded KGX graph from {archive.name}: "
            f"{graph.num_nodes:,} nodes, {graph.edge_count:,} edges, "
            f"{len(graph.relations)} predicates{edge_meta_note}"
        )
        graph.print_memory_usage()
        return graph

    # ------------------------------------------------------------------
    # Serialization (pickle + zstd)
    # ------------------------------------------------------------------

    def save(self, filepath: str, compression_level: int = 3) -> None:
        """Serialize the graph to *filepath* using pickle + zstd compression.

        Parameters
        ----------
        filepath : str
            Destination path (e.g. ``graph.pkl.zst``).
        compression_level : int
            Zstandard compression level (1-22). Default 3.
        """
        if _zstd is None:
            raise ImportError(
                "zstd support is required for save/load. "
                "Use Python >= 3.14 or install the 'zstandard' package."
            )

        t0 = time.time()
        raw = pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL)

        if _BUILTIN_ZSTD:
            compressed = _zstd.compress(raw, level=compression_level)
        else:
            cctx = _zstd.ZstdCompressor(level=compression_level)
            compressed = cctx.compress(raw)

        # Write with a small header so we can verify on load
        with open(filepath, "wb") as fh:
            fh.write(b"CSRG")  # magic
            fh.write(struct.pack("<I", pickle.HIGHEST_PROTOCOL))
            fh.write(compressed)

        elapsed = time.time() - t0
        print(
            f"Saved graph to {filepath} "
            f"({_fmt_bytes(os.path.getsize(filepath))} compressed, "
            f"{_fmt_bytes(len(raw))} uncompressed, {elapsed:.2f}s)"
        )

    @classmethod
    def load(
        cls,
        filepath: str,
        db: MetadataBackend | None = None,
    ) -> CSRGraph:
        """Load a graph previously saved with :meth:`save`.

        Automatically uses the fast memmap path when possible:

        * If a companion ``<stem>.memmap/`` directory exists, the graph is
          loaded from mmap'd binary files — nearly instant, regardless of
          graph size, because the OS only creates page-table entries; actual
          I/O happens lazily as pages are accessed.
        * If the memmap directory does not yet exist but a memory limit has
          been set (see :meth:`set_memory_limit`) and the graph exceeds it,
          the graph is first loaded from pickle, then converted to memmap
          files in the companion directory.  All subsequent runs take the
          fast memmap path automatically.
        * Without a memory limit and no existing memmap directory, the graph
          is loaded from pickle into RAM as before (original behaviour).

        Parameters
        ----------
        filepath : str
            Path to the ``.pkl.zst`` file.
        db : MetadataBackend, optional
            Metadata backend to attach for metadata queries (``get_node``,
            ``filter_nodes``, ``match_path``, etc.).  Can also be set later
            via :meth:`set_db`.
        """
        if _zstd is None:
            raise ImportError(
                "zstd support is required for save/load. "
                "Use Python >= 3.14 or install the 'zstandard' package."
            )

        mmap_dir = cls._memmap_dir(filepath)

        # Fast path: memmap directory already exists — skip pickle entirely.
        if mmap_dir.exists() and (mmap_dir / "meta.json").exists():
            # Validate memmap integrity: check that all expected .bin files
            # exist before attempting to load.  If any are missing the
            # directory is corrupt and we fall through to the pickle path.
            mmap_ok = True
            try:
                with open(mmap_dir / "meta.json") as _mf:
                    _meta = json.load(_mf)
                expected_bins = [
                    "merged.indptr.bin",
                    "merged.indices.bin",
                    "merged.data.bin",
                    "edge_predicate_ids.bin",
                    "component_labels.bin",
                ]
                for rel in _meta.get("relations_meta", {}):
                    s = _LazyCSRDict._stem(rel)
                    expected_bins.append(f"{s}.indptr.bin")
                    expected_bins.append(f"{s}.indices.bin")
                    expected_bins.append(f"{s}.data.bin")
                missing = [b for b in expected_bins if not (mmap_dir / b).exists()]
                if missing:
                    mmap_ok = False
                    print(
                        f"WARNING: memmap directory {mmap_dir} is corrupt — "
                        f"{len(missing)} expected .bin file(s) missing "
                        f"(first 5: {missing[:5]}). "
                        f"Falling back to pickle load. "
                        f"Delete the directory or use --rebuild-graph to rebuild."
                    )
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                mmap_ok = False
                print(
                    f"WARNING: memmap directory {mmap_dir} has invalid meta.json "
                    f"({exc}). Falling back to pickle load. "
                    f"Delete the directory or use --rebuild-graph to rebuild."
                )

            if mmap_ok:
                t0 = time.time()
                graph = cls._load_from_memmap(mmap_dir)
                elapsed = time.time() - t0
                print(
                    f"Loaded graph from memmap ({mmap_dir.name}, {elapsed * 1000:.0f} ms)"
                )
                graph.print_memory_usage()
                graph.db = db
                return graph

        # Normal path: load from pickle.
        t0 = time.time()
        with open(filepath, "rb") as fh:
            magic = fh.read(4)
            if magic != b"CSRG":
                raise ValueError(
                    f"Invalid file format (expected CSRG header): {filepath}"
                )
            _proto = struct.unpack("<I", fh.read(4))[0]  # noqa: F841
            compressed = fh.read()

        if _BUILTIN_ZSTD:
            raw = _zstd.decompress(compressed)
        else:
            import io
            import typing as _t

            # Stream-decompress: the uncompressed size is unknown and pickled
            # numpy/CSR data often compresses far beyond any fixed multiple of
            # the compressed size, so a bounded ``decompress(max_output_size=)``
            # would raise on highly compressible graphs.
            dctx: _t.Any = _zstd.ZstdDecompressor()
            with dctx.stream_reader(io.BytesIO(compressed)) as reader:
                raw = reader.read()

        graph: CSRGraph = pickle.loads(raw)
        elapsed = time.time() - t0
        print(
            f"Loaded graph from {filepath} "
            f"({_fmt_bytes(os.path.getsize(filepath))} on disk, {elapsed:.2f}s)"
        )
        graph.print_memory_usage()

        # If graph exceeds the memory limit, convert to memmap now.
        # The companion directory will be used on all future load() calls.
        if cls._memory_limit is not None:
            total = graph.memory_usage()["total"]
            if total > cls._memory_limit:
                print(
                    f"Graph RAM {_fmt_bytes(total)} exceeds limit "
                    f"{_fmt_bytes(cls._memory_limit)}; building memmap at "
                    f"{mmap_dir} ..."
                )
                graph._to_memmap(mmap_dir)

        graph.db = db
        return graph

    # ------------------------------------------------------------------
    # Memory-limit control
    # ------------------------------------------------------------------

    @classmethod
    def set_memory_limit(cls, limit: int | str | None) -> None:
        """Set the global in-RAM threshold that triggers memmap loading.

        When a graph's estimated RAM footprint exceeds *limit*, ``load()``
        will build (on the first run) or use (on subsequent runs) a companion
        ``<stem>.memmap/`` directory of flat binary files.  Loading from that
        directory is nearly instant regardless of graph size.

        Parameters
        ----------
        limit : int | str | None
            - ``None``       — disable (always load into RAM).
            - ``int``        — threshold in bytes.
            - ``"auto"``     — use all currently available physical RAM.
            - ``"Xg"`` / ``"Xm"`` / ``"Xk"``  — e.g. ``"8g"`` for 8 GiB.
        """
        if limit is None:
            cls._memory_limit = None
            return
        if isinstance(limit, int):
            cls._memory_limit = limit
            return
        if isinstance(limit, str):
            s = limit.strip().lower()
            if s == "auto":
                cls._memory_limit = _available_memory()
                return
            for suffix, mul in (
                ("gib", 1024 ** 3), ("gb", 1024 ** 3), ("g", 1024 ** 3),
                ("mib", 1024 ** 2), ("mb", 1024 ** 2), ("m", 1024 ** 2),
                ("kib", 1024),      ("kb", 1024),      ("k", 1024),
            ):
                if s.endswith(suffix):
                    cls._memory_limit = int(float(s[: -len(suffix)]) * mul)
                    return
            cls._memory_limit = int(s)
            return
        raise TypeError(f"limit must be int, str, or None; got {type(limit)}")

    # ------------------------------------------------------------------
    # Memmap helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _memmap_dir(pkl_path: str) -> Path:
        """Return the companion memmap directory path for a ``.pkl.zst`` file.

        ``/data/kg.csrgraph.pkl.zst``  →  ``/data/kg.csrgraph.memmap/``
        """
        p = Path(pkl_path)
        name = p.name
        for suf in (".pkl.zst", ".pkl"):
            if name.endswith(suf):
                name = name[: -len(suf)]
                break
        return p.parent / f"{name}.memmap"

    def _to_memmap(self, directory: Path) -> None:
        """Convert in-memory CSR arrays to memory-mapped binary files.

        Creates *directory* and writes one ``.bin`` file per numpy array, plus
        a ``meta.json`` with shapes, dtypes, and sizes.  After the call:

        * ``csr_by_relation`` is replaced with a :class:`_LazyCSRDict` that
          mmap's each predicate matrix on first access.
        * ``csr_merged``, ``edge_predicate_ids``, and ``_component_labels``
          are replaced with read-only memmap views.

        The PKL file is **not** modified; it remains the portable source of
        truth.  The memmap directory is a derived cache.

        Note: in-RAM ``node_metadata`` / ``edge_metadata`` are not persisted
        here.  Use a :class:`MetadataBackend` for metadata at scale.
        """
        directory.mkdir(parents=True, exist_ok=True)

        def _write(fname: str, arr: np.ndarray) -> None:
            mm = np.memmap(directory / fname, dtype=arr.dtype, mode="w+", shape=arr.shape)
            mm[:] = arr
            mm.flush()
            del mm

        relations_meta: dict = {}
        for rel, csr in self.csr_by_relation.items():
            s = _LazyCSRDict._stem(rel)
            _write(f"{s}.indptr.bin",  csr.indptr)
            _write(f"{s}.indices.bin", csr.indices)
            _write(f"{s}.data.bin",    csr.data)
            relations_meta[rel] = {
                "indptr_dtype":  str(csr.indptr.dtype),
                "indices_dtype": str(csr.indices.dtype),
                "data_dtype":    str(csr.data.dtype),
                "nnz":           int(csr.nnz),
            }

        _write("merged.indptr.bin",       self.csr_merged.indptr)
        _write("merged.indices.bin",      self.csr_merged.indices)
        _write("merged.data.bin",         self.csr_merged.data)
        _write("edge_predicate_ids.bin",  self.edge_predicate_ids)
        _write("component_labels.bin",    self._component_labels)

        # Compute total array bytes before we replace anything
        total_bytes = (
            self.csr_merged.indptr.nbytes
            + self.csr_merged.indices.nbytes
            + self.csr_merged.data.nbytes
            + self.edge_predicate_ids.nbytes
            + self._component_labels.nbytes
            + sum(
                c.indptr.nbytes + c.indices.nbytes + c.data.nbytes
                for c in self.csr_by_relation.values()
            )
        )

        meta: dict = {
            "nodes":            self.nodes,
            "relations":        self.relations,
            "edge_count":       self.edge_count,
            "predicate_counts": self.predicate_counts,
            "num_nodes":        self.num_nodes,
            "merged": {
                "indptr_dtype":  str(self.csr_merged.indptr.dtype),
                "indices_dtype": str(self.csr_merged.indices.dtype),
                "data_dtype":    str(self.csr_merged.data.dtype),
                "nnz":           int(self.csr_merged.nnz),
            },
            "edge_predicate_ids_dtype": str(self.edge_predicate_ids.dtype),
            "component_labels_dtype":   str(self._component_labels.dtype),
            "relations_meta":  relations_meta,
            "total_bytes":     total_bytes,
        }
        with open(directory / "meta.json", "w") as fh:
            json.dump(meta, fh)

        # Replace in-memory arrays with mmap-backed views in place so the
        # current process immediately benefits from reduced page-cache pressure.
        n = self.num_nodes

        def _mmap_r(fname: str, dtype: str) -> np.ndarray:
            return np.memmap(directory / fname, dtype=dtype, mode="r")  # type: ignore[return-value]

        m = meta["merged"]
        self.csr_merged = csr_matrix(
            (
                _mmap_r("merged.data.bin",    m["data_dtype"]),
                _mmap_r("merged.indices.bin", m["indices_dtype"]),
                _mmap_r("merged.indptr.bin",  m["indptr_dtype"]),
            ),
            shape=(n, n),
        )
        self.edge_predicate_ids = _mmap_r(
            "edge_predicate_ids.bin", meta["edge_predicate_ids_dtype"]
        )
        self._component_labels = _mmap_r(
            "component_labels.bin", meta["component_labels_dtype"]
        )
        self.csr_by_relation = _LazyCSRDict(directory, relations_meta, n)  # type: ignore[assignment]

        print(f"Converted graph to memmap: {directory}")

    @classmethod
    def _load_from_memmap(cls, directory: Path) -> CSRGraph:
        """Load a CSRGraph directly from a memmap directory (fast, zero-copy).

        Bypasses pickle decompression entirely.  ``csr_merged``,
        ``component_labels``, and ``edge_predicate_ids`` are mmap'd
        immediately; per-predicate matrices are mmap'd lazily on first access.
        """
        with open(directory / "meta.json") as fh:
            meta = json.load(fh)

        obj: CSRGraph = cls.__new__(cls)
        obj.nodes            = meta["nodes"]
        obj.relations        = meta["relations"]
        obj.edge_count       = int(meta["edge_count"])
        obj.predicate_counts = {k: int(v) for k, v in meta["predicate_counts"].items()}
        obj.num_nodes        = int(meta["num_nodes"])
        obj.node_to_id       = {n: i for i, n in enumerate(obj.nodes)}
        obj.rel_to_id        = {r: i for i, r in enumerate(obj.relations)}
        obj.node_metadata    = {}
        obj.edge_metadata    = {}

        n = obj.num_nodes

        def _mmap_r(fname: str, dtype: str) -> np.ndarray:
            return np.memmap(directory / fname, dtype=dtype, mode="r")  # type: ignore[return-value]

        m = meta["merged"]
        obj.csr_merged = csr_matrix(
            (
                _mmap_r("merged.data.bin",    m["data_dtype"]),
                _mmap_r("merged.indices.bin", m["indices_dtype"]),
                _mmap_r("merged.indptr.bin",  m["indptr_dtype"]),
            ),
            shape=(n, n),
        )
        obj.edge_predicate_ids = _mmap_r(
            "edge_predicate_ids.bin", meta["edge_predicate_ids_dtype"]
        )
        obj._component_labels = _mmap_r(
            "component_labels.bin", meta["component_labels_dtype"]
        )
        obj._n_components = int(np.max(obj._component_labels)) + 1 if n else 0

        obj.csr_by_relation = _LazyCSRDict(  # type: ignore[assignment]
            directory, meta["relations_meta"], n
        )

        return obj

    # ------------------------------------------------------------------
    # Memory reporting
    # ------------------------------------------------------------------

    def memory_usage(self) -> Dict[str, int]:
        """Return a breakdown of memory usage in bytes."""

        def _csr_bytes(m: csr_matrix) -> int:
            return m.data.nbytes + m.indices.nbytes + m.indptr.nbytes

        def _deep_sizeof(obj: object, _seen: set | None = None) -> int:
            """Recursively estimate memory for nested Python objects (dict/list/tuple/str/etc.)."""
            if _seen is None:
                _seen = set()
            oid = id(obj)
            if oid in _seen:
                return 0
            _seen.add(oid)
            size = sys.getsizeof(obj)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    size += _deep_sizeof(k, _seen) + _deep_sizeof(v, _seen)
            elif isinstance(obj, (list, tuple)):
                for item in obj:
                    size += _deep_sizeof(item, _seen)
            return size

        usage: Dict[str, int] = {}

        # CSR matrices
        merged_bytes = _csr_bytes(self.csr_merged)
        usage["csr_merged"] = merged_bytes

        if isinstance(self.csr_by_relation, _LazyCSRDict):
            # Estimate per-predicate CSR sizes from metadata without
            # materializing any lazy matrices — preserves zero-copy benefit.
            per_rel_bytes = 0
            for m in self.csr_by_relation._relations_meta.values():
                n = self.csr_by_relation._num_nodes
                nnz = m["nnz"]
                indptr_bytes = (n + 1) * np.dtype(m["indptr_dtype"]).itemsize
                indices_bytes = nnz * np.dtype(m["indices_dtype"]).itemsize
                data_bytes = nnz * np.dtype(m["data_dtype"]).itemsize
                per_rel_bytes += indptr_bytes + indices_bytes + data_bytes
        else:
            per_rel_bytes = 0
            for m in self.csr_by_relation.values():
                per_rel_bytes += _csr_bytes(m)
        usage["csr_by_relation"] = per_rel_bytes

        # Component labels
        usage["component_labels"] = self._component_labels.nbytes

        # Representative predicate IDs aligned with csr_merged.indices
        edge_predicate_bytes = self.edge_predicate_ids.nbytes
        usage["edge_predicate"] = edge_predicate_bytes

        # Node / relation lookup dicts
        id_map_bytes = 0
        for d in (self.node_to_id, self.rel_to_id):
            id_map_bytes += sys.getsizeof(d)
            for k, v in d.items():
                id_map_bytes += sys.getsizeof(k) + sys.getsizeof(v)
        id_map_bytes += sys.getsizeof(self.nodes)
        for n in self.nodes:
            id_map_bytes += sys.getsizeof(n)
        id_map_bytes += sys.getsizeof(self.relations)
        for r in self.relations:
            id_map_bytes += sys.getsizeof(r)
        usage["id_maps"] = id_map_bytes

        # Node metadata (shallow: values are flat dicts of str→str/list[str])
        meta_bytes = sys.getsizeof(self.node_metadata)
        for k, v in self.node_metadata.items():
            meta_bytes += sys.getsizeof(k) + _deep_sizeof(v)
        usage["node_metadata"] = meta_bytes

        # Edge metadata (deep: values may contain nested dicts/lists)
        edge_meta_bytes = sys.getsizeof(self.edge_metadata)
        seen: set[int] = set()
        for k, v in self.edge_metadata.items():
            edge_meta_bytes += sys.getsizeof(k) + _deep_sizeof(v, seen)
        usage["edge_metadata"] = edge_meta_bytes

        # Predicate counts
        predicate_count_bytes = sys.getsizeof(self.predicate_counts)
        for k, v in self.predicate_counts.items():
            predicate_count_bytes += sys.getsizeof(k) + sys.getsizeof(v)
        usage["predicate_counts"] = predicate_count_bytes

        usage["total"] = sum(usage.values())
        return usage

    def print_memory_usage(self) -> None:
        """Print a memory usage summary to stdout."""
        usage = self.memory_usage()
        total = usage.pop("total")
        print(f"  Memory usage ({_fmt_bytes(total)} total):")
        for key in sorted(usage, key=usage.get, reverse=True):  # type: ignore[arg-type]
            print(f"    {key:25s} {_fmt_bytes(usage[key]):>10s}")

    # ------------------------------------------------------------------
    # Node metadata helpers
    # ------------------------------------------------------------------

    def get_edge_metadata(self, subject: str, predicate: str, obj: str) -> dict:
        """Return stored metadata for an edge, or an empty dict if not available.

        Parameters
        ----------
        subject : str
            Subject node CURIE (e.g. ``"CHEBI:6801"``).
        predicate : str
            Predicate, with or without the ``biolink:`` prefix.
        obj : str
            Object node CURIE.
        """
        return self.edge_metadata.get((subject, _strip_biolink(predicate), obj), {})

    def get_node_name(self, node_id: str) -> str:
        """Return the human-readable name for a node, or the ID if unavailable."""
        meta = self.node_metadata.get(node_id, {})
        return meta.get("name", node_id)

    def get_node_categories(self, node_id: str) -> List[str]:
        """Return the biolink categories for a node (with ``biolink:`` prefix)."""
        meta = self.node_metadata.get(node_id, {})
        return [_add_biolink(c) for c in meta.get("category", [])]

    # ------------------------------------------------------------------
    # Metadata backend integration
    # ------------------------------------------------------------------

    def set_db(self, db: MetadataBackend) -> None:
        """Attach a metadata backend for metadata-aware queries.

        Parameters
        ----------
        db : MetadataBackend
            Any open metadata backend (LMDB, Elasticsearch, Hybrid, etc.).
        """
        self.db = db

    def _require_db(self) -> MetadataBackend:
        """Return the attached db or raise a clear error."""
        if self.db is None:
            raise RuntimeError(
                "No metadata backend attached. "
                "Pass db= to CSRGraph.load() or call graph.set_db(db)."
            )
        return self.db

    def get_node(self, node_id: str) -> dict:
        """Look up full metadata for a node via the attached backend."""
        return self._require_db().get_node(node_id)

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        """Look up full metadata for an edge via the attached backend."""
        return self._require_db().get_edge(subject, predicate, obj)

    def filter_nodes(
        self,
        node_ids: List[str],
        *,
        category: Optional[str] = None,
        extra_filters: Optional[dict] = None,
    ) -> List[dict]:
        """Filter nodes by category or extra metadata fields via the attached backend."""
        return self._require_db().filter_nodes(
            node_ids, category=category, extra_filters=extra_filters,
        )

    def filter_edges(
        self,
        edges: List[PathEdge],
        *,
        knowledge_level: Optional[str] = None,
        agent_type: Optional[str] = None,
        extra_filters: Optional[dict] = None,
    ) -> List[dict]:
        """Filter edges by knowledge_level, agent_type, or extra fields via the attached backend."""
        return self._require_db().filter_edges(
            edges,
            knowledge_level=knowledge_level,
            agent_type=agent_type,
            extra_filters=extra_filters,
        )

    def close(self) -> None:
        """Close the attached metadata backend, if any."""
        if self.db is not None:
            self.db.close()
            self.db = None

    def has_edge(
        self,
        source: str,
        target: str,
        relation: Optional[str] = None,
    ) -> bool:
        """Check whether a directed edge exists from *source* to *target*.

        Parameters
        ----------
        relation : str, optional
            If given, only check for an edge with this predicate (with or
            without ``biolink:`` prefix).  If ``None``, check any predicate.
        """
        if source not in self.node_to_id or target not in self.node_to_id:
            return False
        u = self.node_to_id[source]
        v = self.node_to_id[target]
        rel = _normalize_relation(relation)
        csr = self.csr_merged if rel is None else self.csr_by_relation.get(rel)
        if csr is None:
            return False
        start, end = int(csr.indptr[u]), int(csr.indptr[u + 1])
        for j in range(start, end):
            if int(csr.indices[j]) == v:
                return True
        return False

    def edges_between(
        self,
        source: str,
        target: str,
    ) -> List[str]:
        """Return all predicate labels for edges from *source* to *target*.

        Returns a list of predicate strings (with ``biolink:`` prefix),
        or an empty list if no edges exist.
        """
        if source not in self.node_to_id or target not in self.node_to_id:
            return []
        u = self.node_to_id[source]
        v = self.node_to_id[target]
        preds: List[str] = []
        for rel, csr in self.csr_by_relation.items():
            start, end = int(csr.indptr[u]), int(csr.indptr[u + 1])
            for j in range(start, end):
                if int(csr.indices[j]) == v:
                    preds.append(_add_biolink(rel))
                    break
        return preds

    # ------------------------------------------------------------------
    # Graph traversal methods
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Subclass-of expansion helpers
    # ------------------------------------------------------------------

    #: Predicates treated as semantic "is-a" / subtype links for node
    #: subclassing.  The canonical biolink form is ``biolink:subclass_of``
    #: (stored stripped as ``subclass_of``).  Some current releases of
    #: translator_kg instead emit the raw ``rdfs:subClassOf`` CURIE — a known
    #: data error slated to be normalised back to ``biolink:subclass_of`` in a
    #: future release.  Both names are listed (canonical first) and every
    #: present variant is unioned, so ``node_subclassing=True`` works on the
    #: current graph, on the fixed future graph, and on a mixed transition
    #: graph alike — no code change needed when the data is corrected.
    SUBCLASS_PREDICATES: tuple[str, ...] = ("subclass_of", "rdfs:subClassOf")

    def _get_subclass_of_T(self) -> Optional[csr_matrix]:
        """Lazily compute and cache the transpose of the subclass adjacency.

        The subclass edges (``child --subclass_of--> parent``) are taken from
        every predicate listed in :attr:`SUBCLASS_PREDICATES` that is present
        in the graph, and unioned.  The transposed matrix has
        ``M[parent, child] = 1``, enabling efficient BFS over
        children/descendants without scanning every edge.  Returns ``None``
        when no subclass edges are present in the graph.
        """
        if not hasattr(self, "_subclass_of_T_cache"):
            mats = [
                self.csr_by_relation[p]
                for p in self.SUBCLASS_PREDICATES
                if p in self.csr_by_relation
            ]
            if not mats:
                combined: Optional[csr_matrix] = None
            elif len(mats) == 1:
                combined = mats[0]
            else:
                # Union of all subclass adjacency matrices (binary OR).
                combined = mats[0].copy()
                for m in mats[1:]:
                    combined = combined + m
                combined = (combined > 0).tocsr()
            self._subclass_of_T_cache: Optional[csr_matrix] = (
                combined.T.tocsr() if combined is not None else None
            )
        return self._subclass_of_T_cache

    def _expand_subclasses(
        self,
        node_id: int,
        max_depth: Optional[int] = None,
    ) -> frozenset[int]:
        """Return *node_id* plus descendant IDs reachable via subclass edges.

        BFS on the transposed subclass matrix (parent→children direction), built
        from every predicate in :attr:`SUBCLASS_PREDICATES` present in the
        graph.  Returns a singleton ``{node_id}`` when no subclass edges exist.

        *max_depth* bounds how many subclass hops are followed: ``1`` takes only
        direct children (what TRAPI engines conventionally do), while ``None``
        follows the hierarchy transitively.  Depth matters — an ontology chain
        several levels deep expands to a much larger set transitively, so two
        engines using different depths give different answers on the same graph.
        """
        T = self._get_subclass_of_T()
        if T is None or max_depth == 0:
            return frozenset({node_id})
        result: set[int] = {node_id}
        frontier: deque[tuple[int, int]] = deque([(node_id, 0)])
        while frontier:
            cur, depth = frontier.popleft()
            if max_depth is not None and depth >= max_depth:
                continue
            start = int(T.indptr[cur])
            end   = int(T.indptr[cur + 1])
            for j in range(start, end):
                child = int(T.indices[j])
                if child not in result:
                    result.add(child)
                    frontier.append((child, depth + 1))
        return frozenset(result)

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def neighbors(
        self,
        node: str,
        relation: Optional[str] = None,
        node_subclassing: bool = False,
    ) -> List[str]:
        """Return outgoing neighbours, optionally filtered by *relation*.

        *relation* may include or omit the ``biolink:`` prefix.

        Parameters
        ----------
        node_subclassing:
            When ``True`` the result includes neighbours of all descendant nodes
            (i.e. nodes that are ``subclass_of`` *node*, transitively) in
            addition to direct neighbours of *node* itself.
        """
        if node not in self.node_to_id:
            raise ValueError(f"Unknown node: {node}")

        relation = _normalize_relation(relation)
        u = self.node_to_id[node]
        csr = (
            self.csr_merged if relation is None else self.csr_by_relation.get(relation)
        )

        if csr is None:
            return []

        if node_subclassing:
            seen: set[int] = set()
            result: List[str] = []
            for uid in self._expand_subclasses(u):
                start = int(csr.indptr[uid])
                end   = int(csr.indptr[uid + 1])
                for j in range(start, end):
                    v = int(csr.indices[j])
                    if v not in seen:
                        seen.add(v)
                        result.append(self.nodes[v])
            return result

        start = csr.indptr[u]
        end = csr.indptr[u + 1]
        nbr_ids = csr.indices[start:end]
        return [self.nodes[int(v)] for v in nbr_ids]

    def _neighbor_ids(
        self,
        node_id: int,
        relation: Optional[str] = None,
    ) -> np.ndarray:
        csr = (
            self.csr_merged if relation is None else self.csr_by_relation.get(relation)
        )
        if csr is None:
            return np.array([], dtype=np.int32)

        start = csr.indptr[node_id]
        end = csr.indptr[node_id + 1]
        return csr.indices[start:end]

    def _get_graph_for_relation(self, relation: Optional[str]) -> Optional[csr_matrix]:
        if relation is None:
            return self.csr_merged
        return self.csr_by_relation.get(relation)

    def _path_ids_to_edges(
        self,
        path: List[int],
        relation: Optional[str] = None,
    ) -> List[PathEdge]:
        edge_path: List[PathEdge] = []

        for i in range(len(path) - 1):
            u = path[i]
            v = path[i + 1]

            if relation is not None:
                pred = _add_biolink(relation)
            else:
                rel_id = self._edge_relation_id(u, v)
                pred = (
                    _add_biolink(self.relations[rel_id]) if rel_id is not None else None
                )

            edge_path.append((self.nodes[u], pred, self.nodes[v]))

        return edge_path

    def _can_possibly_reach(
        self,
        src_id: int,
        tgt_id: int,
        relation: Optional[str] = None,
    ) -> bool:
        if relation is None:
            return bool(
                self._component_labels[src_id] == self._component_labels[tgt_id]
            )

        graph = self._get_graph_for_relation(relation)
        if graph is None:
            return False

        if relation not in self._relation_component_cache:
            _, labels = connected_components(
                graph,
                directed=True,
                connection="weak",
                return_labels=True,
            )
            self._relation_component_cache[relation] = labels

        labels = self._relation_component_cache[relation]
        return bool(labels[src_id] == labels[tgt_id])

    def _reverse_merged(self) -> csr_matrix:
        """Lazily cache the transpose of the merged adjacency (topology only).

        Used for backward reachability probes.  Costs roughly the same as
        ``csr_merged`` itself, so it is built only when a query actually needs
        backward traversal.
        """
        if not hasattr(self, "_reverse_merged_cache"):
            self._reverse_merged_cache: csr_matrix = self.csr_merged.T.tocsr()
        return self._reverse_merged_cache

    #: Sentinel distance meaning "farther than the bounded BFS looked".
    _DIST_FAR = 255

    def _reach_masks(
        self,
        target_ids: frozenset[int],
        max_depth: int,
    ) -> List[np.ndarray]:
        """Masks of the nodes within ``r`` hops of a target, for ``r`` in 0..*max_depth*.

        Element ``r`` is a boolean array over node indices, true where a node can
        reach one of *target_ids* in at most ``r`` hops.  Backed by a bounded
        multi-source BFS over the reversed merged adjacency, so only the
        ``max_depth``-hop backward neighbourhood is touched rather than the whole
        graph.  All masks are built and cached together, keeping the per-hop cost
        of a query to a dict lookup.

        Topology only: predicate and metadata constraints are ignored, which
        makes these an **admissible** bound.  Constraints can only remove paths,
        so a node these report as too far to reach a target within the remaining
        hops cannot lie on a matching path — pruning on it never discards a
        valid result.
        """
        cache = getattr(self, "_reach_cache", None)
        if cache is None:
            cache = self._reach_cache = {}
        key = (target_ids, max_depth)
        hit = cache.get(key)
        if hit is not None:
            return hit

        rev = self._reverse_merged()
        indptr, indices = rev.indptr, rev.indices
        dist = np.full(self.num_nodes, self._DIST_FAR, dtype=np.uint8)
        frontier = np.fromiter(target_ids, dtype=np.int64, count=len(target_ids))
        dist[frontier] = 0

        for d in range(1, min(max_depth, self._DIST_FAR - 1) + 1):
            if frontier.size == 0:
                break
            _, nbrs = _csr_ragged_gather(indptr, indices, frontier)
            if nbrs.size == 0:
                break
            nbrs = nbrs[dist[nbrs] == self._DIST_FAR]
            if nbrs.size == 0:
                break
            frontier = np.unique(nbrs)
            dist[frontier] = d

        masks = [dist <= r for r in range(max_depth + 1)]

        # Bounded by the number of distinct (target set, depth) pairs a process
        # sees; keep it small so long-lived servers do not accumulate arrays.
        if len(cache) > 32:
            cache.clear()
        cache[key] = masks
        return masks

    def all_paths(
        self,
        source: str,
        target: str,
        relation: Optional[str] = None,
        max_depth: Optional[int] = DEFAULT_ALL_PATHS_MAX_DEPTH,
        node_subclassing: bool = False,
    ) -> List[List[PathEdge]]:
        """Return all simple paths from *source* to *target*.

        *relation* may include or omit the ``biolink:`` prefix.

        Parameters
        ----------
        max_depth:
            Maximum number of hops, defaulting to
            :data:`DEFAULT_ALL_PATHS_MAX_DEPTH` (5).  Enumeration is a recursive
            DFS whose cost grows exponentially with depth, so on a graph of any
            size an unbounded search does not finish and deep chains can also
            exhaust the recursion limit.  ``None`` removes the bound explicitly —
            reasonable on a small or sparse graph, a bad idea otherwise.

            Note that this bounds *depth*, not the number of results: a shallow
            search through a hub can still return very many paths.
        node_subclassing:
            When ``True``, paths may start from any subclass of *source* and
            end at any subclass of *target* (both sets expanded transitively
            via ``subclass_of``).
        """
        if source not in self.node_to_id:
            raise ValueError(f"Unknown source node: {source}")
        if target not in self.node_to_id:
            raise ValueError(f"Unknown target node: {target}")

        relation = _normalize_relation(relation)

        if relation is not None and relation not in self.csr_by_relation:
            return []

        src_id = self.node_to_id[source]
        tgt_id = self.node_to_id[target]

        src_ids = self._expand_subclasses(src_id) if node_subclassing else frozenset({src_id})
        tgt_ids = self._expand_subclasses(tgt_id) if node_subclassing else frozenset({tgt_id})

        results: List[List[PathEdge]] = []

        def dfs(current: int, visited: set[int], path: List[int]) -> None:
            if max_depth is not None and len(path) - 1 > max_depth:
                return

            if current in tgt_ids:
                results.append(self._path_ids_to_edges(path, relation=relation))
                return

            for nxt in self._neighbor_ids(current, relation=relation):
                nxt = int(nxt)
                if nxt in visited:
                    continue

                visited.add(nxt)
                path.append(nxt)
                dfs(nxt, visited, path)
                path.pop()
                visited.remove(nxt)

        for sid in src_ids:
            if not self._can_possibly_reach(sid, tgt_id, relation=relation):
                continue
            dfs(sid, {sid}, [sid])

        return results

    def shortest_path(
        self,
        source: str,
        target: str,
        relation: Optional[str] = None,
        node_subclassing: bool = False,
    ) -> Optional[List[PathEdge]]:
        """Return one shortest path (unweighted BFS via SciPy).

        *relation* may include or omit the ``biolink:`` prefix.

        .. note::
            When *relation* is ``None`` the path runs over the merged graph,
            which stores only a single *representative* predicate per directed
            node pair.  For multigraphs (multiple predicates between the same
            two nodes) the reported predicate is one of them, not necessarily
            the only one.  Pass an explicit *relation* to traverse a specific
            predicate, or use :meth:`edges_between` to enumerate all predicates
            between two nodes.

        Parameters
        ----------
        node_subclassing:
            When ``True``, returns the globally shortest path that starts from
            any subclass of *source* and ends at any subclass of *target*.
        """
        if source not in self.node_to_id:
            raise ValueError(f"Unknown source node: {source}")
        if target not in self.node_to_id:
            raise ValueError(f"Unknown target node: {target}")

        relation = _normalize_relation(relation)
        graph = self._get_graph_for_relation(relation)
        if graph is None:
            return None

        src_id = self.node_to_id[source]
        tgt_id = self.node_to_id[target]

        src_ids = self._expand_subclasses(src_id) if node_subclassing else (src_id,)
        tgt_ids = self._expand_subclasses(tgt_id) if node_subclassing else (tgt_id,)

        best: Optional[List[PathEdge]] = None

        for sid in src_ids:
            if not self._can_possibly_reach(sid, tgt_id, relation=relation):
                continue

            dist, predecessors = csgraph_shortest_path(
                graph,
                directed=True,
                unweighted=True,
                indices=sid,
                return_predecessors=True,
            )

            for tid in tgt_ids:
                if np.isinf(dist[tid]):
                    continue

                path_ids: List[int] = []
                cur = tid
                while cur != sid:
                    path_ids.append(cur)
                    cur = int(predecessors[cur])
                    if cur < 0:
                        break
                else:
                    path_ids.append(sid)
                    path_ids.reverse()
                    candidate = self._path_ids_to_edges(path_ids, relation=relation)
                    if best is None or len(candidate) < len(best):
                        best = candidate

        return best

    def all_shortest_paths(
        self,
        source: str,
        target: str,
        relation: Optional[str] = None,
        node_subclassing: bool = False,
    ) -> List[List[PathEdge]]:
        """Return **all** shortest paths from *source* to *target*.

        *relation* may include or omit the ``biolink:`` prefix.

        .. note::
            With *relation* ``None`` the merged graph reports a single
            representative predicate per node pair; see :meth:`shortest_path`.

        Parameters
        ----------
        node_subclassing:
            When ``True``, sources and targets are expanded to their full
            subclass sets; all shortest paths among any (src_subclass,
            tgt_subclass) pair are returned at the globally minimum hop count.
        """
        if source not in self.node_to_id:
            raise ValueError(f"Unknown source node: {source}")
        if target not in self.node_to_id:
            raise ValueError(f"Unknown target node: {target}")

        relation = _normalize_relation(relation)

        if relation is not None and relation not in self.csr_by_relation:
            return []

        src_id = self.node_to_id[source]
        tgt_id = self.node_to_id[target]

        src_ids = self._expand_subclasses(src_id) if node_subclassing else frozenset({src_id})
        tgt_ids = self._expand_subclasses(tgt_id) if node_subclassing else frozenset({tgt_id})

        # Multi-source BFS: initialise all source subclasses at distance 0
        queue: deque[int] = deque(src_ids)
        dist: Dict[int, int] = {sid: 0 for sid in src_ids}
        parents: Dict[int, List[int]] = defaultdict(list)

        while queue:
            current = queue.popleft()
            current_dist = dist[current]

            for nxt in self._neighbor_ids(current, relation=relation):
                nxt = int(nxt)

                if nxt not in dist:
                    dist[nxt] = current_dist + 1
                    parents[nxt].append(current)
                    queue.append(nxt)
                elif dist[nxt] == current_dist + 1:
                    parents[nxt].append(current)

        reached = [t for t in tgt_ids if t in dist]
        if not reached:
            return []

        # Only backtrack from targets at the globally minimum distance
        min_dist = min(dist[t] for t in reached)
        reached = [t for t in reached if dist[t] == min_dist]

        results: List[List[PathEdge]] = []

        def backtrack(node: int, reversed_path: List[int]) -> None:
            if node in src_ids:
                path_ids = list(reversed(reversed_path))
                results.append(self._path_ids_to_edges(path_ids, relation=relation))
                return

            for par in parents[node]:
                reversed_path.append(par)
                backtrack(par, reversed_path)
                reversed_path.pop()

        for tid in reached:
            backtrack(tid, [tid])

        return results

    def paths_by_predicate_sequence(
        self,
        source: str,
        target: str,
        predicate_sequence: List[str],
        node_subclassing: bool = False,
    ) -> List[List[PathEdge]]:
        """Return paths matching an exact ordered predicate sequence.

        Predicate values may include or omit the ``biolink:`` prefix.

        Parameters
        ----------
        node_subclassing:
            When ``True``, paths may start from any subclass of *source* and
            terminate at any subclass of *target*.
        """
        if source not in self.node_to_id:
            raise ValueError(f"Unknown source node: {source}")
        if target not in self.node_to_id:
            raise ValueError(f"Unknown target node: {target}")

        # Normalize to internal (stripped) form
        seq = [_strip_biolink(p) for p in predicate_sequence]

        for rel in seq:
            if rel not in self.csr_by_relation:
                return []

        src_id = self.node_to_id[source]
        tgt_id = self.node_to_id[target]

        src_ids = self._expand_subclasses(src_id) if node_subclassing else frozenset({src_id})
        tgt_ids = self._expand_subclasses(tgt_id) if node_subclassing else frozenset({tgt_id})

        results: List[List[PathEdge]] = []

        def dfs(depth: int, current: int, path: List[int], visited: set[int]) -> None:
            if depth == len(seq):
                if current in tgt_ids:
                    edge_path: List[PathEdge] = []
                    for i, rel in enumerate(seq):
                        u = path[i]
                        v = path[i + 1]
                        edge_path.append(
                            (
                                self.nodes[u],
                                _add_biolink(rel),
                                self.nodes[v],
                            )
                        )
                    results.append(edge_path)
                return

            rel = seq[depth]
            for nxt in self._neighbor_ids(current, relation=rel):
                nxt = int(nxt)
                # Enforce simple paths: never revisit a node already on the
                # current path, otherwise self-loops / cycles produce
                # infinite or combinatorially exploding results.
                if nxt in visited:
                    continue
                path.append(nxt)
                visited.add(nxt)
                dfs(depth + 1, nxt, path, visited)
                visited.remove(nxt)
                path.pop()

        for sid in src_ids:
            dfs(0, sid, [sid], {sid})

        return results

    @overload
    def match_path(
        self,
        path_spec: list,
        limit: int = ...,
        node_subclassing: bool = ...,
        db: MetadataBackend | None = ...,
        return_stats: Literal[False] = ...,
        hop_directions: Optional[List[Optional[bool]]] = ...,
        subclass_depth: Optional[int] = ...,
    ) -> List[List[PathEdge]]: ...

    @overload
    def match_path(
        self,
        path_spec: list,
        limit: int = ...,
        node_subclassing: bool = ...,
        db: MetadataBackend | None = ...,
        *,
        return_stats: Literal[True],
        hop_directions: Optional[List[Optional[bool]]] = ...,
        subclass_depth: Optional[int] = ...,
    ) -> Tuple[List[List[PathEdge]], MatchStats]: ...

    def match_path(
        self,
        path_spec: list,
        limit: int = 100,
        node_subclassing: bool = False,
        db: MetadataBackend | None = None,
        return_stats: bool = False,
        hop_directions: Optional[List[Optional[bool]]] = None,
        subclass_depth: Optional[int] = None,
    ) -> List[List[PathEdge]] | Tuple[List[List[PathEdge]], MatchStats]:
        """Find all paths matching a fixed-length node/edge pattern.

        Combines in-memory CSR topology traversal (this graph) with metadata
        filtering via any :class:`MetadataBackend` implementation.

        Parameters
        ----------
        db : MetadataBackend, optional
            Metadata backend to use.  Falls back to ``self.db`` when omitted.
        return_stats : bool
            When ``True`` return ``(paths, MatchStats)`` instead of just the
            paths, so callers can tell a complete result from a capped one.
            Truncation is logged as a warning either way.
        hop_directions : list[bool | None], optional
            Per-hop edge orientation, one entry per hop.  ``True`` (the default
            for every hop) walks ``subject -> object``; ``False`` walks
            ``object -> subject``, i.e. "which nodes point at this one".  Needed
            when a pattern is anchored at the object end of an edge — without it
            such a pattern matches nothing, since the walk would follow edges the
            wrong way.  Returned ``PathEdge`` tuples always carry the true
            ``(subject, predicate, object)`` orientation regardless.

            ``None`` marks a **symmetric** hop and walks both ways, unioned.
            Biolink declares 39 predicates symmetric (``interacts_with``,
            ``associated_with``, ...), and for those an assertion stored one way
            answers a query posed the other way.  Without this, symmetric queries
            had to fall back to the general subgraph matcher, which costs an
            order of magnitude: on the HelmsDeep ``two_hop_lookup`` shape this
            path takes 0.33 s where the backtracking matcher takes 26.7 s.
            A node reachable both ways under the same predicate is emitted once,
            keeping the stored orientation of the forward edge.
        path_spec : list
            Alternating ``[NodeSpec, EdgeSpec, NodeSpec, EdgeSpec, ..., NodeSpec]``.
            Length must be odd and >= 3 (at least one hop).

            **NodeSpec** — one of:

            - ``str``  — exact node CURIE, e.g. ``"CHEBI:6801"``
            - ``dict`` — metadata filter, e.g. ``{"category": "biolink:Gene"}``
              or ``{"id": "HGNC:1234"}``
            - ``None`` — wildcard (any node). *Not allowed at the first position.*

            **EdgeSpec** — one of:

            - ``str``  — exact predicate, e.g. ``"biolink:affects"``
            - ``dict`` — edge metadata filter, e.g.
              ``{"knowledge_level": "knowledge_assertion"}`` or
              ``{"agent_type": "automated_agent"}`` (keys may be combined)
            - ``None`` — wildcard (any predicate)

        limit : int
            Maximum number of matching paths to return.  Only the final hop is
            capped at *limit*; intermediate frontiers use a larger safety bound
            so valid branches that reach an endpoint are not pruned early.
        node_subclassing : bool
            When ``True``, any string CURIE *NodeSpec* is expanded to include
            its ``subclass_of`` descendants.  Dict/``None`` NodeSpecs are
            unaffected.
        subclass_depth : int, optional
            How many subclass hops to follow when *node_subclassing* is set.
            ``None`` (the default here) follows the hierarchy transitively;
            ``1`` takes only direct children.

        Returns
        -------
        list[list[PathEdge]]
            Each element is one complete matching path: a list of
            ``(subject, predicate, object)`` tuples, one per hop.
            With ``return_stats=True``, a ``(paths, MatchStats)`` tuple; check
            ``stats.truncated`` before reading the result as exhaustive.

        Examples
        --------
        One-hop: any Gene neighbor of a specific drug (uses attached db)::

            graph.match_path([
                "CHEBI:6801",
                None,
                {"category": "biolink:Gene"},
            ])

        One-hop, curated edges only::

            graph.match_path([
                "CHEBI:6801",
                {"knowledge_level": "knowledge_assertion"},
                {"category": "biolink:Gene"},
            ])

        Two-hop: Drug → Gene → Disease (explicit db override)::

            graph.match_path([
                "CHEBI:6801",
                None,
                {"category": "biolink:Gene"},
                None,
                "MONDO:0005015",
            ], db=other_db)
        """
        if db is None:
            db = self._require_db()

        if len(path_spec) < 3 or len(path_spec) % 2 == 0:
            raise ValueError(
                f"path_spec length must be odd and >= 3, got {len(path_spec)}"
            )

        node_specs: List[NodeSpec] = path_spec[0::2]
        edge_specs: List[EdgeSpec] = path_spec[1::2]

        if hop_directions is None:
            hop_directions = [True] * len(edge_specs)
        elif len(hop_directions) != len(edge_specs):
            raise ValueError(
                f"hop_directions has {len(hop_directions)} entries for "
                f"{len(edge_specs)} hops"
            )
        # Only an all-forward walk may use the distance bound below, which is
        # computed over forward topology.  A symmetric hop (``None``) is not
        # forward, so it correctly disables the bound rather than pruning
        # branches that the reverse direction could still reach.
        all_forward = all(d is True for d in hop_directions)

        stats = MatchStats()

        def _done(paths: List[List[PathEdge]]):
            if stats.truncated:
                logger.warning(
                    "match_path truncated at hop(s) %s (caps %s, frontier sizes %s): "
                    "returning %d path(s), which is a subset of the matches. "
                    "Raise limit= for a complete result.",
                    stats.truncated_hops, stats.hop_caps, stats.frontier_sizes,
                    len(paths),
                )
            return (paths, stats) if return_stats else paths

        start_nodes = _resolve_node_candidates(
            node_specs[0], self, db, node_subclassing=node_subclassing,
            subclass_depth=subclass_depth,
        )
        if not start_nodes:
            return _done([])

        # frontier: list of (current_node_id, path_edges_so_far)
        frontier: List[tuple[str, List[PathEdge]]] = [(nid, []) for nid in start_nodes]

        # Capping an *intermediate* frontier at ``limit`` can prune the only
        # branches that reach a valid endpoint, silently dropping complete
        # paths.  So only the final hop is capped at ``limit``; intermediate
        # hops use a much larger safety bound that still prevents unbounded
        # combinatorial explosion.
        n_hops = len(edge_specs)
        intermediate_cap = max(limit * 50, 50_000)

        # ---- backward reachability pruning (both ends pinned) --------------
        # When the tail is a fixed node we know where the walk has to land, so
        # branches that cannot reach it in the hops that remain are dead and
        # should die *before* they cost a metadata round-trip.
        # Gated at 3 hops: shorter walks are already cheap enough that building
        # the reverse adjacency costs more than the pruning saves (measured
        # 0.26s of setup against a 0.00s 2-hop query).
        end_ids = _pinned_node_ids(
            node_specs[-1], self, node_subclassing=node_subclassing,
            subclass_depth=subclass_depth,
        )
        # The distance bound is computed over forward topology, so it is only
        # admissible when every hop is walked forward.
        reach_masks: Optional[List[np.ndarray]] = None
        if end_ids and n_hops >= 3 and all_forward:
            start_ids = [
                self.node_to_id[n] for n in start_nodes if n in self.node_to_id
            ]
            labels = self._component_labels
            if start_ids and not (
                {int(labels[i]) for i in start_ids}
                & {int(labels[i]) for i in end_ids}
            ):
                # No start shares a weakly-connected component with any end, so
                # no directed path can exist. O(1) after the cached labelling.
                return _done([])
            reach_masks = self._reach_masks(end_ids, n_hops - 1)

        for hop, (edge_spec, next_node_spec) in enumerate(
            zip(edge_specs, node_specs[1:])
        ):
            is_last_hop = hop == n_hops - 1
            hop_cap = limit if is_last_hop else intermediate_cap
            next_frontier: List[tuple[str, List[PathEdge]]] = []
            # True: walk subject -> object.  False: walk object -> subject, so the
            # neighbour found is the edge's *subject* and the frontier node is its
            # object.  Emitted PathEdges keep true orientation either way.
            forward = hop_directions[hop]
            # ``None`` means symmetric: walk both ways and union the results.
            # Candidates then carry their own direction, because one batch can
            # hold edges found each way.
            symmetric = forward is None

            def _edge(cur: str, pred: str, nbr: str, fwd: bool) -> PathEdge:
                """The edge as it exists in the graph, whichever way it was walked."""
                return (cur, pred, nbr) if fwd else (nbr, pred, cur)

            # Nodes that can still reach the pinned tail after this hop.  Passed
            # into expansion so unreachable neighbours are masked off the CSR row
            # rather than built into candidate tuples and discarded later.
            #
            # This matters most on the *final* hop, where ``remaining_hops`` is 0
            # and the mask collapses to ``distance == 0`` — the pinned tail set
            # itself.  That is the widest frontier, so masking there turns a
            # full expansion of every neighbour into just the ones that land on
            # the target.
            remaining_hops = n_hops - hop - 1
            reach_ok: Optional[np.ndarray] = (
                reach_masks[remaining_hops] if reach_masks is not None else None
            )

            def _flush(pending: List[tuple]) -> List[tuple]:
                """Metadata-filter one batch of candidate expansions.

                Uses at most one ``filter_edges`` + one ``filter_nodes`` call for
                the *whole* batch, however many distinct source nodes it spans.
                """
                if not pending:
                    return []

                if isinstance(edge_spec, dict):
                    survivors = _mp_filter_edges_batch(
                        db,
                        [_edge(cur, pred, nbr, fwd)
                         for cur, _p, nbr, pred, fwd in pending],
                        edge_spec,
                    )
                    pending = [
                        c for c in pending
                        if _edge(c[0], c[3], c[2], c[4]) in survivors
                    ]
                    if not pending:
                        return []

                allowed = _mp_filter_nodes_batch(
                    db,
                    [nbr for _c, _p, nbr, _pred, _f in pending],
                    next_node_spec,
                    graph=self,
                    node_subclassing=node_subclassing,
                    subclass_depth=subclass_depth,
                )
                if allowed is not None:
                    pending = [c for c in pending if c[2] in allowed]

                return [
                    (nbr, path_so_far + [_edge(cur, pred, nbr, fwd)])
                    for cur, path_so_far, nbr, pred, fwd in pending
                ]

            # Candidate expansions awaiting a batched metadata check, each
            # ``(current_node, path_so_far, nbr, pred)``.  Filtering a whole
            # batch in one backend call — rather than once per frontier node —
            # is what keeps a deep hop affordable: every backend pays a fixed
            # per-call cost (an Elasticsearch round-trip; an LMDB transaction
            # plus a full category-index scan), and calling per node multiplies
            # that cost by the frontier size, which at hop 3+ is the cap.
            batch: List[tuple] = []
            nodes = self.nodes
            node_to_id = self.node_to_id
            unfinished = False   # candidates or frontier entries left unexpanded

            # The frontier is expanded in chunks: one vectorized gather per
            # relation per chunk, rather than a Python call per frontier node.
            # Chunking bounds the transient arrays on a multi-million-node
            # frontier while keeping the gathers large enough to amortize.
            pos = 0
            while pos < len(frontier):
                chunk = frontier[pos : pos + _MP_EXPAND_CHUNK]
                pos += len(chunk)
                chunk_idxs = np.fromiter(
                    (node_to_id[c] for c, _p in chunk),
                    dtype=np.int64,
                    count=len(chunk),
                )
                # A symmetric hop expands both ways and unions the two; every
                # other hop walks the single direction it was given.
                walks = (True, False) if symmetric else (forward,)
                src_l: List[int] = []
                cols_l: List[int] = []
                preds_l: List[str] = []
                fwd_l: List[bool] = []
                seen_sym: set[tuple[int, int, str]] | None = (
                    set() if symmetric else None
                )
                for walk_fwd in walks:
                    src, cols, rels, labels = _mp_expand_frontier(
                        self, chunk_idxs, edge_spec, reach_ok, reverse=not walk_fwd
                    )
                    # Resolve the predicate label *now* rather than keeping the
                    # relation index: each direction returns its own ``labels``
                    # sequence, so an index kept from the forward walk would be
                    # read against the reverse walk's labels and mislabel the
                    # edge — which silently failed the edge-spec filter and cost
                    # 3,607 answers on two_hop_lookup.
                    #
                    # .tolist() once: indexing numpy scalars in the inner loop
                    # costs more than the conversion.
                    for s_i, c_i, r_i in zip(
                        src.tolist(), cols.tolist(), rels.tolist()
                    ):
                        pred = labels[r_i]
                        if seen_sym is not None:
                            # Both A->B and B->A can be stored under the same
                            # symmetric predicate.  They assert the same
                            # relationship, so emit one path, keeping the
                            # forward orientation because it is walked first.
                            key = (s_i, c_i, pred)
                            if key in seen_sym:
                                continue
                            seen_sym.add(key)
                        src_l.append(s_i)
                        cols_l.append(c_i)
                        preds_l.append(pred)
                        fwd_l.append(walk_fwd)

                # The flush threshold adapts to how many results this hop still
                # needs.  Always accumulating a full batch would trade per-node
                # call overhead for wasted expansion: a low ``limit`` satisfied
                # by the first few high-degree frontier nodes needs far fewer
                # candidates than ``_MP_FILTER_BATCH``, and expanding them
                # anyway costs more than the calls it saves.
                #
                # It only changes when ``next_frontier`` grows, i.e. after a
                # flush -- recomputing it per candidate cost three builtin calls
                # per candidate (4.5M each on a 2.2M-path query).
                flush_at = min(
                    _MP_FILTER_BATCH,
                    max(hop_cap - len(next_frontier), _MP_MIN_FLUSH),
                )
                batch_len = len(batch)
                n_cand = len(src_l)

                for k, (s, c, pred, f) in enumerate(
                    zip(src_l, cols_l, preds_l, fwd_l)
                ):
                    current_node, path_so_far = chunk[s]
                    batch.append((current_node, path_so_far, nodes[c], pred, f))

                    batch_len += 1

                    if batch_len >= flush_at:
                        next_frontier.extend(_flush(batch))
                        batch = []
                        batch_len = 0
                        if len(next_frontier) >= hop_cap:
                            unfinished = k + 1 < n_cand or pos < len(frontier)
                            break
                        flush_at = min(
                            _MP_FILTER_BATCH,
                            max(hop_cap - len(next_frontier), _MP_MIN_FLUSH),
                        )

                if len(next_frontier) >= hop_cap:
                    break

            if batch and len(next_frontier) < hop_cap:
                next_frontier.extend(_flush(batch))
                batch = []

            # This hop is incomplete if the cap stopped us before the frontier
            # was fully expanded, if a filtered batch was left unmerged, or if
            # survivors had to be sliced away below.
            if (
                unfinished
                or bool(batch)
                or len(next_frontier) > hop_cap
            ):
                stats.truncated = True
                stats.truncated_hops.append(hop)

            stats.hop_caps.append(hop_cap)
            frontier = next_frontier[:hop_cap]
            stats.frontier_sizes.append(len(frontier))
            if not frontier:
                return _done([])

        return _done([path for _, path in frontier[:limit]])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _open_tar(archive: Path, suffix: str) -> Tuple[tarfile.TarFile, List]:
        """Open a (possibly compressed) tar archive in streaming mode.

        Returns ``(tarfile, extra_closeables)``.  For the ``.tar.zst`` formats
        ``tarfile`` is wrapped around an externally-opened stream that it will
        **not** close itself, so the underlying file/reader objects are
        returned in *extra_closeables* for the caller to close.
        """
        if suffix.endswith(".tar.zst"):
            if _zstd is None:
                raise ImportError(
                    "Zstandard support is required for .tar.zst files. "
                    "Use Python >= 3.14 or: pip install zstandard"
                )
            if _BUILTIN_ZSTD:
                # compression.zstd.open returns a file-like object
                zf = _zstd.open(archive, "rb")
                try:
                    return tarfile.open(fileobj=zf, mode="r|"), [zf]
                except Exception:
                    zf.close()
                    raise
            else:
                # zstandard (third-party): use stream_reader
                import typing as _t

                fh = open(archive, "rb")
                try:
                    dctx: _t.Any = _zstd.ZstdDecompressor()
                    reader = dctx.stream_reader(fh)
                    return tarfile.open(fileobj=reader, mode="r|"), [reader, fh]
                except Exception:
                    fh.close()
                    raise
        elif suffix.endswith((".tar.gz", ".tgz")):
            return tarfile.open(archive, mode="r:gz"), []
        elif suffix.endswith(".tar"):
            return tarfile.open(archive, mode="r:"), []
        else:
            raise ValueError(
                f"Unsupported archive format: {suffix}. "
                "Expected .tar.zst, .tar.gz, or .tar"
            )


# ===================================================================
# match_path traversal helpers  (module-level; called by CSRGraph.match_path)
# ===================================================================

# Candidate expansions accumulated per hop before they are flushed through the
# metadata backend in one batched call.  10k matches the Elasticsearch
# backend's internal id-chunk size, so one flush costs about one round-trip
# there; for LMDB it amortises one transaction + category scan over the batch.
_MP_FILTER_BATCH = 10_000

# Floor for the adaptive flush threshold, so a nearly-satisfied hop still batches
# a useful number of candidates per call instead of degenerating towards one call
# per frontier node.
_MP_MIN_FLUSH = 256


# Frontier nodes expanded per vectorized gather.  Large enough that the numpy
# work dominates the per-chunk setup, small enough that the transient gathered
# arrays stay bounded when the frontier runs to millions of nodes.
_MP_EXPAND_CHUNK = 100_000


def _resolve_node_candidates(
    node_spec: NodeSpec,
    graph: CSRGraph,
    db: MetadataBackend,
    node_subclassing: bool = False,
    subclass_depth: Optional[int] = None,
) -> List[str]:
    """Return graph node IDs that satisfy *node_spec*.

    Raises ValueError if *node_spec* is ``None`` (wildcard not allowed at the
    first position of a path pattern).

    When *node_subclassing* is ``True`` and *node_spec* is a string CURIE, the
    returned list includes all subclass descendants of that CURIE.
    """
    if node_spec is None:
        raise ValueError(
            "The first NodeSpec in a path pattern cannot be None. "
            "Provide a node CURIE or a metadata filter dict."
        )
    if isinstance(node_spec, str):
        if node_spec not in graph.node_to_id:
            return []
        if node_subclassing:
            nid = graph.node_to_id[node_spec]
            return [graph.nodes[i] for i in graph._expand_subclasses(nid, subclass_depth)]
        return [node_spec]
    # dict filter
    if "id" in node_spec:
        nid = node_spec["id"]
        if nid not in graph.node_to_id:
            return []
        if node_subclassing:
            return [graph.nodes[i] for i in graph._expand_subclasses(graph.node_to_id[nid], subclass_depth)]
        return [nid]
    if "category" in node_spec:
        # Ask the backend which nodes carry the category, rather than handing it
        # every node in the graph and asking which ones survive.  The latter
        # builds a list of millions of CURIEs per call; backends with a category
        # index answer this directly.
        try:
            candidates = db.nodes_by_category(node_spec["category"])
        except NotImplementedError:
            candidates = [
                m["id"]
                for m in db.filter_nodes(
                    list(graph.node_to_id.keys()),
                    category=node_spec["category"],
                )
            ]
        node_to_id = graph.node_to_id
        return [c for c in candidates if c in node_to_id]
    return []


def _pinned_node_ids(
    node_spec: NodeSpec,
    graph: CSRGraph,
    node_subclassing: bool = False,
    subclass_depth: Optional[int] = None,
) -> frozenset[int]:
    """Node indices a *pinned* NodeSpec resolves to, or empty if it is not pinned.

    "Pinned" means the spec names specific nodes — a CURIE string or a dict with
    an ``id`` — as opposed to a category filter or a wildcard.  Mirrors how
    ``_mp_filter_nodes_batch`` resolves the same spec, including subclass
    widening, so a prune based on this set cannot disagree with the filter that
    ultimately accepts or rejects the node.
    """
    curie: Optional[str] = None
    if isinstance(node_spec, str):
        curie = node_spec
    elif isinstance(node_spec, dict) and "id" in node_spec:
        curie = node_spec["id"]
    if curie is None or curie not in graph.node_to_id:
        return frozenset()

    nid = graph.node_to_id[curie]
    if node_subclassing:
        return frozenset(graph._expand_subclasses(nid, subclass_depth))
    return frozenset({nid})


def _mp_expand_edges(
    graph: CSRGraph,
    node_id: str,
    edge_spec: EdgeSpec,
    reach_ok: Optional[np.ndarray] = None,
) -> List[tuple]:
    """Return ``(neighbor_id, predicate)`` pairs reachable from *node_id*.

    For exact-predicate EdgeSpecs uses only that relation's CSR matrix.
    For ``None``/dict specs iterates all per-predicate CSRs to preserve the
    exact predicate label in the output.

    *reach_ok*, when given, is a boolean array over node indices marking which
    neighbours can still reach the query's pinned tail within the hops that
    remain.  It is applied as a numpy mask on the raw CSR row, so unreachable
    neighbours are discarded before any Python tuple is built — the difference
    between filtering candidates and never creating them.
    """
    if node_id not in graph.node_to_id:
        return []
    u = graph.node_to_id[node_id]

    nodes = graph.nodes

    if isinstance(edge_spec, str):
        rel = _strip_biolink(edge_spec)
        csr = graph.csr_by_relation.get(rel)
        if csr is None:
            return []
        pred_label = _add_biolink(rel)
        start, end = int(csr.indptr[u]), int(csr.indptr[u + 1])
        if start == end:
            return []
        row = csr.indices[start:end]
        if reach_ok is not None:
            row = row[reach_ok[row]]
        return [(nodes[v], pred_label) for v in row.tolist()]

    # None or dict: visit every relation, since each edge must keep its own
    # predicate.  Most relations hold no row for a given node (mean degree ~17
    # spread over dozens of predicates), so the empty-row skip below elides the
    # large majority of the work.
    pairs: List[tuple] = []
    for indptr, indices, pred_label in graph._expansion_plan():
        start = indptr[u]
        end = indptr[u + 1]
        if start == end:
            continue
        row = indices[start:end]
        if reach_ok is not None:
            row = row[reach_ok[row]]
            if row.size == 0:
                continue
        pairs.extend((nodes[v], pred_label) for v in row.tolist())
    return pairs


def _mp_expand_frontier(
    graph: CSRGraph,
    node_idxs: np.ndarray,
    edge_spec: EdgeSpec,
    reach_ok: Optional[np.ndarray] = None,
    reverse: bool = False,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, tuple]:
    """Expand an entire frontier at once, vectorized.

    The per-node equivalent (:func:`_mp_expand_edges`) costs a Python call per
    frontier node, and for a wildcard EdgeSpec each of those calls walks all
    per-predicate CSR rows.  On a wide frontier that dominates everything else:
    profiling a 2.2M-path query put 77% of its runtime in 2.26M such calls.  Here
    each relation is gathered once for the whole frontier instead.

    Returns ``(src_pos, cols, rel_ids, labels)``:

    - ``src_pos`` — index into *node_idxs* each edge came from
    - ``cols`` — neighbour node index
    - ``rel_ids`` — index into *labels*
    - ``labels`` — tuple of ``biolink:``-prefixed predicate labels

    *reach_ok* is applied to the gathered columns, so unreachable neighbours are
    dropped inside numpy and never reach Python.  Output order matches the
    per-node path exactly: grouped by frontier node, then by relation, then CSR
    row order.
    """
    full_plan = (
        graph._reverse_expansion_plan() if reverse else graph._expansion_plan()
    )
    # A str or a collection of predicates restricts the plan, so the predicate
    # filter happens *during* traversal.  Filtering afterwards is wrong whenever a
    # cap is involved: the cap fills up with whatever predicates come first, and a
    # selective predicate can end up with nothing left.
    wanted: Optional[set] = None
    if isinstance(edge_spec, str):
        wanted = {_add_biolink(_strip_biolink(edge_spec))}
    elif isinstance(edge_spec, (list, tuple, set, frozenset)):
        # An empty collection means "unconstrained", matching TRAPI's reading of
        # an absent predicates list.
        if edge_spec:
            wanted = {_add_biolink(_strip_biolink(p)) for p in edge_spec}
    plan: list = (
        full_plan if wanted is None
        else [entry for entry in full_plan if entry[2] in wanted]
    )

    labels = tuple(p[2] for p in plan)
    src_parts: list = []
    col_parts: list = []
    rel_parts: list = []

    for rel_id, (indptr, indices, _label) in enumerate(plan):
        src_pos, cols = _csr_ragged_gather(indptr, indices, node_idxs)
        if cols.size == 0:
            continue
        if reach_ok is not None:
            keep = reach_ok[cols]
            if not keep.any():
                continue
            src_pos = src_pos[keep]
            cols = cols[keep]
        src_parts.append(src_pos)
        col_parts.append(cols)
        rel_parts.append(np.full(cols.size, rel_id, dtype=np.int32))

    if not src_parts:
        return _EMPTY_I64, _EMPTY_I64, _EMPTY_I32, labels

    src = np.concatenate(src_parts)
    cols_all = np.concatenate(col_parts)
    rels = np.concatenate(rel_parts)

    # Relations were gathered one after another, so results are relation-major.
    # A *stable* sort on the frontier position restores node-major order while
    # preserving relation order within each node -- byte-identical to what the
    # per-node loop emitted.
    order = np.argsort(src, kind="stable")
    return src[order], cols_all[order], rels[order], labels


def _mp_filter_edges_batch(
    db: MetadataBackend,
    triples: List[PathEdge],
    edge_spec: dict,
) -> set:
    """Return the ``(subject, predicate, object)`` triples matching *edge_spec*.

    One backend call for the whole batch, however many distinct subjects the
    triples span.  Both sides of the comparison carry ``biolink:``-prefixed
    predicates (``_mp_expand_edges`` emits them, and the backends' edge
    normalisers restore the prefix), so the returned keys line up with the
    caller's candidates.
    """
    if not triples:
        return set()
    matched = db.filter_edges(
        triples,
        knowledge_level=edge_spec.get("knowledge_level"),
        agent_type=edge_spec.get("agent_type"),
    )
    return {(e["subject"], e["predicate"], e["object"]) for e in matched}


def _mp_filter_nodes_batch(
    db: MetadataBackend,
    node_ids: List[str],
    node_spec: NodeSpec,
    graph: Optional[CSRGraph] = None,
    node_subclassing: bool = False,
    subclass_depth: Optional[int] = None,
) -> Optional[set]:
    """Return the subset of *node_ids* satisfying *node_spec*.

    Returns ``None`` for a wildcard (``None``) spec, meaning "everything
    passes" — letting the caller skip the membership test altogether rather
    than build a set it would only compare against itself.

    String CURIE specs are resolved in memory (optionally widened to subclass
    descendants when *node_subclassing* is set); only dict specs reach the
    backend, and then in a single batched call.
    """
    if node_spec is None:
        return None
    if isinstance(node_spec, str):
        if node_subclassing and graph is not None and node_spec in graph.node_to_id:
            valid_ids = graph._expand_subclasses(graph.node_to_id[node_spec], subclass_depth)
            return {graph.nodes[i] for i in valid_ids}
        return {node_spec}
    # dict filter — subclassing does not apply (category/id filter selects specific nodes)
    matched = db.filter_nodes(node_ids, category=node_spec.get("category"))
    return {m["id"] for m in matched}


# ===================================================================
# Generic CLI  (python csrgraph/csrgraph_kgx.py <kgx_file> [options])
# ===================================================================

DATA_DIR = Path("~/tmp/csrgraph_data").expanduser()


def _archive_stem(archive: Path) -> str:
    """Strip all archive suffixes, e.g. 'kg.tar.zst' → 'kg'."""
    name = archive.name
    for suffix in (".tar.zst", ".tar.gz", ".tar.bz2", ".tar"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return archive.stem


def _parse_metadata_arg(val: str) -> list[str] | None:
    """Convert a CLI metadata string to an internal field list.

    'none'  → None        (skip entirely)
    'all'   → ['all']     (store everything)
    'f1,f2' → ['f1','f2'] (specific extra fields only)
    """
    if val.lower() == "none":
        return None
    if val.lower() == "all":
        return ["all"]
    return [f.strip() for f in val.split(",") if f.strip()]


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from metadata_db import SQLiteMetadataBackend  # type: ignore[import]
    # LMDBMetadataBackend, ElasticsearchMetadataBackend, HybridMetadataBackend
    # are imported lazily below only when the corresponding --backend is selected,
    # so users without lmdb/elasticsearch installed can still use --backend sqlite.

    parser = argparse.ArgumentParser(
        description=(
            "Load a KGX archive into a CSRGraph + SQLite metadata DB, "
            "then print graph statistics and run demo queries."
        )
    )
    parser.add_argument(
        "kgx_file",
        help="Path to a .tar.zst / .tar.gz / .tar KGX archive.",
    )
    parser.add_argument(
        "--rebuild-graph",
        action="store_true",
        help="Force rebuild the CSRGraph cache even if it already exists.",
    )
    parser.add_argument(
        "--rebuild-db",
        action="store_true",
        help="Force rebuild the metadata DB even if it already exists.",
    )
    parser.add_argument(
        "--node-metadata",
        default="all",
        metavar="FIELDS",
        help=(
            "Node metadata fields to store: 'all' (default), 'none' to skip, "
            "or a comma-separated list of extra field names."
        ),
    )
    parser.add_argument(
        "--edge-metadata",
        default="all",
        metavar="FIELDS",
        help=(
            "Edge metadata fields to store: 'all' (default), 'none' to skip, "
            "or a comma-separated list of extra field names."
        ),
    )
    parser.add_argument(
        "--build-memmap",
        action="store_true",
        help=(
            "After loading/building the graph, write a companion <stem>.memmap/ "
            "directory of flat binary files for near-instant loading on production. "
            "Subsequent load() calls use this directory automatically."
        ),
    )
    parser.add_argument(
        "--memory-limit",
        metavar="LIMIT",
        default=None,
        help=(
            "RAM threshold that triggers automatic memmap building on first load. "
            "Accepts 'auto' (available RAM), a size string like '8g' / '16g', "
            "or a byte count. When the loaded graph exceeds this limit the "
            "<stem>.memmap/ directory is built once and reused on all future loads "
            "(near-instant). Typical production usage: --memory-limit auto."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["sqlite", "lmdb", "es", "hybrid"],
        default="sqlite",
        help=(
            "Metadata backend: 'sqlite' (default), 'lmdb', 'es' "
            "(Elasticsearch only), or 'hybrid' (LMDB + ES with auto routing). "
            "'es' builds/opens ES indices directly. 'hybrid' opens existing "
            "<stem>.metadata.lmdb and ES indices — build them separately first."
        ),
    )
    parser.add_argument(
        "--es-host",
        default="http://localhost:9200",
        metavar="URL",
        help="Elasticsearch host for 'es' or 'hybrid' backend (default: http://localhost:9200).",
    )
    parser.add_argument(
        "--node-threshold",
        type=int,
        default=2000,
        metavar="N",
        help=(
            "filter_nodes input size above which 'hybrid' auto mode routes to ES "
            "(default: 2000). Tune based on deployment benchmarks."
        ),
    )
    parser.add_argument(
        "--edge-threshold",
        type=int,
        default=None,
        metavar="N",
        help=(
            "filter_edges input size above which 'hybrid' auto mode routes to ES. "
            "Default: None (always use LMDB — ES is slower for edges at all tested sizes). "
            "Set an explicit value once deployment benchmarks show a cross-over."
        ),
    )
    args = parser.parse_args()

    archive = Path(args.kgx_file).expanduser()
    if not archive.exists():
        print(f"Archive not found: {archive}")
        raise SystemExit(1)

    stem = _archive_stem(archive)
    cache_path = archive.parent / f"{stem}.csrgraph.pkl.zst"

    # Backend-specific path and class
    if args.backend == "lmdb":
        from metadata_db import LMDBMetadataBackend  # type: ignore[import]
        _BackendCls = LMDBMetadataBackend
        db_path = archive.parent / f"{stem}.metadata.lmdb"
    elif args.backend == "es":
        _BackendCls = None  # ES is handled separately in section 2
        db_path = None      # ES has no local file path
    elif args.backend == "hybrid":
        _BackendCls = None  # hybrid is assembled in section 2, not via _BackendCls
        db_path = archive.parent / f"{stem}.metadata.lmdb"  # primary build path
    else:
        _BackendCls = SQLiteMetadataBackend
        db_path = archive.parent / f"{stem}.metadata.db"

    if args.memory_limit:
        CSRGraph.set_memory_limit(args.memory_limit)
        print(f"Memory limit: {_fmt_bytes(CSRGraph._memory_limit)}\n")  # type: ignore[arg-type]

    node_fields = _parse_metadata_arg(args.node_metadata)
    edge_fields = _parse_metadata_arg(args.edge_metadata)
    use_db = node_fields is not None or edge_fields is not None

    # ------------------------------------------------------------------
    # 1. Load or build CSRGraph (topology-only)
    # ------------------------------------------------------------------
    if cache_path.exists() and not args.rebuild_graph:
        print("=" * 70)
        print(f"Loading graph from cache: {cache_path.name}")
        print("=" * 70)
        t0 = time.time()
        graph = CSRGraph.load(str(cache_path))
        print(f"Load time: {time.time() - t0:.2f}s\n")
    else:
        print("=" * 70)
        print(f"Loading graph from archive: {archive.name}")
        print(f"  archive size: {archive.stat().st_size / 1024**3:.2f} GB")
        print("=" * 70)
        t0 = time.time()
        graph = CSRGraph.from_kgx_archive(str(archive))  # topology-only
        load_time = time.time() - t0
        print(f"Load time: {load_time:.2f}s\n")
        print(f"Saving graph cache: {cache_path.name} ...")
        graph.save(str(cache_path))
        print()

    # ------------------------------------------------------------------
    # 1b. Build memmap directory (optional; for fast production loading)
    # ------------------------------------------------------------------
    if args.build_memmap:
        mmap_dir = CSRGraph._memmap_dir(str(cache_path))
        if mmap_dir.exists() and (mmap_dir / "meta.json").exists():
            print(f"Memmap already exists: {mmap_dir.name}  (skipping; use --rebuild-graph to force)\n")
        else:
            print("=" * 70)
            print(f"Building memmap directory: {mmap_dir.name}")
            print("=" * 70)
            t0 = time.time()
            graph._to_memmap(mmap_dir)
            mmap_bytes = sum(f.stat().st_size for f in mmap_dir.iterdir())
            print(
                f"Done: {mmap_bytes / 1024**3:.2f} GB on disk, "
                f"{time.time() - t0:.1f}s\n"
            )

    # ------------------------------------------------------------------
    # 2. Load or build metadata DB
    # ------------------------------------------------------------------
    db: MetadataBackend | None = None
    if use_db:
        if args.backend == "es":
            from metadata_db import ElasticsearchMetadataBackend as _ES  # type: ignore[import]
            if args.rebuild_db:
                print("=" * 70)
                print(f"Building ES indices from archive  (host: {args.es_host}, prefix: {stem})")
                print("=" * 70)
                t0 = time.time()
                db = _ES.build(
                    str(archive),
                    host=args.es_host,
                    index_prefix=stem,
                    node_metadata_fields=node_fields,
                    edge_metadata_fields=edge_fields,
                )
                print(f"Build time: {time.time() - t0:.1f}s\n")
            else:
                print("=" * 70)
                print(f"Opening ES backend  (host: {args.es_host}, prefix: {stem})")
                print("=" * 70)
                db = _ES(host=args.es_host, index_prefix=stem)
                print(f"  ES connected: {args.es_host}\n")
        elif args.backend == "hybrid":
            # Hybrid: LMDB must exist (build it separately with --backend lmdb).
            # ES is optional; we try to connect but continue without it if down.
            if not db_path.exists():
                print(
                    f"ERROR: hybrid backend requires an existing LMDB store at "
                    f"{db_path}. Build it first with --backend lmdb.\n"
                )
                raise SystemExit(1)
            from metadata_db import (  # type: ignore[import]
                LMDBMetadataBackend as _LMDB,
                ElasticsearchMetadataBackend as _ES,
                HybridMetadataBackend as _Hybrid,
            )
            print("=" * 70)
            print(f"Opening hybrid backend  (LMDB + ES @ {args.es_host})")
            print("=" * 70)
            lmdb_be = _LMDB(str(db_path))
            try:
                es_be = _ES(host=args.es_host, index_prefix=stem)
                print(f"  ES connected: {args.es_host}")
            except Exception as exc:
                print(f"  ES unavailable ({exc}); falling back to LMDB-only")
                es_be = None
            db = _Hybrid(
                lmdb=lmdb_be,
                es=es_be,
                mode="auto" if es_be is not None else "lmdb",
                node_threshold=args.node_threshold,
                edge_threshold=args.edge_threshold,
            )
            lmdb_size_mb = sum(f.stat().st_size for f in db_path.iterdir()) / 1024**2
            edge_thr_str = str(args.edge_threshold) if args.edge_threshold is not None else "None (always LMDB)"
            print(f"  LMDB: {lmdb_size_mb:.0f} MB  node_threshold: {args.node_threshold:,}  edge_threshold: {edge_thr_str}\n")
        elif _BackendCls is not None and db_path.exists() and not args.rebuild_db:
            print("=" * 70)
            print(f"Opening metadata DB: {db_path.name}  (backend: {args.backend})")
            print("=" * 70)
            db = _BackendCls(str(db_path))
            # Size: file for SQLite, directory total for LMDB
            if db_path.is_dir():
                db_size_mb = sum(f.stat().st_size for f in db_path.iterdir()) / 1024**2
            else:
                db_size_mb = db_path.stat().st_size / 1024**2
            print(f"  {db_size_mb:.0f} MB on disk\n")
        elif _BackendCls is not None:
            print("=" * 70)
            print(f"Building metadata DB from archive  (backend: {args.backend}) ...")
            print("=" * 70)
            t0 = time.time()
            db = _BackendCls.build(
                str(archive),
                str(db_path),
                node_metadata_fields=node_fields,
                edge_metadata_fields=edge_fields,
            )
            print(f"Build time: {time.time() - t0:.1f}s\n")

    # ------------------------------------------------------------------
    # 3. Graph statistics
    # ------------------------------------------------------------------
    print("=" * 70)
    print("Graph statistics")
    print("=" * 70)
    print(f"Nodes:      {graph.num_nodes:,}")
    print(f"Edges:      {graph.edge_count:,}")
    print(f"Predicates: {len(graph.relations)}")
    print()

    print("Edge counts by predicate:")
    for pred, count in sorted(
        graph.predicate_counts.items(), key=lambda x: x[1], reverse=True
    ):
        print(f"  {_add_biolink(pred):55s} {count:>10,}")
    print()

    if db is not None and node_fields is not None:
        print("Top 20 node categories (from metadata DB):")
        if hasattr(db, '_con'):  # duck-type check for SQLiteMetadataBackend
            for row in db._con.execute(
                "SELECT category, COUNT(*) AS cnt FROM node_categories "
                "GROUP BY category ORDER BY cnt DESC LIMIT 20"
            ):
                print(f"  {_add_biolink(row['category']):55s} {row['cnt']:>10,}")
        else:
            # Non-SQLite backends: tally categories from a sample of graph nodes
            sample = graph.nodes[:5000]
            cat_counts: dict[str, int] = {}
            for meta in db.filter_nodes(sample):
                for cat in (meta.get("category") or []):
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
            for cat, cnt in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)[:20]:
                print(f"  {_add_biolink(cat):55s} {cnt:>10,}  (sample of 5000 nodes)")
        print()

    # ------------------------------------------------------------------
    # 4. Helper: resolve display name
    # ------------------------------------------------------------------
    def _node_name(nid: str) -> str:
        if db is not None:
            meta = db.get_node(nid)
            name = meta.get("name", "")
            if name:
                return name
        return nid

    def _sample_node_by_cat(category: str) -> str | None:
        if db is None:
            return None
        if hasattr(db, '_con'):  # duck-type check for SQLiteMetadataBackend
            for row in db._con.execute(
                "SELECT node_id FROM node_categories WHERE category=? LIMIT 200",
                (_strip_biolink(category),),
            ):
                nid = row["node_id"]
                if nid in graph.node_to_id:
                    return nid
        else:
            sample = graph.nodes[:5000]
            matched = db.filter_nodes(sample, category=_add_biolink(category))
            for m in matched:
                if m["id"] in graph.node_to_id:
                    return m["id"]
        return None

    # ------------------------------------------------------------------
    # 5. Demo queries
    # ------------------------------------------------------------------
    print("=" * 70)
    print("Demo queries")
    print("=" * 70)

    # Pick sample nodes via DB categories if available; else first few graph nodes
    sample_gene = _sample_node_by_cat("Gene")
    sample_disease = _sample_node_by_cat("Disease")
    sample_drug = _sample_node_by_cat("Drug") or _sample_node_by_cat("SmallMolecule")

    # Fallback: if DB unavailable or category not found, use first N graph nodes
    all_nodes = list(graph.node_to_id.keys())
    if sample_gene is None and len(all_nodes) > 0:
        sample_gene = all_nodes[0]
    if sample_disease is None and len(all_nodes) > 1:
        sample_disease = all_nodes[1]
    if sample_drug is None and len(all_nodes) > 2:
        sample_drug = all_nodes[2]

    # Neighbors
    for label, nid in [
        ("Gene", sample_gene),
        ("Disease", sample_disease),
        ("Drug/SmallMolecule", sample_drug),
    ]:
        if not nid:
            continue
        name = _node_name(nid)
        nbrs = graph.neighbors(nid)
        print(f"\nSample {label}: {nid} ({name})")
        print(f"  Total neighbors: {len(nbrs)}")
        for n in nbrs[:5]:
            print(f"    -> {n} ({_node_name(n)})")
        if len(nbrs) > 5:
            print(f"    ... and {len(nbrs) - 5} more")

    # Shortest path
    sp = None
    if sample_gene and sample_disease:
        gene_name = _node_name(sample_gene)
        disease_name = _node_name(sample_disease)
        print(f"\nShortest path: {gene_name} → {disease_name}")
        t0 = time.time()
        sp = graph.shortest_path(sample_gene, sample_disease)
        sp_time = time.time() - t0
        if sp:
            for src, pred, tgt in sp:
                print(f"  {_node_name(src):30s} --[{pred}]--> {_node_name(tgt)}")
        else:
            print("  No path found")
        print(f"  (query time: {sp_time:.3f}s)")

    # DB-backed demos (only when DB is available)
    if db is not None:
        print()
        print("=" * 70)
        print("Metadata DB demos")
        print("=" * 70)

        # filter_nodes: keep only Gene neighbors
        if sample_drug:
            drug_name = _node_name(sample_drug)
            nbrs = graph.neighbors(sample_drug)
            print(f"\nNeighbors of {sample_drug} ({drug_name}): {len(nbrs)} total")
            if node_fields is not None:
                t0 = time.time()
                gene_nbrs = db.filter_nodes(nbrs, category="biolink:Gene")
                ft = time.time() - t0
                print(
                    f"  Gene neighbors ({len(gene_nbrs)} of {len(nbrs)}, {ft * 1000:.1f}ms):"
                )
                for n in gene_nbrs[:5]:
                    print(f"    {n['id']:20s}  {n.get('name', '')}")
                if len(gene_nbrs) > 5:
                    print(f"    ... and {len(gene_nbrs) - 5} more")
            else:
                print("  (node category filtering skipped — node_metadata=none)")

        # filter_edges: curated path edges
        if sample_gene and sample_disease and sp and edge_fields is not None:
            print("\nPath edges filtered by knowledge_level=knowledge_assertion:")
            t0 = time.time()
            curated = db.filter_edges(sp, knowledge_level="knowledge_assertion")
            ft = time.time() - t0
            print(f"  {len(curated)} of {len(sp)} path edges match ({ft * 1000:.1f}ms)")
            for e in curated:
                print(f"    {e['subject']:25s} --[{e['predicate']}]-->")
                print(
                    f"    {e['object']:25s}  kl={e.get('knowledge_level')}  at={e.get('agent_type')}"
                )

        # get_node: full metadata for a single node
        if sample_gene and node_fields is not None:
            print(f"\nFull metadata for {sample_gene}:")
            meta = db.get_node(sample_gene)
            for k, v in meta.items():
                if k == "category":
                    cats = v if isinstance(v, list) else [v]
                    print(f"  category: {cats[:3]}{'...' if len(cats) > 3 else ''}")
                else:
                    val_str = str(v)
                    print(f"  {k}: {val_str[:80]}{'...' if len(val_str) > 80 else ''}")

        # match_path: two-hop pattern demo
        if sample_drug and node_fields is not None:
            print("\nmatch_path demo: Drug → any → Gene → any → Disease (limit 3):")
            t0 = time.time()
            paths = graph.match_path(
                [
                    sample_drug,
                    None,
                    {"category": "Gene"},
                    None,
                    {"category": "Disease"},
                ],
                limit=3,
                db=db,
            )
            mt = time.time() - t0
            print(f"  Found {len(paths)} path(s) ({mt * 1000:.1f}ms):")
            for path in paths:
                for src, pred, tgt in path:
                    print(f"    {_node_name(src):30s} --[{pred}]--> {_node_name(tgt)}")
                print()

        db.close()

    print("=" * 70)
    print("Done.")
