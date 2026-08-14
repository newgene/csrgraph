"""Metadata storage backends for CSRGraph nodes and edges.

Provides an abstract :class:`MetadataBackend` interface and four concrete
implementations — each totally independent of :mod:`csrgraph_kgx`:

* :class:`SQLiteMetadataBackend`         — embedded SQLite (zero extra deps)
* :class:`DuckDBMetadataBackend`         — embedded DuckDB  (``pip install duckdb``)
* :class:`LMDBMetadataBackend`           — LMDB key-value   (``pip install lmdb``)
* :class:`ElasticsearchMetadataBackend`  — ES server        (``pip install elasticsearch``)

All path-traversal logic lives in :mod:`csrgraph_kgx`.  These backends handle
only metadata storage and retrieval — they accept lists of node CURIEs or
``PathEdge`` tuples and return enriched metadata dicts.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import sqlite3
import tarfile
import threading
import time
import warnings
import zlib
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Zstandard — mirrored from csrgraph_kgx.py to avoid a circular import.
# ---------------------------------------------------------------------------
_BUILTIN_ZSTD = False
try:
    import compression.zstd as _zstd   # Python >= 3.14  # type: ignore[import]
    _BUILTIN_ZSTD = True
except ImportError:
    try:
        import zstandard as _zstd       # type: ignore[no-redef]
    except ImportError:
        _zstd = None                     # type: ignore[assignment]

# Reusable decompressor — avoids creating a new object on every blob read.
# Only needed for the third-party `zstandard` package; the built-in module
# exposes a stateless `decompress()` function.
_ZSTD_DCTX = (
    _zstd.ZstdDecompressor()   # type: ignore[union-attr]
    if (_zstd is not None and not _BUILTIN_ZSTD)
    else None
)

# ---------------------------------------------------------------------------
# biolink: prefix helpers (duplicated from csrgraph_kgx.py)
# ---------------------------------------------------------------------------
_BIOLINK_PREFIX = "biolink:"


def _add_biolink(s: str) -> str:
    # Only prefix bare values; values already carrying a namespace (biolink:
    # or another CURIE prefix such as rdfs:subClassOf) round-trip unchanged,
    # so non-biolink predicates/categories aren't corrupted into
    # "biolink:rdfs:subClassOf".
    return s if ":" in s else _BIOLINK_PREFIX + s


#: On-disk format of the metadata stores this code can read.
#:
#: Bump whenever a key layout changes.  Version 2 keys edge metadata on
#: ``(subject, predicate, object, qualifier_fingerprint)``; version 1 omitted the
#: fingerprint.  The distinction is not cosmetic and not gracefully degradable: a
#: version-1 store read by version-2 code matches nothing, because the
#: 4-component prefix scan never matches a 3-component key.  ``get_edge_variants``
#: then returns ``[]``, every qualifier-constrained query answers nothing, and no
#: error is raised — so a release must record this and the server must refuse to
#: serve on a mismatch rather than answer with silent emptiness.
STORE_FORMAT_VERSION = 2


def qualifier_fingerprint(edge_meta: dict) -> str:
    """Short, content-derived discriminator for one edge's qualifier set.

    Edge metadata is keyed by ``(subject, predicate, object)`` plus this, so that
    the same triple asserted with different qualifiers is stored as separate
    records rather than one overwriting the other.

    Two properties matter:

    * **Content-derived, not positional.** The value depends only on the qualifiers,
      so it is identical across rebuilds regardless of input order — which an
      ordinal variant index would not be.
    * **Empty for unqualified edges.** Most triples carry a single variant and no
      qualifiers at all, so those keep a one-byte suffix instead of a hash,
      keeping the key overhead negligible.

    Measured on the 2026-07-19 archive (``probes/verify_variants.py``):

    ==========================================  ==============
    raw edge records                             28,925,258
    distinct ``(s, p, o)``                       28,105,517
    distinct ``(s, p, o, fingerprint)``          28,860,305
    ==========================================  ==============

    So keying on the triple alone was dropping **754,788 assertions**, and the
    qualifier set recovers all but 64,953 of the duplicate records — those remain
    collapsed because they are indistinguishable in their qualifiers.  Variants
    per triple: 98.18% have exactly one, the mean is 1.0269, and the maximum is
    **128**, which is the number any per-triple fetch bound has to clear.
    """
    quals = {k: v for k, v in edge_meta.items() if "qualifier" in k}
    if not quals:
        return ""
    canonical = json.dumps(quals, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canonical.encode()).hexdigest()[:10]


def _strip_biolink(s: str) -> str:
    return s[len(_BIOLINK_PREFIX):] if s.startswith(_BIOLINK_PREFIX) else s


# ---------------------------------------------------------------------------
# Public type alias
# ---------------------------------------------------------------------------
PathEdge = tuple[str, Optional[str], str]   # (subject, predicate_or_None, object)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------
# Fields stored in dedicated columns; everything else → compressed extra blob.
_NODE_CORE  = {"_id", "id", "name", "category"}
_EDGE_CORE  = {"_id", "id", "subject", "predicate", "object", "knowledge_level", "agent_type"}
_BATCH_SIZE = 10_000

# Extra metadata fields to promote to dedicated indexed columns at build time.
# Fields not listed here are stored in the compressed extra blob.
# Adjust via indexed_extra_node_fields / indexed_extra_edge_fields in build().
DEFAULT_INDEXED_NODE_FIELDS: list[str] = []
DEFAULT_INDEXED_EDGE_FIELDS: list[str] = [
    "qualified_predicate",
    "object_aspect_qualifier",
    "object_direction_qualifier",
    "causal_mechanism_qualifier",
    "negated",
    "clinical_approval_status",
    "frequency_qualifier",
    "disease_context_qualifier",
    "species_context_qualifier",
]

# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------

def _compress_blob(obj: dict) -> bytes | None:
    """JSON-encode and zstd/zlib-compress *obj*.  Returns ``None`` for empty dicts."""
    if not obj:
        return None
    data = json.dumps(obj, separators=(",", ":")).encode()
    if _BUILTIN_ZSTD:
        return _zstd.compress(data, level=3)   # type: ignore[union-attr]
    if _zstd is not None:
        return _zstd.ZstdCompressor(level=3).compress(data)   # type: ignore[union-attr]
    return zlib.compress(data, 6)


def _decompress_blob(value: bytes | str | None) -> dict:
    """Decompress/decode a blob written by :func:`_compress_blob`."""
    if value is None:
        return {}
    if isinstance(value, bytes):
        try:
            if _BUILTIN_ZSTD:
                return json.loads(_zstd.decompress(value))   # type: ignore[union-attr]
            if _ZSTD_DCTX is not None:
                return json.loads(_ZSTD_DCTX.decompress(value))
            return json.loads(zlib.decompress(value))
        except Exception:
            try:
                return json.loads(zlib.decompress(value))   # legacy zlib fallback
            except Exception:
                # Neither codec could decode the blob: this indicates genuine
                # corruption/truncation, not an empty record.  Warn instead of
                # silently masking it as "no metadata".
                warnings.warn(
                    "Failed to decompress/decode a metadata blob; "
                    "returning empty dict. The store may be corrupt.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                return {}
    return json.loads(value)   # legacy uncompressed TEXT column

# ---------------------------------------------------------------------------
# KGX archive streaming (independent of CSRGraph)
# ---------------------------------------------------------------------------

def _open_tar_archive(archive: Path, suffix: str) -> tarfile.TarFile:
    """Open a (possibly zstd/gz-compressed) tar archive for streaming."""
    if suffix.endswith(".tar.zst"):
        if _zstd is None:
            raise ImportError(
                "Zstandard support required for .tar.zst files. "
                "Use Python >= 3.14 or: pip install zstandard"
            )
        if _BUILTIN_ZSTD:
            zf = _zstd.open(archive, "rb")   # type: ignore[union-attr]
            return tarfile.open(fileobj=zf, mode="r|")
        else:
            import typing as _t
            fh = open(archive, "rb")
            dctx: _t.Any = _zstd.ZstdDecompressor()   # type: ignore[union-attr]
            return tarfile.open(fileobj=dctx.stream_reader(fh), mode="r|")
    elif suffix.endswith((".tar.gz", ".tgz")):
        return tarfile.open(archive, mode="r:gz")
    elif suffix.endswith(".tar"):
        return tarfile.open(archive, mode="r:")
    raise ValueError(f"Unsupported archive format: {suffix}")


def _stream_kgx(archive_path: str):
    """Yield ``('node', raw_dict)`` and ``('edge', raw_dict)`` from a KGX archive.

    Streams line-by-line; the full JSONL is never held in memory at once.
    Records missing required IDs are skipped.
    """
    archive = Path(archive_path)
    suffix  = "".join(archive.suffixes)
    with _open_tar_archive(archive, suffix) as tar:
        for member in tar:
            basename = Path(member.name).name
            fobj     = tar.extractfile(member)
            if fobj is None:
                continue
            if basename == "nodes.jsonl":
                for raw in fobj:
                    line = raw.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("id"):
                        yield "node", rec
            elif basename == "edges.jsonl":
                for raw in fobj:
                    line = raw.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("subject") and rec.get("predicate") and rec.get("object"):
                        yield "edge", rec


# ===========================================================================
# Abstract base class
# ===========================================================================

class MetadataBackend(abc.ABC):
    """Abstract base class for all CSRGraph metadata storage backends.

    Implementations handle *only* metadata storage and retrieval.
    Graph-topology traversal lives in :class:`csrgraph_kgx.CSRGraph`.

    All predicates returned by any method include the ``biolink:`` prefix;
    input predicates are accepted with or without the prefix.
    """

    @abc.abstractmethod
    def get_node(self, node_id: str) -> dict:
        """Return full metadata for *node_id*, or ``{}`` if not found."""

    @abc.abstractmethod
    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        """Return full metadata for the edge, or ``{}`` if not found.

        *predicate* may include or omit the ``biolink:`` prefix.
        """

    @abc.abstractmethod
    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        """Return metadata for the subset of *node_ids* matching the filter.

        Parameters
        ----------
        node_ids:
            Node CURIEs from a graph query result.
        category:
            Biolink category with or without ``biolink:`` prefix.
        extra_filters:
            Additional key/value filters on node metadata fields.
            Indexed fields use SQL WHERE; others use Python-side filtering.
        """

    @abc.abstractmethod
    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        """Return metadata for the subset of *edges* matching the filters.

        Parameters
        ----------
        edges:
            ``(subject, predicate, object)`` tuples from a graph query.
            *predicate* may be ``None`` to match any predicate between
            that subject/object pair.
        knowledge_level:
            e.g. ``'knowledge_assertion'``.
        agent_type:
            e.g. ``'automated_agent'``.
        extra_filters:
            Additional key/value filters on edge metadata fields.
            Indexed fields use SQL WHERE; others use Python-side filtering.
        """

    def get_edge_variants(
        self,
        subject: str,
        predicate: str,
        obj: str,
    ) -> list[dict]:
        """Every stored edge for this triple, one per distinct qualifier set.

        A KGX archive routinely asserts the same ``(subject, predicate, object)``
        more than once with *different* qualifiers — e.g. one record saying a
        chemical decreases a gene's abundance and another saying it increases it.
        Keying edge metadata on the triple alone keeps only the last of those, so a
        query constraining qualifiers silently misses answers.  This returns all of
        them; a constraint matches when **any** variant satisfies it.

        The default returns at most one edge, which is correct for backends that do
        not store variants separately (SQLite, DuckDB).  LMDB and Elasticsearch
        override it.
        """
        edge = self.get_edge(subject, predicate, obj)
        return [edge] if edge else []

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Return the CURIEs of every node in *category*.

        Answers "which nodes are Genes?" without the caller having to enumerate
        candidates first.  ``filter_nodes`` narrows a list the caller already
        has; this *produces* the list, which is what a category-only query
        needs.

        Backends with a category index should override this.  The default
        implementation raises :class:`NotImplementedError` so callers can detect
        the absence and fall back, rather than silently doing something
        expensive.

        Parameters
        ----------
        category:
            Biolink category, with or without the ``biolink:`` prefix.
        limit:
            Stop after this many CURIEs when given.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no category index; "
            "callers should fall back to filter_nodes()"
        )

    @abc.abstractmethod
    def close(self) -> None:
        """Release any open handles or connections."""

    def __enter__(self) -> MetadataBackend:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ===========================================================================
# SQLite backend
# ===========================================================================

def _make_sqlite_schema(
    extra_node_fields: list[str],
    extra_edge_fields: list[str],
) -> str:
    """Generate the SQLite schema DDL for the given extra indexed columns."""
    node_extra_cols = "".join(f"    {f} TEXT,\n" for f in extra_node_fields)
    edge_extra_cols = "".join(f"    {f} TEXT,\n" for f in extra_edge_fields)
    node_extra_idx  = "".join(
        f"CREATE INDEX IF NOT EXISTS idx_node_{f} ON nodes({f});\n"
        for f in extra_node_fields
    )
    edge_extra_idx  = "".join(
        f"CREATE INDEX IF NOT EXISTS idx_edge_{f} ON edges({f});\n"
        for f in extra_edge_fields
    )
    return f"""
