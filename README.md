# csrgraph

A memory-efficient, CSR-backed knowledge graph for [Biolink](https://biolink.github.io/biolink-model/)/[Translator](https://ncats.nih.gov/translator) data, with [KGX](https://github.com/biolink/kgx) archive loading, pluggable metadata backends, path-finding, and a [TRAPI](https://github.com/NCATSTranslator/ReasonerAPI) query server.

Graph topology is stored as per-predicate [SciPy CSR matrices](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.csr_matrix.html), keeping large knowledge graphs in a fraction of the RAM a typical adjacency-list representation would need. Node and edge metadata is kept in a separate, swappable backend (SQLite / DuckDB / LMDB / Elasticsearch) so it can stay on disk and out of memory.

## Features

- **KGX archive loading** — stream nodes/edges from `.tar.zst` / `.tar.gz` / `.tar` archives with low memory overhead (`CSRGraph.from_kgx_archive`).
- **Compact CSR topology** — one CSR matrix per predicate; the constant `biolink:` prefix is stripped internally and re-added transparently on output.
- **Pluggable metadata backends** — `SQLiteMetadataBackend`, `DuckDBMetadataBackend`, `LMDBMetadataBackend`, `ElasticsearchMetadataBackend`, and a `HybridMetadataBackend` (LMDB for fast lookups + ES for full-text search).
- **Fast serialization** — pickle + zstd snapshots (`graph.save()` / `CSRGraph.load()`), with optional memory-mapped CSR loading for instant startup.
- **Graph queries** — `neighbors`, `shortest_path`, `all_shortest_paths`, `all_paths`, `paths_by_predicate_sequence`, `match_path` (pattern-based multi-hop traversal), plus node/edge metadata filters.
- **TRAPI server** — a FastAPI app (`trapi_server.py`) exposing the graph as a TRAPI query endpoint.

## Requirements

- **Python 3.14+** (uses the built-in `compression.zstd`), or an older Python with the `zstandard` package installed.
- `numpy`, `scipy` (core).
- Optional features are packaged as extras: `lmdb`, `es`, `duckdb`, `server`, `psutil`, `all`, and `dev`.

```bash
# core only
pip install -e .

# with optional backends / the TRAPI server / test deps
pip install -e ".[lmdb]"      # LMDB metadata backend
pip install -e ".[es]"        # Elasticsearch backend
pip install -e ".[server]"    # FastAPI TRAPI server
pip install -e ".[all]"       # everything
pip install -e ".[dev]"       # test dependencies (pytest, ...)
```

## Quick start

```python
from csrgraph_kgx import CSRGraph
from metadata_db import LMDBMetadataBackend

# Build from a KGX archive (one-time), then snapshot for fast reloads
graph = CSRGraph.from_kgx_archive("translator_kg.tar.zst")
graph.save("translator_kg.csrgraph.pkl.zst")

# Reload a prebuilt snapshot with a metadata backend attached
db = LMDBMetadataBackend("translator_kg.metadata.lmdb")
graph = CSRGraph.load("translator_kg.csrgraph.pkl.zst", db=db)
print(f"{graph.num_nodes:,} nodes, {graph.edge_count:,} edges")

# Neighbors, filtered by category
nbrs = graph.neighbors("CHEBI:6801")  # Metformin
genes = graph.filter_nodes(nbrs, category="biolink:Gene")

# Shortest path: Metformin -> Type 2 Diabetes
for s, p, o in graph.shortest_path("CHEBI:6801", "MONDO:0005148") or []:
    print(s, p, o)

# Pattern match: Drug -> Gene -> Disease
paths = graph.match_path([
    "CHEBI:6801",
    None, {"category": "biolink:Gene"},
    None, {"category": "biolink:Disease"},
], limit=5)
```

See [`usage_demo.py`](usage_demo.py) for a runnable, copy-paste-into-a-console walkthrough.

## TRAPI server

```bash
python trapi_server.py --data-dir ~/tmp/csrgraph_data --graph translator_kg
# LMDB-only (no Elasticsearch):
python trapi_server.py --no-es --port 8000
```

Run `python trapi_server.py --help` for all options (`--es-host`, `--host`, `--port`, …).

## Project layout

| File | Description |
|------|-------------|
| `csrgraph_kgx.py` | `CSRGraph` — CSR topology, KGX loading, serialization, path queries |
| `metadata_db.py` | `MetadataBackend` interface + SQLite/DuckDB/LMDB/Elasticsearch/Hybrid backends |
| `trapi.py` | TRAPI query-graph engine over `CSRGraph` |
| `trapi_server.py` | FastAPI TRAPI server |
| `trapi_demo.py`, `usage_demo.py` | Usage examples |
| `bench_backends.py` | Metadata-backend benchmarks |
| `tests/` | pytest suite (`test_queries.py`, `test_trapi.py`) |

## Tests

```bash
pytest
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
