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
import json
import os
import sqlite3
import tarfile
import time
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
    return s if s.startswith(_BIOLINK_PREFIX) else _BIOLINK_PREFIX + s


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
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._indexed_node_fields: list[str] = []
        self._indexed_edge_fields: list[str] = []
        try:
            row = self._con.execute(
                "SELECT value FROM _meta WHERE key='indexed_node_fields'"
            ).fetchone()
            if row:
                self._indexed_node_fields = json.loads(row[0])
            row = self._con.execute(
                "SELECT value FROM _meta WHERE key='indexed_edge_fields'"
            ).fetchone()
            if row:
                self._indexed_edge_fields = json.loads(row[0])
        except Exception:
            pass

    def close(self) -> None:
        self._con.close()

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

        db  = cls.__new__(cls)
        db.db_path = db_path
        db._con = sqlite3.connect(db_path, check_same_thread=False)
        db._con.row_factory = sqlite3.Row
        db._indexed_node_fields = inodes
        db._indexed_edge_fields = iedges

        con = db._con
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

        elapsed = time.time() - t0
        size_mb = Path(db_path).stat().st_size / 1024**2
        print(f"Done: {node_count:,} nodes, {edge_count:,} edges, {size_mb:.1f} MB, {elapsed:.1f}s")
        return db

    # -- Single-item lookups -------------------------------------------------

    def get_node(self, node_id: str) -> dict:
        extra_cols = "".join(f", n.{f}" for f in self._indexed_node_fields)
        row = self._con.execute(
            f"SELECT n.id, n.name{extra_cols}, n.extra, "
            f"GROUP_CONCAT(nc.category) AS categories "
            f"FROM nodes n "
            f"LEFT JOIN node_categories nc ON nc.node_id = n.id "
            f"WHERE n.id = ? GROUP BY n.id",
            (node_id,),
        ).fetchone()
        return self._node_row(row) if row else {}

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        row = self._con.execute(
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
        rows = self._con.execute(
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
            for row in self._con.execute(
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
            for row in self._con.execute(
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
        rows = self._con.execute(
            f"{self._node_select()} WHERE id = ?", [node_id]
        ).fetchall()
        return self._node_row(rows[0]) if rows else {}

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        rows = self._con.execute(
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

        rows = self._con.execute(
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
            for row in self._con.execute(
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
            for row in self._con.execute(
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
    """

    _SEP = b"\x00"

    def __init__(self, db_path: str, *, map_size: int = 50 * 1024 ** 3) -> None:
        try:
            import lmdb
        except ImportError:
            raise ImportError("LMDBMetadataBackend requires: pip install lmdb") from None
        self._env      = lmdb.open(db_path, max_dbs=5, map_size=map_size)
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
                    ekey = sep.join([subj.encode(), pred_s.encode(), obj.encode()])
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

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        pred_s = _strip_biolink(predicate)
        ekey   = self._SEP.join([subject.encode(), pred_s.encode(), obj.encode()])
        with self._env.begin() as txn:
            val = txn.get(ekey, db=self._edges_db)
        if val is None:
            return {}
        return self._normalise_edge(_decompress_blob(val))

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
                # Use _cats_db prefix scan: keys are {category}\x00{node_id}
                id_set = set(node_ids)
                prefix = cat_key.encode() + self._SEP
                matched_ids: list[str] = []
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
                    ekey = sep.join([subj.encode(), _strip_biolink(pred).encode(), obj.encode()])
                    val  = txn.get(ekey, db=self._edges_db)
                    if val is None:
                        continue
                    data = _decompress_blob(val)
                    if self._edge_matches(data, knowledge_level, agent_type, extra_filters):
                        results.append(self._normalise_edge(data))
                else:
                    # Scan all edges for this subject, then filter by object
                    prefix = subj.encode() + sep
                    cursor = txn.cursor(db=self._edges_db)
                    if cursor.set_range(prefix):
                        while True:
                            raw_key = cursor.key()
                            if not raw_key.startswith(prefix):
                                break
                            parts = raw_key.split(sep)
                            if len(parts) == 3 and parts[2].decode() == obj:
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
            "number_of_replicas": 0,                    # single-node: no replica shards
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

    def __init__(
        self,
        host: str = "http://localhost:9200",
        index_prefix: str = "kgquery",
        *,
        connections_per_node: int = 10,
        request_timeout: float = 120.0,
    ) -> None:
        """Connect to an Elasticsearch cluster.

        Parameters
        ----------
        connections_per_node:
            Size of the urllib3 connection pool per node (default: 10).
        request_timeout:
            Per-request timeout in seconds (default: 120).  Raise this when
            bulk-indexing large archives whose individual HTTP requests exceed
            the default 10-second elastic-transport timeout.
        """
        try:
            from elasticsearch import Elasticsearch  # type: ignore[import]
            self._es = Elasticsearch(
                host,
                connections_per_node=connections_per_node,
                request_timeout=request_timeout,
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
        host: str = "http://localhost:9200",
        index_prefix: str = "kgquery",
        node_metadata_fields: list[str] | None = None,
        edge_metadata_fields: list[str] | None = None,
        request_timeout: float = 120.0,
        bulk_chunk_size: int = 500,
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
        """
        try:
            from elasticsearch.helpers import bulk  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "ElasticsearchMetadataBackend requires: pip install elasticsearch"
            ) from None

        db = cls(host=host, index_prefix=index_prefix, request_timeout=request_timeout)
        es = db._es

        for idx, mapping in [
            (db._nodes_idx, cls._NODES_MAPPING),
            (db._edges_idx, cls._EDGES_MAPPING),
        ]:
            if es.indices.exists(index=idx):
                es.indices.delete(index=idx)
            es.indices.create(
                index=idx,
                settings=mapping.get("settings"),
                mappings=mapping.get("mappings"),
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
                        "_id": f"{d['subject']}|{d['predicate']}|{d['object']}",
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

        # Force-merge each index to a single segment — significantly reduces
        # on-disk size by collapsing Lucene segment files.  Can be slow on large
        # indices; uses the same request_timeout as bulk indexing.
        es_slow = es.options(request_timeout=request_timeout)
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
        try:
            resp = self._es.get(index=self._nodes_idx, id=node_id)
            return self._normalise_node(resp["_source"])
        except Exception:
            return {}

    def get_edge(self, subject: str, predicate: str, obj: str) -> dict:
        eid = f"{subject}|{_strip_biolink(predicate)}|{obj}"
        try:
            resp = self._es.get(index=self._edges_idx, id=eid)
            return self._normalise_edge(resp["_source"])
        except Exception:
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
        if extra_filters:
            for k, v in extra_filters.items():
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
        return results

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

        # ── Known-predicate edges: msearch (one HTTP round-trip per batch) ───
        # ES processes all sub-queries in parallel server-side.
        # Batch to avoid exceeding msearch limits on very large edge lists.
        if has_pred:
            _MSEARCH_BATCH = self._ES_MAX_RESULT_WINDOW
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
                    if extra_filters:
                        for k, v in extra_filters.items():
                            must.append({"term": {k: str(v)}})
                    searches.append({"index": self._edges_idx})
                    searches.append({"query": {"bool": {"must": must}}, "size": 10})
                resp = self._es.msearch(searches=searches)
                for r in resp["responses"]:
                    for hit in r.get("hits", {}).get("hits", []):
                        results.append(self._normalise_edge(hit["_source"]))

        # ── Wildcard-predicate edges: bool/should search ─────────────────────
        # Batched in chunks so that neither the ``should`` clause count nor
        # ``size`` exceeds ``_ES_MAX_RESULT_WINDOW``.
        if no_pred:
            # Each (subject, object) pair may yield up to ~10 edges, so we
            # limit chunks so that chunk_len * 10 <= _ES_MAX_RESULT_WINDOW.
            _SHOULD_CHUNK = max(1, self._ES_MAX_RESULT_WINDOW // 10)
            for batch_start in range(0, len(no_pred), _SHOULD_CHUNK):
                batch = no_pred[batch_start : batch_start + _SHOULD_CHUNK]
                should: list[dict] = []
                for s, o in batch:
                    must_np: list[dict] = [
                        {"term": {"subject": s}},
                        {"term": {"object":  o}},
                    ]
                    if knowledge_level:
                        must_np.append({"term": {"knowledge_level": knowledge_level}})
                    if agent_type:
                        must_np.append({"term": {"agent_type": agent_type}})
                    if extra_filters:
                        for k, v in extra_filters.items():
                            must_np.append({"term": {k: str(v)}})
                    should.append({"bool": {"must": must_np}})
                resp = self._es.search(
                    index=self._edges_idx,
                    query={"bool": {"should": should, "minimum_should_match": 1}},
                    size=len(batch) * 10,
                )
                for hit in resp["hits"]["hits"]:
                    results.append(self._normalise_edge(hit["_source"]))

        return results

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