PRAGMA page_size    = 8192;
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS nodes (
    id    TEXT PRIMARY KEY,
    name  TEXT,
{node_extra_cols}    extra BLOB
);
CREATE TABLE IF NOT EXISTS node_categories (
    node_id  TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (node_id, category)
);
CREATE INDEX IF NOT EXISTS idx_nc_category ON node_categories(category);
CREATE INDEX IF NOT EXISTS idx_nc_node     ON node_categories(node_id);
{node_extra_idx}
CREATE TABLE IF NOT EXISTS edges (
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    knowledge_level TEXT,
    agent_type      TEXT,
{edge_extra_cols}    extra           BLOB,
    PRIMARY KEY (subject, predicate, object)
);
CREATE INDEX IF NOT EXISTS idx_edge_kl ON edges(knowledge_level);
CREATE INDEX IF NOT EXISTS idx_edge_at ON edges(agent_type);
{edge_extra_idx}"""


class SQLiteMetadataBackend(MetadataBackend):
    """SQLite-backed metadata store.

    Zero extra dependencies.  Stores per-node categories in a normalised table
    for fast ``WHERE category = ?`` queries; all other extra fields are stored
    in zstd/zlib-compressed JSON blobs.

    Build once::

        db = SQLiteMetadataBackend.build("kg.tar.zst", "kg.metadata.db")

    Open an existing DB::

        db = SQLiteMetadataBackend("kg.metadata.db")
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # SQLite connections are not safe to share across threads.  Hand each
        # thread its own connection (lazily) so a threaded server (e.g. the
        # TRAPI server) can query concurrently without "recursive use of
        # cursors" errors or interleaved results.
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._indexed_node_fields: list[str] = []
        self._indexed_edge_fields: list[str] = []
        try:
            con = self._conn()
            row = con.execute(
                "SELECT value FROM _meta WHERE key='indexed_node_fields'"
            ).fetchone()
            if row:
                self._indexed_node_fields = json.loads(row[0])
            row = con.execute(
                "SELECT value FROM _meta WHERE key='indexed_edge_fields'"
            ).fetchone()
            if row:
                self._indexed_edge_fields = json.loads(row[0])
        except sqlite3.OperationalError:
            # Fresh/partly-built DB without the _meta table — leave the
            # indexed-field lists empty.  Other errors propagate.
            pass

    def _conn(self) -> sqlite3.Connection:
        """Return this thread's SQLite connection, opening one if needed."""
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.db_path, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
            with self._conns_lock:
                self._all_conns.append(con)
        return con

    def close(self) -> None:
        with self._conns_lock:
            for con in self._all_conns:
                try:
                    con.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._local = threading.local()

    # -- Build ---------------------------------------------------------------

    @classmethod
    def build(
        cls,
        archive_path: str,
        db_path: str,
        *,
        node_metadata_fields: list[str] | None = None,
        edge_metadata_fields: list[str] | None = None,
        indexed_extra_node_fields: list[str] | None = None,
        indexed_extra_edge_fields: list[str] | None = None,
    ) -> SQLiteMetadataBackend:
        """Build (or rebuild) the metadata DB from a KGX archive.

        Parameters
        ----------
        archive_path:
            Path to the ``.tar.zst`` / ``.tar.gz`` / ``.tar`` KGX archive.
        db_path:
            Destination SQLite file path.
        node_metadata_fields:
            ``None`` = skip nodes entirely; ``["all"]`` = all extra fields;
            ``["f1","f2"]`` = only those extra fields (id/name/category always stored).
        edge_metadata_fields:
            Same semantics as *node_metadata_fields* for edges.
        indexed_extra_node_fields:
            Extra node fields to promote to dedicated indexed TEXT columns.
            Defaults to :data:`DEFAULT_INDEXED_NODE_FIELDS`.
        indexed_extra_edge_fields:
            Extra edge fields to promote to dedicated indexed TEXT columns.
            Defaults to :data:`DEFAULT_INDEXED_EDGE_FIELDS`.
        """
        if indexed_extra_node_fields is None:
            indexed_extra_node_fields = list(DEFAULT_INDEXED_NODE_FIELDS)
        if indexed_extra_edge_fields is None:
            indexed_extra_edge_fields = list(DEFAULT_INDEXED_EDGE_FIELDS)

        inodes: list[str] = indexed_extra_node_fields
        iedges: list[str] = indexed_extra_edge_fields

        if Path(db_path).exists():
            os.remove(db_path)

        con = sqlite3.connect(db_path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.executescript(_make_sqlite_schema(inodes, iedges))

        # Write indexed field lists to _meta
        con.execute(
            "INSERT OR REPLACE INTO _meta VALUES (?,?)",
            ("indexed_node_fields", json.dumps(inodes)),
        )
        con.execute(
            "INSERT OR REPLACE INTO _meta VALUES (?,?)",
            ("indexed_edge_fields", json.dumps(iedges)),
        )

        load_nodes    = node_metadata_fields is not None
        keep_all_node = node_metadata_fields == ["all"] if load_nodes else False
        node_xkeys: set[str] | None = (
            None if keep_all_node
            else set(node_metadata_fields) if load_nodes else None  # type: ignore[arg-type]
        )
        load_edges    = edge_metadata_fields is not None
        keep_all_edge = edge_metadata_fields == ["all"] if load_edges else False
        edge_xkeys: set[str] | None = (
            None if keep_all_edge
            else set(edge_metadata_fields) if load_edges else None  # type: ignore[arg-type]
        )

        # Build INSERT placeholders: nodes=(id, name, *inodes_vals, extra)
        #                            edges=(subj, pred, obj, kl, at, *iedges_vals, extra)
        node_ph = ",".join("?" * (3 + len(inodes)))
        edge_ph = ",".join("?" * (6 + len(iedges)))
        node_insert = f"INSERT OR REPLACE INTO nodes VALUES ({node_ph})"
        edge_insert = f"INSERT OR REPLACE INTO edges VALUES ({edge_ph})"

        print(f"Building SQLite metadata DB from {Path(archive_path).name} ...")
        t0         = time.time()
        node_count = 0
        edge_count = 0
        node_rows: list[tuple[Any, ...]] = []
        cat_rows:  list[tuple[str, str]] = []
        edge_rows: list[tuple[Any, ...]] = []

        # Fields that must be excluded from the extra blob (they have own columns)
        node_blob_exclude = _NODE_CORE | set(inodes)
        edge_blob_exclude = _EDGE_CORE | set(iedges)

        for kind, rec in _stream_kgx(archive_path):
            if kind == "node":
                if not load_nodes:
                    continue
                nid  = rec["id"]
                name = rec.get("name") or None
                cats = [_strip_biolink(c) for c in rec.get("category", [])]
                xtra = {k: v for k, v in rec.items()
                        if k not in node_blob_exclude and (node_xkeys is None or k in node_xkeys)}
                indexed_vals = tuple(
                    str(rec.get(f)) if rec.get(f) is not None else None
                    for f in inodes
                )
                node_rows.append((nid, name) + indexed_vals + (_compress_blob(xtra),))
                for cat in cats:
                    cat_rows.append((nid, cat))
                if len(node_rows) >= _BATCH_SIZE:
                    con.executemany(node_insert, node_rows)
                    con.executemany("INSERT OR REPLACE INTO node_categories VALUES (?,?)", cat_rows)
                    node_count += len(node_rows)
                    node_rows.clear()
                    cat_rows.clear()

            else:  # edge
                if not load_edges:
                    continue
                xtra = {k: v for k, v in rec.items()
                        if k not in edge_blob_exclude and (edge_xkeys is None or k in edge_xkeys)}
                indexed_vals = tuple(
                    str(rec.get(f)) if rec.get(f) is not None else None
                    for f in iedges
                )
                edge_rows.append((
                    rec["subject"],
                    _strip_biolink(rec["predicate"]),
                    rec["object"],
                    rec.get("knowledge_level") or None,
                    rec.get("agent_type") or None,
                ) + indexed_vals + (_compress_blob(xtra),))
                if len(edge_rows) >= _BATCH_SIZE:
                    con.executemany(edge_insert, edge_rows)
                    edge_count += len(edge_rows)
                    edge_rows.clear()

        if node_rows:
            con.executemany(node_insert, node_rows)
            con.executemany("INSERT OR REPLACE INTO node_categories VALUES (?,?)", cat_rows)
            node_count += len(node_rows)
        if edge_rows:
            con.executemany(edge_insert, edge_rows)
            edge_count += len(edge_rows)
        con.commit()

        print(f"  nodes: {node_count:,}" if load_nodes else "  nodes: skipped")
        print(f"  edges: {edge_count:,}" if load_edges else "  edges: skipped")

        con.execute("PRAGMA wal_checkpoint(FULL)")
        con.execute("VACUUM")
        con.close()

        elapsed = time.time() - t0
        size_mb = Path(db_path).stat().st_size / 1024**2
        print(f"Done: {node_count:,} nodes, {edge_count:,} edges, {size_mb:.1f} MB, {elapsed:.1f}s")
        return cls(db_path)

    # -- Single-item lookups -------------------------------------------------

    def get_node(self, node_id: str) -> dict:
        extra_cols = "".join(f", n.{f}" for f in self._indexed_node_fields)
        row = self._conn().execute(
            f"SELECT n.id, n.name{extra_cols}, n.extra, "
            f"GROUP_CONCAT(nc.category) AS categories "
            f"FROM nodes n "
            f"LEFT JOIN node_categories nc ON nc.node_id = n.id "
            f"WHERE n.id = ? GROUP BY n.id",
            (node_id,),
        ).fetchone()
        return self._node_row(row) if row else {}

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        row = self._conn().execute(
            "SELECT * FROM edges WHERE subject=? AND predicate=? AND object=?",
            (subject, _strip_biolink(predicate), obj),
        ).fetchone()
        return self._edge_row(row) if row else {}

    # -- Bulk filtering ------------------------------------------------------

    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not node_ids:
            return []
        ph     = ",".join("?" * len(node_ids))
        params: list[Any] = list(node_ids)
        where  = f"n.id IN ({ph})"
        if category:
            where += " AND n.id IN (SELECT node_id FROM node_categories WHERE category = ?)"
            params.append(_strip_biolink(category))

        # Indexed extra fields → SQL WHERE; non-indexed → Python post-filter
        python_filters: dict = {}
        if extra_filters:
            for k, v in extra_filters.items():
                if k in self._indexed_node_fields:
                    where += f" AND n.{k} = ?"
                    params.append(str(v))
                else:
                    python_filters[k] = v

        extra_cols = "".join(f", n.{f}" for f in self._indexed_node_fields)
        rows = self._conn().execute(
            f"SELECT n.id, n.name{extra_cols}, n.extra, "
            f"GROUP_CONCAT(nc.category) AS categories "
            f"FROM nodes n "
            f"LEFT JOIN node_categories nc ON nc.node_id = n.id "
            f"WHERE {where} GROUP BY n.id",
            params,
        ).fetchall()
        results = [self._node_row(r) for r in rows]
        if python_filters:
            results = [
                d for d in results
                if all(str(d.get(k)) == str(v) for k, v in python_filters.items())
            ]
        return results

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Read straight off the ``idx_nc_category`` index on ``node_categories``."""
        sql = "SELECT node_id FROM node_categories WHERE category = ?"
        params: list[Any] = [_strip_biolink(category)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [r[0] for r in self._conn().execute(sql, params).fetchall()]

    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not edges:
            return []

        # Split into edges with a known predicate vs wildcard (pred=None).
        # Each group is fetched in a single batched query using row-value IN.
        has_pred: list[tuple[str, str, str]] = []
        no_pred:  list[tuple[str, str]]      = []
        for subj, pred, obj in edges:
            if pred is not None:
                has_pred.append((subj, _strip_biolink(pred), obj))
            else:
                no_pred.append((subj, obj))

        # Build SQL suffix for known built-in columns + indexed extra fields
        sql_suffix = ""
        suffix_params: list[Any] = []
        python_filters: dict = {}
        if knowledge_level:
            sql_suffix += " AND knowledge_level = ?"
            suffix_params.append(knowledge_level)
        if agent_type:
            sql_suffix += " AND agent_type = ?"
            suffix_params.append(agent_type)
        if extra_filters:
            for k, v in extra_filters.items():
                if k in self._indexed_edge_fields:
                    sql_suffix += f" AND {k} = ?"
                    suffix_params.append(str(v))
                else:
                    python_filters[k] = v

        results: list[dict] = []

        if has_pred:
            ph = ",".join("(?,?,?)" for _ in has_pred)
            for row in self._conn().execute(
                f"SELECT * FROM edges WHERE (subject,predicate,object) IN (VALUES {ph}){sql_suffix}",
                [v for t in has_pred for v in t] + suffix_params,
            ):
                d = self._edge_row(row)
                if python_filters and not all(
                    str(d.get(k)) == str(v) for k, v in python_filters.items()
                ):
                    continue
                results.append(d)

        if no_pred:
            ph = ",".join("(?,?)" for _ in no_pred)
            for row in self._conn().execute(
                f"SELECT * FROM edges WHERE (subject,object) IN (VALUES {ph}){sql_suffix}",
                [v for t in no_pred for v in t] + suffix_params,
            ):
                d = self._edge_row(row)
                if python_filters and not all(
                    str(d.get(k)) == str(v) for k, v in python_filters.items()
                ):
                    continue
                results.append(d)

        return results

    # -- Internal row helpers ------------------------------------------------

    def _node_row(self, row: sqlite3.Row) -> dict:
        result: dict = {"id": row["id"]}
        if row["name"]:
            result["name"] = row["name"]
        if row["categories"]:
            result["category"] = [_add_biolink(c) for c in row["categories"].split(",")]
        for f in self._indexed_node_fields:
            v = row[f]
            if v is not None:
                result[f] = v
        if row["extra"]:
            result.update(_decompress_blob(row["extra"]))
        return result

    def _edge_row(self, row: sqlite3.Row) -> dict:
        result: dict = {
            "subject":   row["subject"],
            "predicate": _add_biolink(row["predicate"]),
            "object":    row["object"],
        }
        if row["knowledge_level"]:
            result["knowledge_level"] = row["knowledge_level"]
        if row["agent_type"]:
            result["agent_type"] = row["agent_type"]
        for f in self._indexed_edge_fields:
            v = row[f]
            if v is not None:
                result[f] = v
        if row["extra"]:
            result.update(_decompress_blob(row["extra"]))
        return result


# ===========================================================================
# DuckDB backend
# ===========================================================================

class DuckDBMetadataBackend(MetadataBackend):
    """DuckDB-backed metadata store.

    Requires ``pip install duckdb``.

    Uses native ``TEXT[]`` arrays for categories and DuckDB's ``JSON`` type for
    extra fields.  Compatible with the same ``build()`` signature as the SQLite
    backend.

    Build once::

        db = DuckDBMetadataBackend.build("kg.tar.zst", "kg.duckdb")

    Open an existing DB::

        db = DuckDBMetadataBackend("kg.duckdb")
    """

    _SCHEMA_STMTS = [
        """CREATE TABLE IF NOT EXISTS nodes (
               id         TEXT PRIMARY KEY,
               name       TEXT,
               categories TEXT[],
               extra      JSON
           )""",
        """CREATE TABLE IF NOT EXISTS edges (
               subject         TEXT NOT NULL,
               predicate       TEXT NOT NULL,
               object          TEXT NOT NULL,
               knowledge_level TEXT,
               agent_type      TEXT,
               extra           JSON,
               PRIMARY KEY (subject, predicate, object)
           )""",
        "CREATE INDEX IF NOT EXISTS idx_edges_kl ON edges (knowledge_level)",
        "CREATE INDEX IF NOT EXISTS idx_edges_at ON edges (agent_type)",
    ]

    def __init__(self, db_path: str) -> None:
        try:
            import duckdb
        except ImportError:
            raise ImportError("DuckDBMetadataBackend requires: pip install duckdb") from None
        self._con = duckdb.connect(db_path)
        self._indexed_node_fields: list[str] = []
        self._indexed_edge_fields: list[str] = []
        try:
            rows = self._con.execute(
                "SELECT value FROM _meta WHERE key=?", ["indexed_node_fields"]
            ).fetchall()
            if rows:
                self._indexed_node_fields = json.loads(rows[0][0])
            rows = self._con.execute(
                "SELECT value FROM _meta WHERE key=?", ["indexed_edge_fields"]
            ).fetchall()
            if rows:
                self._indexed_edge_fields = json.loads(rows[0][0])
        except Exception:
            pass

    def close(self) -> None:
        self._con.close()

    def _q(self):
        """Return a per-call cursor safe to use from any thread.

        A single DuckDB connection is not safe for concurrent use; ``cursor()``
        returns a new object sharing the same database that can be used
        independently, so a threaded server can query concurrently.
        """
        return self._con.cursor()

    @classmethod
    def build(
        cls,
        archive_path: str,
        db_path: str,
        *,
        node_metadata_fields: list[str] | None = None,
        edge_metadata_fields: list[str] | None = None,
        indexed_extra_node_fields: list[str] | None = None,
        indexed_extra_edge_fields: list[str] | None = None,
    ) -> DuckDBMetadataBackend:
        """Build (or rebuild) a DuckDB metadata store from a KGX archive."""
        try:
            import duckdb
        except ImportError:
            raise ImportError("DuckDBMetadataBackend requires: pip install duckdb") from None

        if indexed_extra_node_fields is None:
            indexed_extra_node_fields = list(DEFAULT_INDEXED_NODE_FIELDS)
        if indexed_extra_edge_fields is None:
            indexed_extra_edge_fields = list(DEFAULT_INDEXED_EDGE_FIELDS)

        inodes: list[str] = indexed_extra_node_fields
        iedges: list[str] = indexed_extra_edge_fields

        if Path(db_path).exists():
            os.remove(db_path)

        con = duckdb.connect(db_path)
        # DuckDB uses adaptive compression by default (selects best algorithm per column).
        # Forcing a specific codec often hurts; rely on DuckDB's auto-selection.

        # Build dynamic schema stmts
        node_extra_col_defs = "".join(f"               {f} TEXT,\n" for f in inodes)
        edge_extra_col_defs = "".join(f"               {f} TEXT,\n" for f in iedges)
        schema_stmts = [
            "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)",
            f"""CREATE TABLE IF NOT EXISTS nodes (
               id         TEXT PRIMARY KEY,
               name       TEXT,
               categories TEXT[],
{node_extra_col_defs}               extra      JSON
           )""",
            f"""CREATE TABLE IF NOT EXISTS edges (
               subject         TEXT NOT NULL,
               predicate       TEXT NOT NULL,
               object          TEXT NOT NULL,
               knowledge_level TEXT,
               agent_type      TEXT,
{edge_extra_col_defs}               extra           JSON,
               PRIMARY KEY (subject, predicate, object)
           )""",
            "CREATE INDEX IF NOT EXISTS idx_edges_kl ON edges (knowledge_level)",
            "CREATE INDEX IF NOT EXISTS idx_edges_at ON edges (agent_type)",
        ]
        for f in inodes:
            schema_stmts.append(f"CREATE INDEX IF NOT EXISTS idx_node_{f} ON nodes ({f})")
        for f in iedges:
            schema_stmts.append(f"CREATE INDEX IF NOT EXISTS idx_edge_{f} ON edges ({f})")

        for stmt in schema_stmts:
            con.execute(stmt)

        # Write indexed field lists to _meta
        con.execute("INSERT OR REPLACE INTO _meta VALUES (?,?)", ["indexed_node_fields", json.dumps(inodes)])
        con.execute("INSERT OR REPLACE INTO _meta VALUES (?,?)", ["indexed_edge_fields", json.dumps(iedges)])

        load_nodes    = node_metadata_fields is not None
        keep_all_node = node_metadata_fields == ["all"] if load_nodes else False
        node_xkeys: set[str] | None = (
            None if keep_all_node
            else set(node_metadata_fields) if load_nodes else None  # type: ignore[arg-type]
        )
        load_edges    = edge_metadata_fields is not None
        keep_all_edge = edge_metadata_fields == ["all"] if load_edges else False
        edge_xkeys: set[str] | None = (
            None if keep_all_edge
            else set(edge_metadata_fields) if load_edges else None  # type: ignore[arg-type]
        )

        # Placeholder counts: nodes=(id,name,categories,*inodes,extra)
        #                      edges=(subj,pred,obj,kl,at,*iedges,extra)
        node_ph = ",".join("?" * (4 + len(inodes)))
        edge_ph = ",".join("?" * (6 + len(iedges)))
        node_insert = f"INSERT INTO nodes VALUES ({node_ph})"
        edge_insert = f"INSERT INTO edges VALUES ({edge_ph})"

        node_blob_exclude = _NODE_CORE | set(inodes)
        edge_blob_exclude = _EDGE_CORE | set(iedges)

        print(f"Building DuckDB metadata from {Path(archive_path).name} ...")
        t0         = time.time()
        node_count = 0
        edge_count = 0

        # Python-side dedup avoids PK conflicts so plain INSERT is safe.
        # Batched inserts let DuckDB build column statistics incrementally for
        # better adaptive compression than a single large executemany.
        node_batch: list[tuple[Any, ...]] = []
        edge_batch: list[tuple[Any, ...]] = []
        seen_nodes: set[str] = set()
        seen_edges: set[tuple[str, str, str]] = set()

        for kind, rec in _stream_kgx(archive_path):
            if kind == "node" and load_nodes:
                nid = rec["id"]
                if nid in seen_nodes:
                    continue
                seen_nodes.add(nid)
                name = rec.get("name") or None
                cats = [_strip_biolink(c) for c in rec.get("category", [])]
                xtra = {k: v for k, v in rec.items()
                        if k not in node_blob_exclude and (node_xkeys is None or k in node_xkeys)}
                indexed_vals = tuple(
                    str(rec.get(f)) if rec.get(f) is not None else None
                    for f in inodes
                )
                node_batch.append((nid, name, cats) + indexed_vals + (json.dumps(xtra) if xtra else None,))
                if len(node_batch) >= _BATCH_SIZE:
                    con.executemany(node_insert, node_batch)
                    node_count += len(node_batch)
                    node_batch.clear()

            elif kind == "edge" and load_edges:
                subj = rec["subject"]
                pred = _strip_biolink(rec["predicate"])
                obj  = rec["object"]
                ekey = (subj, pred, obj)
                if ekey in seen_edges:
                    continue
                seen_edges.add(ekey)
                xtra = {k: v for k, v in rec.items()
                        if k not in edge_blob_exclude and (edge_xkeys is None or k in edge_xkeys)}
                indexed_vals = tuple(
                    str(rec.get(f)) if rec.get(f) is not None else None
                    for f in iedges
                )
                edge_batch.append((
                    subj, pred, obj,
                    rec.get("knowledge_level") or None,
                    rec.get("agent_type") or None,
                ) + indexed_vals + (json.dumps(xtra) if xtra else None,))
                if len(edge_batch) >= _BATCH_SIZE:
                    con.executemany(edge_insert, edge_batch)
                    edge_count += len(edge_batch)
                    edge_batch.clear()

        if node_batch:
            con.executemany(node_insert, node_batch)
            node_count += len(node_batch)
        if edge_batch:
            con.executemany(edge_insert, edge_batch)
            edge_count += len(edge_batch)
        con.close()

        elapsed = time.time() - t0
        size_mb = Path(db_path).stat().st_size / 1024**2
        print(f"Done: {node_count:,} nodes, {edge_count:,} edges, {size_mb:.1f} MB, {elapsed:.1f}s")
        return cls(db_path)

    # -- Single-item lookups -------------------------------------------------

    def _node_select(self) -> str:
        """SELECT clause for nodes including extra indexed columns."""
        extra = "".join(f", {f}" for f in self._indexed_node_fields)
        return f"SELECT id, name, categories{extra}, extra FROM nodes"

    def _edge_select(self) -> str:
        """SELECT clause for edges including extra indexed columns."""
        extra = "".join(f", {f}" for f in self._indexed_edge_fields)
        return f"SELECT subject, predicate, object, knowledge_level, agent_type{extra}, extra FROM edges"

    def get_node(self, node_id: str) -> dict:
        rows = self._q().execute(
            f"{self._node_select()} WHERE id = ?", [node_id]
        ).fetchall()
        return self._node_row(rows[0]) if rows else {}

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        rows = self._q().execute(
            f"{self._edge_select()} WHERE subject=? AND predicate=? AND object=?",
            [subject, _strip_biolink(predicate), obj],
        ).fetchall()
        return self._edge_row(rows[0]) if rows else {}

    # -- Bulk filtering ------------------------------------------------------

    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not node_ids:
            return []

        where = "id = ANY(?)"
        params: list[Any] = [node_ids]
        if category:
            where += " AND array_contains(categories, ?)"
            params.append(_strip_biolink(category))

        python_filters: dict = {}
        if extra_filters:
            for k, v in extra_filters.items():
                if k in self._indexed_node_fields:
                    where += f" AND {k} = ?"
                    params.append(str(v))
                else:
                    python_filters[k] = v

        rows = self._q().execute(
            f"{self._node_select()} WHERE {where}",
            params,
        ).fetchall()
        results = [self._node_row(r) for r in rows]
        if python_filters:
            results = [
                d for d in results
                if all(str(d.get(k)) == str(v) for k, v in python_filters.items())
            ]
        return results

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Scan the ``categories`` list column for *category*.

        There is no category index here, but DuckDB is columnar so projecting a
        single column beats shipping every node id into the query as a candidate
        list.
        """
        sql = "SELECT id FROM nodes WHERE array_contains(categories, ?)"
        params: list[Any] = [_strip_biolink(category)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [r[0] for r in self._q().execute(sql, params).fetchall()]

    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not edges:
            return []

        has_pred: list[tuple[str, str, str]] = []
        no_pred:  list[tuple[str, str]]      = []
        for subj, pred, obj in edges:
            if pred is not None:
                has_pred.append((subj, _strip_biolink(pred), obj))
            else:
                no_pred.append((subj, obj))

        suffix = ""
        suffix_params: list[Any] = []
        python_filters: dict = {}
        if knowledge_level:
            suffix += " AND knowledge_level=?"
            suffix_params.append(knowledge_level)
        if agent_type:
            suffix += " AND agent_type=?"
            suffix_params.append(agent_type)
        if extra_filters:
            for k, v in extra_filters.items():
                if k in self._indexed_edge_fields:
                    suffix += f" AND {k}=?"
                    suffix_params.append(str(v))
                else:
                    python_filters[k] = v

        sel = self._edge_select()
        results: list[dict] = []

        if has_pred:
            ph     = ",".join("(?,?,?)" for _ in has_pred)
            params = [v for t in has_pred for v in t] + suffix_params
            for row in self._q().execute(
                f"{sel} WHERE (subject,predicate,object) IN (VALUES {ph}){suffix}",
                params,
            ).fetchall():
                d = self._edge_row(row)
                if python_filters and not all(
                    str(d.get(k)) == str(v) for k, v in python_filters.items()
                ):
                    continue
                results.append(d)

        if no_pred:
            ph     = ",".join("(?,?)" for _ in no_pred)
            params = [v for t in no_pred for v in t] + suffix_params
            for row in self._q().execute(
                f"{sel} WHERE (subject,object) IN (VALUES {ph}){suffix}",
                params,
            ).fetchall():
                d = self._edge_row(row)
                if python_filters and not all(
                    str(d.get(k)) == str(v) for k, v in python_filters.items()
                ):
                    continue
                results.append(d)

        return results

    # -- Internal row helpers ------------------------------------------------

    def _node_row(self, row: tuple) -> dict:
        # Columns: id, name, categories, *indexed_node_fields, extra
        nid, name, categories = row[0], row[1], row[2]
        offset = 3
        result: dict = {"id": nid}
        if name:
            result["name"] = name
        if categories:
            result["category"] = [_add_biolink(c) for c in categories]
        for i, f in enumerate(self._indexed_node_fields):
            v = row[offset + i]
            if v is not None:
                result[f] = v
        extra_json = row[offset + len(self._indexed_node_fields)]
        if extra_json:
            xtra = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
            if xtra:
                result.update(xtra)
        return result

    def _edge_row(self, row: tuple) -> dict:
        # Columns: subject, predicate, object, knowledge_level, agent_type, *indexed_edge_fields, extra
        subj, pred, obj, kl, at = row[0], row[1], row[2], row[3], row[4]
        offset = 5
        result: dict = {
            "subject":   subj,
            "predicate": _add_biolink(pred),
            "object":    obj,
        }
        if kl:
            result["knowledge_level"] = kl
        if at:
            result["agent_type"] = at
        for i, f in enumerate(self._indexed_edge_fields):
            v = row[offset + i]
            if v is not None:
                result[f] = v
        extra_json = row[offset + len(self._indexed_edge_fields)]
        if extra_json:
            xtra = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
            if xtra:
                result.update(xtra)
        return result


# ===========================================================================
# LMDB backend
# ===========================================================================

class LMDBMetadataBackend(MetadataBackend):
    """LMDB-backed metadata store.

    Requires ``pip install lmdb``.

    Uses four named LMDB databases within a single environment directory:

    * ``nodes``     — key: node CURIE  → compressed JSON of all node metadata
    * ``node_cats`` — key: ``{cat}\\x00{id}``   → ``b""`` (category index)
    * ``edges``     — key: ``{subj}\\x00{pred}\\x00{obj}`` → compressed JSON
    * ``edge_kl``   — key: ``{kl}\\x00{subj}\\x00{pred}\\x00{obj}`` → ``b""``

    Individual PK lookups are O(log N) B-tree traversals — extremely fast.
    Category and knowledge-level index scans are efficient prefix lookups.

    Build once::

        db = LMDBMetadataBackend.build("kg.tar.zst", "kg.lmdb")

    Open an existing environment::

        db = LMDBMetadataBackend("kg.lmdb")

    Open one for serving, without writing ``lock.mdb`` into it::

        db = LMDBMetadataBackend("kg.lmdb", readonly=True)
    """

    _SEP = b"\x00"

    #: ``filter_nodes(category=…)`` probes ``node_cats`` per candidate up to this
    #: many ids, and prefix-scans the category above it.  Biolink categories on a
    #: Translator KG run from tens of thousands to ~1M members, so a threshold in
    #: that range keeps whichever strategy is cheaper: normal traversal batches
    #: (≤ 10k, see ``_MP_FILTER_BATCH``) always probe.
    _CAT_PROBE_MAX = 50_000

    def __init__(
        self,
        db_path: str,
        *,
        map_size: int = 50 * 1024 ** 3,
        readonly: bool = False,
        lock: bool | None = None,
    ) -> None:
        """Open an existing LMDB metadata environment.

        Parameters
        ----------
        readonly:
            Open the environment read-only.  The default (``False``) opens
            read-write, which **creates ``lock.mdb`` in the store directory** —
            so a serving process mutates the release directory it was given, and
            cannot open one at all on a read-only mount.  Set this on any serving
            path: a release is immutable by design, and shared read-only volumes
            (a ``ReadOnlyMany`` PVC across pods) are a supported deployment.
            Read-only LMDB access is safe from many readers at once.
        lock:
            Whether to use the lock file.  Defaults to ``not readonly``.  Passing
            ``readonly=True`` with ``lock=True`` still requires the directory to
            be writable, which defeats the purpose; the default pairing is the
            one you almost always want.  Only override if readers must
            coordinate with a concurrent writer, which a released store has none
            of.
        """
        try:
            import lmdb
        except ImportError:
            raise ImportError("LMDBMetadataBackend requires: pip install lmdb") from None
        if lock is None:
            lock = not readonly
        self.readonly = readonly
        self._env = lmdb.open(
            db_path, max_dbs=5, map_size=map_size, readonly=readonly, lock=lock,
        )
        # A read-only env cannot *create* sub-databases, so the handles must be
        # opened with create=False.  Opening them inside an explicit
        # ``env.begin()`` block instead looks equivalent but is not: py-lmdb
        # aborts a read transaction on context exit, and a handle from an aborted
        # transaction is invalid — every later cursor fails with
        # ``mdb_cursor_open: Invalid argument``.
        if readonly:
            self._nodes_db = self._env.open_db(b"nodes", create=False)
            self._cats_db  = self._env.open_db(b"node_cats", create=False)
            self._edges_db = self._env.open_db(b"edges", create=False)
            self._kl_db    = self._env.open_db(b"edge_kl", create=False)
            self._meta_db  = self._env.open_db(b"_meta", create=False)
        else:
            self._nodes_db = self._env.open_db(b"nodes")
            self._cats_db  = self._env.open_db(b"node_cats")
            self._edges_db = self._env.open_db(b"edges")
            self._kl_db    = self._env.open_db(b"edge_kl")
            self._meta_db  = self._env.open_db(b"_meta")
        self._indexed_node_fields: list[str] = []
        self._indexed_edge_fields: list[str] = []
        try:
            with self._env.begin() as txn:
                val = txn.get(b"indexed_node_fields", db=self._meta_db)
                if val:
                    self._indexed_node_fields = json.loads(val.decode())
                val = txn.get(b"indexed_edge_fields", db=self._meta_db)
                if val:
                    self._indexed_edge_fields = json.loads(val.decode())
        except Exception:
            pass

    def close(self) -> None:
        self._env.close()

    @classmethod
    def build(
        cls,
        archive_path: str,
        db_path: str,
        *,
        node_metadata_fields: list[str] | None = None,
        edge_metadata_fields: list[str] | None = None,
        indexed_extra_node_fields: list[str] | None = None,
        indexed_extra_edge_fields: list[str] | None = None,
        map_size: int = 50 * 1024 ** 3,
    ) -> LMDBMetadataBackend:
        """Build (or rebuild) an LMDB metadata environment from a KGX archive."""
        try:
            import lmdb  # type: ignore[import]  # noqa: F401  — existence check only
        except ImportError:
            raise ImportError("LMDBMetadataBackend requires: pip install lmdb") from None

        if indexed_extra_node_fields is None:
            indexed_extra_node_fields = list(DEFAULT_INDEXED_NODE_FIELDS)
        if indexed_extra_edge_fields is None:
            indexed_extra_edge_fields = list(DEFAULT_INDEXED_EDGE_FIELDS)

        import shutil
        if Path(db_path).exists():
            shutil.rmtree(db_path)

        db = cls(db_path, map_size=map_size)
        db._indexed_node_fields = indexed_extra_node_fields
        db._indexed_edge_fields = indexed_extra_edge_fields

        # Write indexed field lists to _meta
        with db._env.begin(write=True) as meta_txn:
            meta_txn.put(
                b"indexed_node_fields",
                json.dumps(db._indexed_node_fields).encode(),
                db=db._meta_db,
            )
            meta_txn.put(
                b"indexed_edge_fields",
                json.dumps(db._indexed_edge_fields).encode(),
                db=db._meta_db,
            )

        load_nodes    = node_metadata_fields is not None
        keep_all_node = node_metadata_fields == ["all"] if load_nodes else False
        node_xkeys: set[str] | None = (
            None if keep_all_node
            else set(node_metadata_fields) if load_nodes else None  # type: ignore[arg-type]
        )
        load_edges    = edge_metadata_fields is not None
        keep_all_edge = edge_metadata_fields == ["all"] if load_edges else False
        edge_xkeys: set[str] | None = (
            None if keep_all_edge
            else set(edge_metadata_fields) if load_edges else None  # type: ignore[arg-type]
        )

        print(f"Building LMDB metadata from {Path(archive_path).name} ...")
        t0         = time.time()
        node_count = 0
        edge_count = 0
        count      = 0
        sep        = db._SEP

        txn = db._env.begin(write=True)
        try:
            for kind, rec in _stream_kgx(archive_path):
                if kind == "node" and load_nodes:
                    nid  = rec["id"]
                    name = rec.get("name") or None
                    cats = [_strip_biolink(c) for c in rec.get("category", [])]
                    xtra = {k: v for k, v in rec.items()
                            if k not in _NODE_CORE and (node_xkeys is None or k in node_xkeys)}
                    meta: dict = {"id": nid, "category": cats}
                    if name:
                        meta["name"] = name
                    meta.update(xtra)
                    txn.put(nid.encode(), _compress_blob(meta) or b"", db=db._nodes_db)
                    for cat in cats:
                        txn.put(cat.encode() + sep + nid.encode(), b"", db=db._cats_db)
                    node_count += 1

                elif kind == "edge" and load_edges:
                    subj   = rec["subject"]
                    pred_s = _strip_biolink(rec["predicate"])
                    obj    = rec["object"]
                    kl     = rec.get("knowledge_level") or None
                    at     = rec.get("agent_type") or None
                    xtra   = {k: v for k, v in rec.items()
                              if k not in _EDGE_CORE and (edge_xkeys is None or k in edge_xkeys)}
                    meta   = {"subject": subj, "predicate": pred_s, "object": obj}
                    if kl:
                        meta["knowledge_level"] = kl
                    if at:
                        meta["agent_type"] = at
                    meta.update(xtra)
                    # Include the qualifier fingerprint so a triple asserted more
                    # than once with different qualifiers keeps every variant
                    # instead of the last one overwriting the rest.
                    ekey = sep.join([
                        subj.encode(), pred_s.encode(), obj.encode(),
                        qualifier_fingerprint(meta).encode(),
                    ])
                    txn.put(ekey, _compress_blob(meta) or b"", db=db._edges_db)
                    if kl:
                        klkey = sep.join([kl.encode(), subj.encode(), pred_s.encode(), obj.encode()])
                        txn.put(klkey, b"", db=db._kl_db)
                    edge_count += 1

                count += 1
                if count % _BATCH_SIZE == 0:
                    txn.commit()
                    txn = db._env.begin(write=True)

            txn.commit()
        except Exception:
            txn.abort()
            raise

        elapsed = time.time() - t0
        print(f"Done: {node_count:,} nodes, {edge_count:,} edges, {elapsed:.1f}s")
        return db

    # -- Single-item lookups -------------------------------------------------

    def get_node(self, node_id: str) -> dict:
        with self._env.begin() as txn:
            val = txn.get(node_id.encode(), db=self._nodes_db)
        if val is None:
            return {}
        return self._normalise_node(_decompress_blob(val))

    def _variant_prefix(self, subject: str, predicate: str, obj: str) -> bytes:
        return self._SEP.join([
            subject.encode(), _strip_biolink(predicate).encode(), obj.encode(),
        ]) + self._SEP

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        """One edge for this triple.

        When several qualifier variants exist the lowest fingerprint wins, which
        is deterministic across rebuilds. Callers that must see all of them --
        anything filtering on qualifiers -- should use
        :meth:`get_edge_variants`.
        """
        variants = self.get_edge_variants(subject, predicate, obj)
        return variants[0] if variants else {}

    def get_edge_variants(
        self,
        subject: str,
        predicate: str,
        obj: str,
    ) -> list[dict]:
        """All qualifier variants of this triple, ordered by fingerprint.

        A prefix scan rather than an exact get.  Measured on the full store this
        costs 1.00 us/edge against 0.96 us for an exact get -- LMDB positions the
        same B-tree cursor either way -- so there is no fast path worth keeping
        separate for the ~99% of triples that have one variant.
        """
        prefix = self._variant_prefix(subject, predicate, obj)
        out: list[dict] = []
        with self._env.begin() as txn:
            cursor = txn.cursor(db=self._edges_db)
            if cursor.set_range(prefix):
                while True:
                    key = cursor.key()
                    if not key.startswith(prefix):
                        break
                    val = cursor.value()
                    if val:
                        out.append(self._normalise_edge(_decompress_blob(val)))
                    if not cursor.next():
                        break
        return out

    # -- Bulk filtering ------------------------------------------------------

    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not node_ids:
            return []
        cat_key = _strip_biolink(category) if category else None
        results: list[dict] = []
        with self._env.begin() as txn:
            if cat_key is not None:
                prefix = cat_key.encode() + self._SEP
                # ``node_cats`` is keyed ``{category}\x00{node_id}``, so asking
                # "is this node in this category?" is an exact B-tree lookup.
                # Prefix-scanning the category instead costs O(|category|) no
                # matter how few ids were asked about — 51,704 cursor steps to
                # check 500 candidates on translator_kg — and those per-step C
                # calls are also what made threaded serving convoy on the GIL.
                # Probing is O(|ids| · log N).
                #
                # The scan still wins when the caller passes far more ids than the
                # category holds, so it is kept for very large inputs.
                candidates = list(dict.fromkeys(node_ids))  # dedup, keep order
                matched_ids: list[str] = []
                if len(candidates) <= self._CAT_PROBE_MAX:
                    cats_db = self._cats_db
                    matched_ids = [
                        nid for nid in candidates
                        if txn.get(prefix + nid.encode(), db=cats_db) is not None
                    ]
                else:
                    id_set = set(candidates)
                    cursor = txn.cursor(db=self._cats_db)
                    if cursor.set_range(prefix):
                        while True:
                            raw_key = cursor.key()
                            if not raw_key.startswith(prefix):
                                break
                            nid = raw_key[len(prefix):].decode()
                            if nid in id_set:
                                matched_ids.append(nid)
                            if not cursor.next():
                                break
                # Fetch full metadata for matched nodes
                for nid in matched_ids:
                    val = txn.get(nid.encode(), db=self._nodes_db)
                    if val is None:
                        continue
                    data = _decompress_blob(val)
                    if extra_filters and not all(
                        str(data.get(k)) == str(v) for k, v in extra_filters.items()
                    ):
                        continue
                    results.append(self._normalise_node(data))
            else:
                for nid in node_ids:
                    val = txn.get(nid.encode(), db=self._nodes_db)
                    if val is None:
                        continue
                    data = _decompress_blob(val)
                    if extra_filters and not all(
                        str(data.get(k)) == str(v) for k, v in extra_filters.items()
                    ):
                        continue
                    results.append(self._normalise_node(data))
        return results

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Prefix-scan the ``node_cats`` index; no candidate list needed."""
        prefix = _strip_biolink(category).encode() + self._SEP
        out: list[str] = []
        with self._env.begin() as txn:
            cursor = txn.cursor(db=self._cats_db)
            if cursor.set_range(prefix):
                while True:
                    raw_key = cursor.key()
                    if not raw_key.startswith(prefix):
                        break
                    out.append(raw_key[len(prefix):].decode())
                    if limit is not None and len(out) >= limit:
                        break
                    if not cursor.next():
                        break
        return out

    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not edges:
            return []
        sep     = self._SEP
        results: list[dict] = []
        with self._env.begin() as txn:
            for subj, pred, obj in edges:
                if pred is not None:
                    # Every qualifier variant of this triple shares this prefix, and
                    # each is filtered on its own merits: a triple asserted once as
                    # "decreased" and once as "increased" matches a query for
                    # either, which keying on the triple alone could not express.
                    prefix = sep.join([
                        subj.encode(), _strip_biolink(pred).encode(), obj.encode(),
                    ]) + sep
                    cursor = txn.cursor(db=self._edges_db)
                    if cursor.set_range(prefix):
                        while True:
                            if not cursor.key().startswith(prefix):
                                break
                            val = cursor.value()
                            if val:
                                data = _decompress_blob(val)
                                if self._edge_matches(
                                    data, knowledge_level, agent_type, extra_filters
                                ):
                                    results.append(self._normalise_edge(data))
                            if not cursor.next():
                                break
                else:
                    # Any predicate: scan this subject's edges and keep the ones
                    # landing on obj.  Keys are subject/predicate/object/fingerprint,
                    # so four components — not three.
                    prefix = subj.encode() + sep
                    cursor = txn.cursor(db=self._edges_db)
                    if cursor.set_range(prefix):
                        while True:
                            raw_key = cursor.key()
                            if not raw_key.startswith(prefix):
                                break
                            parts = raw_key.split(sep)
                            if len(parts) == 4 and parts[2].decode() == obj:
                                data = _decompress_blob(cursor.value())
                                if self._edge_matches(data, knowledge_level, agent_type, extra_filters):
                                    results.append(self._normalise_edge(data))
                            if not cursor.next():
                                break
        return results

    @staticmethod
    def _edge_matches(
        data: dict,
        knowledge_level: str | None,
        agent_type: str | None,
        extra_filters: dict | None = None,
    ) -> bool:
        if knowledge_level and data.get("knowledge_level") != knowledge_level:
            return False
        if agent_type and data.get("agent_type") != agent_type:
            return False
        if extra_filters and not all(
            str(data.get(k)) == str(v) for k, v in extra_filters.items()
        ):
            return False
        return True

    @staticmethod
    def _normalise_node(data: dict) -> dict:
        result = dict(data)
        if "category" in result:
            result["category"] = [_add_biolink(c) for c in result["category"]]
        return result

    @staticmethod
    def _normalise_edge(data: dict) -> dict:
        result = dict(data)
        if "predicate" in result:
            result["predicate"] = _add_biolink(result["predicate"])
        return result


# ===========================================================================
# Elasticsearch backend
# ===========================================================================

def _es_term_queryable(mapping: dict) -> frozenset[str]:
    """Field names a ``term`` query can match exactly.

    Two ways a filter can silently match nothing rather than erroring:

    * **Unmapped fields.**  Both mappings are ``dynamic: False``, so any other
      field lives in ``_source`` but is not indexed at all.
    * **Analyzed fields.**  ``name`` is ``text``, so a ``term`` query is
      compared against analyzed tokens rather than the original string, and an
      exact-value filter never matches.

    Only ``keyword`` fields are safe to push down; everything else is filtered
    client-side, mirroring how the SQL backends split indexed columns from
    Python post-filtering.
    """
    props = mapping["mappings"]["properties"]
    return frozenset(
        k for k, spec in props.items() if spec.get("type") == "keyword"
    )

class ElasticsearchMetadataBackend(MetadataBackend):
    """Elasticsearch-backed metadata store.

    Requires ``pip install elasticsearch`` and a running ES server
    (tested against http://localhost:9200).

    Uses two indices:

    * ``{prefix}_nodes`` — ``id`` + ``name`` + ``category`` (keyword array) + extra fields
    * ``{prefix}_edges`` — ``subject`` / ``predicate`` / ``object`` /
      ``knowledge_level`` / ``agent_type`` (all keyword) + extra fields

    Build once (re-creates indices)::

        db = ElasticsearchMetadataBackend.build(
            "kg.tar.zst",
            host="http://localhost:9200",
            index_prefix="translator_kg",
        )

    Connect to existing indices::

        db = ElasticsearchMetadataBackend(
            host="http://localhost:9200",
            index_prefix="translator_kg",
        )
    """

    _INDEX_SETTINGS: dict = {
        "index": {
            "codec":              "best_compression",   # ZSTD vs LZ4 default; ~28% smaller
            "number_of_replicas": 0,                    # single-node default; see build()
        }
    }
    _NODES_MAPPING: dict = {
        "settings": _INDEX_SETTINGS,
        "mappings": {
            "dynamic": False,   # unknown fields stored in _source but not indexed
            "properties": {
                "id":       {"type": "keyword"},
                "name":     {"type": "text",
                             "fields": {"keyword": {"type": "keyword"}}},
                "category": {"type": "keyword"},
            },
        },
    }
    _EDGES_MAPPING: dict = {
        "settings": _INDEX_SETTINGS,
        "mappings": {
            "dynamic": False,   # unknown fields stored in _source but not indexed
            "properties": {
                "subject":         {"type": "keyword"},
                "predicate":       {"type": "keyword"},
                "object":          {"type": "keyword"},
                "knowledge_level": {"type": "keyword"},
                "agent_type":      {"type": "keyword"},
            },
        },
    }

    #: Filters on these can be pushed into Elasticsearch; the rest run in Python.
    #: Derived from the mappings so they cannot drift out of sync with them.
    _NODE_MAPPED: frozenset[str] = _es_term_queryable(_NODES_MAPPING)
    _EDGE_MAPPED: frozenset[str] = _es_term_queryable(_EDGES_MAPPING)

    @staticmethod
    def _split_filters(
        extra_filters: dict | None,
        mapped: frozenset[str],
    ) -> tuple[dict, dict]:
        """Split *extra_filters* into (pushed to Elasticsearch, applied in Python)."""
        if not extra_filters:
            return {}, {}
        pushdown = {k: v for k, v in extra_filters.items() if k in mapped}
        py_side = {k: v for k, v in extra_filters.items() if k not in mapped}
        return pushdown, py_side

    @staticmethod
    def _apply_py_filters(rows: list[dict], py_filters: dict) -> list[dict]:
        """Filter *rows* on fields Elasticsearch cannot query."""
        if not py_filters:
            return rows
        return [
            r for r in rows
            if all(str(r.get(k)) == str(v) for k, v in py_filters.items())
        ]

    def __init__(
        self,
        host: str | list[str] = "http://localhost:9200",
        index_prefix: str = "kgquery",
        *,
        connections_per_node: int = 10,
        request_timeout: float = 120.0,
        max_edges_per_pair: int = 100,
    ) -> None:
        """Connect to an Elasticsearch cluster.

        Parameters
        ----------
        host:
            One node URL, or a list of them.  Against a multi-node cluster pass
            every node so the client round-robins instead of routing every
            request through a single coordinating node.
        connections_per_node:
            Size of the urllib3 connection pool per node (default: 10).  This is
            a hard ceiling on in-flight requests per node: serving more
            concurrent queries than this queues them in the client rather than in
            Elasticsearch, which looks like the cluster saturating when it has
            not.  Raise it to at least the number of concurrent callers.
        request_timeout:
            Per-request timeout in seconds (default: 120).  Raise this when
            bulk-indexing large archives whose individual HTTP requests exceed
            the default 10-second elastic-transport timeout.
        max_edges_per_pair:
            Maximum number of edge documents fetched per (subject, predicate,
            object) / (subject, object) match in :meth:`filter_edges`
            (default: 100).  The previous fixed value of 10 silently dropped
            edges for node pairs connected by more than 10 stored edges
            (e.g. multiple knowledge sources).  Raise for densely
            multi-sourced graphs; bounded by Elasticsearch's
            ``index.max_result_window``.
        """
        self._max_edges_per_pair = max(1, max_edges_per_pair)
        try:
            from elasticsearch import Elasticsearch  # type: ignore[import]
            self._es = Elasticsearch(
                host,
                connections_per_node=connections_per_node,
                request_timeout=request_timeout,
                # Retry transient failures (timeouts, 429s) instead of letting
                # them surface as permanent errors.
                retry_on_timeout=True,
                max_retries=3,
            )
        except ImportError:
            raise ImportError(
                "ElasticsearchMetadataBackend requires: pip install elasticsearch"
            ) from None
        self._nodes_idx = f"{index_prefix}_nodes"
        self._edges_idx = f"{index_prefix}_edges"

    def close(self) -> None:
        self._es.close()

    @classmethod
    def build(
        cls,
        archive_path: str,
        *,
        host: str | list[str] = "http://localhost:9200",
        index_prefix: str = "kgquery",
        node_metadata_fields: list[str] | None = None,
        edge_metadata_fields: list[str] | None = None,
        request_timeout: float = 120.0,
        bulk_chunk_size: int = 500,
        number_of_shards: int | None = None,
        number_of_replicas: int | None = None,
    ) -> ElasticsearchMetadataBackend:
        """(Re-)index a KGX archive into Elasticsearch.

        Deletes and recreates the ``{index_prefix}_nodes`` and
        ``{index_prefix}_edges`` indices before indexing.

        Parameters
        ----------
        request_timeout:
            Per-request timeout in seconds (default: 120).  Increase for large
            archives where individual bulk HTTP requests are slow.
        bulk_chunk_size:
            Number of documents per bulk HTTP request (default: 500).  Reduce
            if requests are timing out even with a high *request_timeout*.
        number_of_shards:
            Primary shards per index.  ``None`` leaves the Elasticsearch default
            (1).  Elasticsearch sizing guidance is 10–50 GB per shard, so a
            single shard is correct for a graph of this size; raise it only to
            spread work across the data nodes of a multi-node cluster, accepting
            some fan-out overhead per query in exchange for parallelism.
        number_of_replicas:
            Replica shards per primary.  ``None`` keeps the single-node default
            of 0.  On a cluster, 1 gives redundancy and extra read capacity, at
            the cost of doubling both stored size and indexing work.
        """
        try:
            from elasticsearch.helpers import bulk  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "ElasticsearchMetadataBackend requires: pip install elasticsearch"
            ) from None

        db = cls(host=host, index_prefix=index_prefix, request_timeout=request_timeout)
        es = db._es

        # Per-build shard/replica overrides, layered over the class defaults so
        # the class constant stays the single-node baseline.
        overrides: dict = {}
        if number_of_shards is not None:
            overrides["number_of_shards"] = number_of_shards
        if number_of_replicas is not None:
            overrides["number_of_replicas"] = number_of_replicas

        for idx, mapping in [
            (db._nodes_idx, cls._NODES_MAPPING),
            (db._edges_idx, cls._EDGES_MAPPING),
        ]:
            settings = mapping.get("settings") or {}
            if overrides:
                settings = {"index": {**settings.get("index", {}), **overrides}}
                mapping = {**mapping, "settings": settings}
            if es.indices.exists(index=idx):
                es.indices.delete(index=idx)
            es.indices.create(
                index=idx,
                settings=mapping.get("settings"),
                mappings=mapping.get("mappings"),
            )
            # Suspend refreshes for the bulk load.  At the default 1s interval a
            # multi-million-document build pays continuous segment creation and
            # merging; -1 defers that until the explicit refresh after indexing.
            #
            # If a build fails partway the index is left unsearchable, which is
            # acceptable because it is also incomplete: this method deletes and
            # recreates both indices on entry, so simply re-running the build
            # resets the setting along with the data.
            es.indices.put_settings(
                index=idx, settings={"index": {"refresh_interval": "-1"}}
            )

        load_nodes    = node_metadata_fields is not None
        keep_all_node = node_metadata_fields == ["all"] if load_nodes else False
        node_xkeys: set[str] | None = (
            None if keep_all_node
            else set(node_metadata_fields) if load_nodes else None  # type: ignore[arg-type]
        )
        load_edges    = edge_metadata_fields is not None
        keep_all_edge = edge_metadata_fields == ["all"] if load_edges else False
        edge_xkeys: set[str] | None = (
            None if keep_all_edge
            else set(edge_metadata_fields) if load_edges else None  # type: ignore[arg-type]
        )

        print(f"Indexing into Elasticsearch ({host}) from {Path(archive_path).name} ...",
              flush=True)
        t0         = time.time()
        node_count = 0
        edge_count = 0
        node_buf:  list[dict] = []
        edge_buf:  list[dict] = []

        failed_nodes = 0
        failed_edges = 0
        _last_log    = t0

        _err_shown = False

        def _progress(force: bool = False) -> None:
            nonlocal _last_log
            now = time.time()
            if not force and now - _last_log < 10:
                return
            _last_log = now
            elapsed = now - t0
            rate = (node_count + edge_count) / elapsed if elapsed > 0 else 0
            fail_str = ""
            if failed_nodes or failed_edges:
                fail_str = f"  FAILED: {failed_nodes:,}n {failed_edges:,}e"
            print(
                f"  [{elapsed:6.0f}s]  nodes: {node_count:>12,}  "
                f"edges: {edge_count:>12,}  ({rate:,.0f} docs/s){fail_str}",
                flush=True,
            )

        def _show_first_error(errors: list) -> None:
            nonlocal _err_shown
            if _err_shown or not errors:
                return
            _err_shown = True
            err = errors[0] if isinstance(errors[0], dict) else {"error": str(errors[0])}
            print(f"  ** First bulk error: {err}", flush=True)

        def _flush_nodes() -> None:
            nonlocal node_count, failed_nodes
            if node_buf:
                ok, errors = bulk(es, (  # type: ignore[misc]
                    {"_index": db._nodes_idx, "_id": d["id"], "_source": d}
                    for d in node_buf
                ), chunk_size=bulk_chunk_size, raise_on_error=False)
                node_count += ok
                failed_nodes += len(errors)  # type: ignore[arg-type]
                _show_first_error(errors)  # type: ignore[arg-type]
                node_buf.clear()
                _progress()

        def _flush_edges() -> None:
            nonlocal edge_count, failed_edges
            if edge_buf:
                ok, errors = bulk(es, (
                    {
                        "_index": db._edges_idx,
                        # Include the qualifier fingerprint: a deterministic id on
                        # the triple alone made variants overwrite each other, which
                        # is why the edge index held 28,105,517 docs for 28,925,258
                        # source records.
                        "_id": (
                            f"{d['subject']}|{d['predicate']}|{d['object']}"
                            f"|{qualifier_fingerprint(d)}"
                        ),
                        "_source": d,
                    }
                    for d in edge_buf
                ), chunk_size=bulk_chunk_size, raise_on_error=False)
                edge_count += ok
                failed_edges += len(errors)  # type: ignore[arg-type]
                _show_first_error(errors)  # type: ignore[arg-type]
                edge_buf.clear()
                _progress()

        for kind, rec in _stream_kgx(archive_path):
            if kind == "node" and load_nodes:
                doc: dict = {"id": rec["id"]}
                if rec.get("name"):
                    doc["name"] = rec["name"]
                cats = [_strip_biolink(c) for c in rec.get("category", [])]
                if cats:
                    doc["category"] = cats
                xtra = {k: v for k, v in rec.items()
                        if k not in _NODE_CORE and (node_xkeys is None or k in node_xkeys)}
                doc.update(xtra)
                node_buf.append(doc)
                if len(node_buf) >= _BATCH_SIZE:
                    _flush_nodes()

            elif kind == "edge" and load_edges:
                doc = {
                    "subject":   rec["subject"],
                    "predicate": _strip_biolink(rec["predicate"]),
                    "object":    rec["object"],
                }
                if rec.get("knowledge_level"):
                    doc["knowledge_level"] = rec["knowledge_level"]
                if rec.get("agent_type"):
                    doc["agent_type"] = rec["agent_type"]
                xtra = {k: v for k, v in rec.items()
                        if k not in _EDGE_CORE and (edge_xkeys is None or k in edge_xkeys)}
                doc.update(xtra)
                edge_buf.append(doc)
                if len(edge_buf) >= _BATCH_SIZE:
                    _flush_edges()

        _flush_nodes()
        _flush_edges()
        _progress(force=True)

        # Restore the default refresh behaviour suspended during the bulk load and
        # make everything just indexed visible to search.  Do this before the
        # force-merge so the indices are queryable even if the merge is slow.
        es_slow = es.options(request_timeout=request_timeout)
        for idx in [db._nodes_idx, db._edges_idx]:
            es.indices.put_settings(
                index=idx, settings={"index": {"refresh_interval": None}}
            )
            es_slow.indices.refresh(index=idx)

        # Force-merge each index to a single segment — significantly reduces
        # on-disk size by collapsing Lucene segment files.  Can be slow on large
        # indices; uses the same request_timeout as bulk indexing.
        for idx in [db._nodes_idx, db._edges_idx]:
            print(f"Force-merging {idx} to 1 segment ...", flush=True)
            es_slow.indices.forcemerge(index=idx, max_num_segments=1)

        elapsed = time.time() - t0
        msg = f"Done: {node_count:,} nodes, {edge_count:,} edges, {elapsed:.1f}s"
        if failed_nodes or failed_edges:
            msg += f"  (skipped: {failed_nodes:,} nodes, {failed_edges:,} edges with mapping errors)"
        print(msg)
        return db

    # -- Single-item lookups -------------------------------------------------

    def get_node(self, node_id: str) -> dict:
        from elasticsearch import NotFoundError  # type: ignore[import]

        try:
            resp = self._es.get(index=self._nodes_idx, id=node_id)
            return self._normalise_node(resp["_source"])
        except NotFoundError:
            # Genuinely absent — distinct from a connection/transport error,
            # which we deliberately let propagate rather than mask as "{}".
            return {}

    def get_edge_variants(
        self,
        subject: str,
        predicate: str,
        obj: str,
    ) -> list[dict]:
        """All qualifier variants of this triple.

        Retrieval is by term query on subject/predicate/object, so it already
        returns every stored document for the triple; only the write-time ``_id``
        needed the fingerprint to stop variants overwriting one another.

        Deliberately **not** bounded by ``max_edges_per_pair``.  That constant
        sizes the bulk :meth:`filter_edges` path, where it trades against msearch
        batch size, and its default of 100 truncated real triples here:
        ``CHEBI:33216 -affects-> GO:0008283`` carries 103 variants in the
        2026-07-19 graph, so ES returned 100 where LMDB returned all 103 — both a
        dropped-answer bug on qualifier constraints and a divergence between the
        two backends.  This is a single-triple point lookup, one request either
        way, so it uses the result window instead: a triple with more variants
        than that is implausible (the observed maximum is ~10^2), and if one ever
        appears the count is reported rather than silently dropped.
        """
        resp = self._es.search(
            index=self._edges_idx,
            query={"bool": {"must": [
                {"term": {"subject": subject}},
                {"term": {"predicate": _strip_biolink(predicate)}},
                {"term": {"object": obj}},
            ]}},
            size=self._ES_MAX_RESULT_WINDOW,
            track_total_hits=True,
        )
        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]["value"]
        if total > len(hits):
            warnings.warn(
                f"{subject} -{predicate}-> {obj} has {total:,} stored variants; "
                f"returning the first {len(hits):,} (index.max_result_window). "
                "Qualifier constraints on this edge may miss answers.",
                RuntimeWarning,
                stacklevel=2,
            )
        return [self._normalise_edge(h["_source"]) for h in hits]

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        from elasticsearch import NotFoundError  # type: ignore[import]

        eid = f"{subject}|{_strip_biolink(predicate)}|{obj}"
        try:
            resp = self._es.get(index=self._edges_idx, id=eid)
            return self._normalise_edge(resp["_source"])
        except NotFoundError:
            return {}

    # -- Bulk filtering ------------------------------------------------------

    _ES_MAX_RESULT_WINDOW = 10_000  # Elasticsearch default index.max_result_window

    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not node_ids:
            return []

        # Build the non-ID part of the filter once.
        extra_must: list[dict] = []
        if category:
            extra_must.append({"term": {"category": _strip_biolink(category)}})
        pushdown, py_filters = self._split_filters(extra_filters, self._NODE_MAPPED)
        for k, v in pushdown.items():
            extra_must.append({"term": {k: str(v)}})

        # Batch node IDs in chunks to stay within ES limits.
        # The ``ids`` query and ``size`` parameter both have practical caps
        # around 10K, so we chunk the input and combine results.
        chunk_size = self._ES_MAX_RESULT_WINDOW
        results: list[dict] = []
        for start in range(0, len(node_ids), chunk_size):
            chunk = node_ids[start : start + chunk_size]
            must: list[dict] = [{"ids": {"values": chunk}}]
            must.extend(extra_must)
            resp = self._es.search(
                index=self._nodes_idx,
                query={"bool": {"must": must}},
                size=len(chunk),
            )
            results.extend(
                self._normalise_node(h["_source"]) for h in resp["hits"]["hits"]
            )
        return self._apply_py_filters(results, py_filters)

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Term query on ``category``, paged with ``search_after``.

        A Biolink category routinely holds more than ``index.max_result_window``
        nodes, so this pages on a sort key instead of asking for one oversized
        result set.  Only the ``id`` field is fetched.

        Sorting is on the ``id`` keyword field rather than ``_id``: node CURIEs
        are unique, so ``id`` is a valid tiebreaker, and sorting on ``_id``
        needs fielddata that Elasticsearch disallows by default
        (``indices.id_field_data.enabled``).
        """
        page = self._ES_MAX_RESULT_WINDOW
        if limit is not None:
            page = min(page, limit)

        out: list[str] = []
        search_after: list | None = None
        while True:
            kwargs: dict = {
                "index": self._nodes_idx,
                "query": {"term": {"category": _strip_biolink(category)}},
                "size": page,
                "sort": [{"id": "asc"}],
                "source_includes": ["id"],
            }
            if search_after is not None:
                kwargs["search_after"] = search_after
            resp = self._es.search(**kwargs)
            hits = resp["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                out.append(h["_source"].get("id") or h["_id"])
                if limit is not None and len(out) >= limit:
                    return out
            if len(hits) < page:
                break
            search_after = hits[-1]["sort"]
        return out

    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        if not edges:
            return []

        has_pred: list[tuple[str, str, str]] = []
        no_pred:  list[tuple[str, str]]      = []
        for subj, pred, obj in edges:
            if pred is not None:
                has_pred.append((subj, _strip_biolink(pred), obj))
            else:
                no_pred.append((subj, obj))

        results: list[dict] = []
        # Only mapped fields can be pushed into Elasticsearch; the rest must be
        # filtered client-side or they would silently match nothing.
        pushdown, py_filters = self._split_filters(extra_filters, self._EDGE_MAPPED)

        # ── Known-predicate edges: msearch (one HTTP round-trip per batch) ───
        # ES processes all sub-queries in parallel server-side.
        # Batch to avoid exceeding msearch limits on very large edge lists.
        per_pair = self._max_edges_per_pair
        if has_pred:
            # Keep each msearch's worst-case total hits within the result
            # window: batch_size * per_pair <= _ES_MAX_RESULT_WINDOW.
            _MSEARCH_BATCH = max(1, self._ES_MAX_RESULT_WINDOW // per_pair)
            for batch_start in range(0, len(has_pred), _MSEARCH_BATCH):
                batch = has_pred[batch_start : batch_start + _MSEARCH_BATCH]
                searches: list[dict] = []
                for s, p, o in batch:
                    must: list[dict] = [
                        {"term": {"subject":   s}},
                        {"term": {"predicate": p}},
                        {"term": {"object":    o}},
                    ]
                    if knowledge_level:
                        must.append({"term": {"knowledge_level": knowledge_level}})
                    if agent_type:
                        must.append({"term": {"agent_type": agent_type}})
                    for k, v in pushdown.items():
                        must.append({"term": {k: str(v)}})
                    searches.append({"index": self._edges_idx})
                    searches.append({"query": {"bool": {"must": must}}, "size": per_pair})
                resp = self._es.msearch(searches=searches)
                for r in resp["responses"]:
                    for hit in r.get("hits", {}).get("hits", []):
                        results.append(self._normalise_edge(hit["_source"]))

        # ── Wildcard-predicate edges: msearch, one sub-query per pair ─────────
        # This used to be a single bool/should query per chunk with
        # ``size = len(batch) * per_pair``.  That budget is *shared*: ES returns
        # the globally top-scoring hits, so one dense pair could consume it and
        # crowd other pairs of the same chunk out entirely — dropping whole
        # predicates rather than surplus variants of one.  Qualifier-variant
        # keying made that acute, since a pair like
        # ``CHEBI:33216 / GO:0008283`` now stores 103 documents where it
        # previously stored one.  An msearch gives each pair its own ``size``,
        # for the same number of HTTP round-trips as the known-predicate branch.
        if no_pred:
            _MSEARCH_BATCH_NP = max(1, self._ES_MAX_RESULT_WINDOW // per_pair)
            for batch_start in range(0, len(no_pred), _MSEARCH_BATCH_NP):
                batch = no_pred[batch_start : batch_start + _MSEARCH_BATCH_NP]
                searches_np: list[dict] = []
                for s, o in batch:
                    must_np: list[dict] = [
                        {"term": {"subject": s}},
                        {"term": {"object":  o}},
                    ]
                    if knowledge_level:
                        must_np.append({"term": {"knowledge_level": knowledge_level}})
                    if agent_type:
                        must_np.append({"term": {"agent_type": agent_type}})
                    for k, v in pushdown.items():
                        must_np.append({"term": {k: str(v)}})
                    searches_np.append({"index": self._edges_idx})
                    searches_np.append(
                        {"query": {"bool": {"must": must_np}}, "size": per_pair}
                    )
                resp = self._es.msearch(searches=searches_np)
                for r in resp["responses"]:
                    for hit in r.get("hits", {}).get("hits", []):
                        results.append(self._normalise_edge(hit["_source"]))

        return self._apply_py_filters(results, py_filters)

    @staticmethod
    def _normalise_node(data: dict) -> dict:
        result = dict(data)
        if "category" in result:
            result["category"] = [_add_biolink(c) for c in result["category"]]
        return result

    @staticmethod
    def _normalise_edge(data: dict) -> dict:
        result = dict(data)
        if "predicate" in result:
            result["predicate"] = _add_biolink(result["predicate"])
        return result




# ===========================================================================
# Hybrid backend  (LMDB + ES with configurable routing mode)
# ===========================================================================

class HybridMetadataBackend(MetadataBackend):
    """Routes queries between LMDB and Elasticsearch based on a *mode* and
    a configurable *threshold*.

    Modes
    -----
    ``"lmdb"``
        All operations use LMDB.  ES backend is not required.
    ``"es"``
        All operations use Elasticsearch.  LMDB backend is not required.
    ``"auto"``  *(default)*
        Routes per-operation based on *threshold*:

        * ``get_node`` / ``get_edge``   → LMDB always  (0.004 ms vs 1.6 ms)
        * ``filter_nodes`` / ``filter_edges`` with no active filter → LMDB
        * ``filter_nodes`` / ``filter_edges`` with filter + ``len ≤ threshold`` → LMDB
        * ``filter_nodes`` / ``filter_edges`` with filter + ``len >  threshold`` → ES

        Point lookups always go to LMDB in "auto" mode because LMDB is
        400× faster (0.004 ms vs 1.6 ms) regardless of data scale.

    Threshold
    ---------
    The default **2000** is the empirical cross-over from translator_kg
    benchmarks on the build server.  Production servers with different I/O
    characteristics will have a different cross-over — re-benchmark and
    adjust with ``HybridMetadataBackend(..., threshold=N)``.

    Usage::

        lmdb = LMDBMetadataBackend("kg.metadata.lmdb")
        es   = ElasticsearchMetadataBackend("kg", host="http://localhost:9200")

        # Auto routing (recommended)
        db = HybridMetadataBackend(lmdb=lmdb, es=es)

        # LMDB-only  (no ES needed)
        db = HybridMetadataBackend(lmdb=lmdb, mode="lmdb")

        # ES-only  (no LMDB needed)
        db = HybridMetadataBackend(es=es, mode="es")

        # Auto with custom threshold
        db = HybridMetadataBackend(lmdb=lmdb, es=es, threshold=5000)
    """

    def __init__(
        self,
        lmdb: "LMDBMetadataBackend | None" = None,
        es: "ElasticsearchMetadataBackend | None" = None,
        *,
        mode: str = "auto",
        node_threshold: int = 2000,
        edge_threshold: int | None = None,
    ) -> None:
        """
        Parameters
        ----------
        lmdb:
            Open :class:`LMDBMetadataBackend`.  Required for modes ``"lmdb"``
            and ``"auto"``.
        es:
            Open :class:`ElasticsearchMetadataBackend`.  Required for modes
            ``"es"`` and ``"auto"``.
        mode:
            ``"lmdb"``, ``"es"``, or ``"auto"`` (default).
        node_threshold:
            ``filter_nodes`` input size above which ``"auto"`` mode routes to
            ES (when a category/extra filter is active).
            Default **2000** — empirical cross-over from translator_kg
            benchmarks (LMDB ~3.8 µs/node, ES flat ~6 ms).
            Tune after benchmarking on your deployment servers.
        edge_threshold:
            ``filter_edges`` input size above which ``"auto"`` mode routes to
            ES (when a filter is active).
            Default **None** (always use LMDB) — ES is consistently slower
            for edge filtering at all tested sizes.  Set an explicit integer
            once you have deployment benchmarks that show a cross-over.
        """
        if mode not in ("lmdb", "es", "auto"):
            raise ValueError(f"mode must be 'lmdb', 'es', or 'auto'; got {mode!r}")
        if mode in ("lmdb", "auto") and lmdb is None:
            raise ValueError(f"mode={mode!r} requires an lmdb backend")
        if mode in ("es", "auto") and es is None:
            raise ValueError(f"mode={mode!r} requires an es backend")

        self._lmdb = lmdb
        self._es = es
        self._mode = mode
        self._node_threshold = node_threshold
        self._edge_threshold = edge_threshold  # None = always LMDB

    # ── internal routing helpers ─────────────────────────────────────────────

    def _use_es_for_nodes(self, n: int, has_filter: bool) -> bool:
        """Return True if filter_nodes should be routed to ES."""
        if self._mode == "lmdb":
            return False
        if self._mode == "es":
            return True
        return has_filter and n > self._node_threshold

    def _use_es_for_edges(self, n: int, has_filter: bool) -> bool:
        """Return True if filter_edges should be routed to ES."""
        if self._mode == "lmdb":
            return False
        if self._mode == "es":
            return True
        # Default: always LMDB (edge_threshold=None). ES is only used when
        # an explicit threshold is set and the input exceeds it.
        if self._edge_threshold is None:
            return False
        return has_filter and n > self._edge_threshold

    # ── point lookups ────────────────────────────────────────────────────────

    def get_node(self, node_id: str) -> dict:
        if self._mode == "es":
            return self._es.get_node(node_id)  # type: ignore[union-attr]
        return self._lmdb.get_node(node_id)  # type: ignore[union-attr]

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        if self._mode == "es":
            return self._es.get_edge(subject, predicate, obj)  # type: ignore[union-attr]
        return self._lmdb.get_edge(subject, predicate, obj)  # type: ignore[union-attr]

    def get_edge_variants(
        self,
        subject: str,
        predicate: str,
        obj: str,
    ) -> list[dict]:
        """Route to whichever backend serves point edge lookups."""
        if self._lmdb is not None:
            return self._lmdb.get_edge_variants(subject, predicate, obj)
        return self._es.get_edge_variants(subject, predicate, obj)  # type: ignore[union-attr]

    def nodes_by_category(
        self,
        category: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        """Prefer LMDB's local prefix scan; fall back to ES when LMDB is absent.

        Unlike ``filter_nodes`` there is no input size to route on, and a local
        index scan beats a network round-trip, so LMDB wins whenever available.
        """
        if self._lmdb is not None:
            return self._lmdb.nodes_by_category(category, limit=limit)
        return self._es.nodes_by_category(category, limit=limit)  # type: ignore[union-attr]

    # ── bulk filtering ───────────────────────────────────────────────────────

    def filter_nodes(
        self,
        node_ids: list[str],
        *,
        category: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        has_filter = category is not None or bool(extra_filters)
        if self._use_es_for_nodes(len(node_ids), has_filter):
            return self._es.filter_nodes(  # type: ignore[union-attr]
                node_ids, category=category, extra_filters=extra_filters
            )
        return self._lmdb.filter_nodes(  # type: ignore[union-attr]
            node_ids, category=category, extra_filters=extra_filters
        )

    def filter_edges(
        self,
        edges: list[PathEdge],
        *,
        knowledge_level: str | None = None,
        agent_type: str | None = None,
        extra_filters: dict | None = None,
    ) -> list[dict]:
        has_filter = (
            knowledge_level is not None
            or agent_type is not None
            or bool(extra_filters)
        )
        if self._use_es_for_edges(len(edges), has_filter):
            return self._es.filter_edges(  # type: ignore[union-attr]
                edges,
                knowledge_level=knowledge_level,
                agent_type=agent_type,
                extra_filters=extra_filters,
            )
        return self._lmdb.filter_edges(  # type: ignore[union-attr]
            edges,
            knowledge_level=knowledge_level,
            agent_type=agent_type,
            extra_filters=extra_filters,
        )

    def close(self) -> None:
        if self._lmdb is not None:
            self._lmdb.close()
        if self._es is not None:
            self._es.close()
