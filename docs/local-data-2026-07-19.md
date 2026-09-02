# Local benchmark dataset — Translator KG `2026-07-19`

The shared dataset for csrgraph vs. gandalf comparison. Both systems were built
from **the same source archive**, so any difference in results or timing is
attributable to the implementations rather than the data.

## Source

| | |
| --- | --- |
| URL | `https://kgx-storage.ci.transltr.io/releases/translator_kg/latest/translator_kg.tar.zst` |
| Version | **2026-07-19** (`last-modified: Sun, 19 Jul 2026 05:42:38 GMT`) |
| Size | 3,244,164,916 bytes (3.02 GB) |
| SHA-256 | `a91e75201b53962624063091b57ae0f210dd651e6f580cdc68ca2ab59261a879` |
| Local copy | `~/tmp/csrgraph_data/translator_kg_2026-07-19.tar.zst` |

Archive members: `nodes.jsonl` (1,077,818,157 B), `edges.jsonl`
(24,598,537,067 B), `graph-metadata.json` (3,765,604 B).

Named with a version suffix rather than the bare `translator_kg` default, so the
previous April dataset is untouched. Point tools at it with
`CSRGRAPH_GRAPH_NAME=translator_kg_2026-07-19`, or pass
`--stem` / `get_graph(name=...)`.

## Graph shape — the two implementations agree

Built independently, from the same archive:

| | csrgraph | gandalf |
| --- | --- | --- |
| Unique nodes | 1,675,087 | 1,675,087 |
| Predicates | 63 | 63 |
| Raw edge triples | 28,925,258 | 28,925,258 |

Nodes come from edge endpoints in both, so the ~84k nodes in `nodes.jsonl` with
no edges (1,759,470 total node records) are absent from both graphs.

### Duplicate triples: the one place the two differ

The archive contains **819,741 duplicate `(subject, predicate, object)` triples**:

| Count | Value |
| --- | --- |
| Raw triples | 28,925,258 |
| Distinct `(subject, predicate, object)` | 28,105,517 |
| Distinct `(subject, object)` pairs | 27,434,551 |

**csrgraph collapses duplicates** (its per-predicate CSR matrices are built from
COO, which merges repeated coordinates), so it holds 28,105,517 edges.
**gandalf keeps them** — `fwd_offsets[-1]` is 28,925,258. This is visible per
node: `NCBIGene:10425` has out-degree **399 in csrgraph, 403 in gandalf**. Any
edge-count or degree comparison has to account for it.

Independently confirmed: the Elasticsearch edge index holds exactly 28,105,517
docs, matching csrgraph's distinct-triple count, because ES doc IDs are
deterministic on the triple.

The `(subject, object)` row also matters for csrgraph internals: **670,966 node
pairs carry more than one predicate**, which is why `csr_merged` +
`edge_predicate_ids` (one representative predicate per pair) cannot serve
wildcard neighbour expansion.

## csrgraph artifacts

Under `~/tmp/csrgraph_data/`:

| Artifact | Size | Build time |
| --- | --- | --- |
| `translator_kg_2026-07-19.csrgraph.pkl.zst` | 34.4 MB (606 MB uncompressed) | 190 s parse + 1 s save |
| `translator_kg_2026-07-19.csrgraph.memmap/` | 745 MB | 3 s |
| `translator_kg_2026-07-19.metadata.lmdb/` | 23 GB | 2873 s |

Snapshot built with `node_metadata_fields` left at its default. **Note:** the
default is `None`, which *skips* node metadata — despite the docstring claiming
`None` keeps all of it. Passing `[]` does load it and costs ~2.2 GB resident;
metadata belongs in the backends, so the lean snapshot is correct.

Loads from memmap in ~300–450 ms; in-memory footprint ~1.0 GB
(`csr_by_relation` 536.6 MB, `id_maps` 387.9 MB, `csr_merged` 137.2 MB,
`edge_predicate` 26.2 MB, `component_labels` 6.4 MB).

## Elasticsearch

Server **9.5.0** in a container, matched to the installed `elasticsearch`
Python client (9.5.0 — a 9.x client enforces major-version compatibility, so an
8.x server is not usable):

```bash
docker run -d --name csrgraph-es -p 9200:9200 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  -e xpack.security.http.ssl.enabled=false \
  -e "ES_JAVA_OPTS=-Xms3g -Xmx3g" \
  -e action.destructive_requires_name=false \
  -e cluster.routing.allocation.disk.threshold_enabled=false \
  --memory 6g \
  docker.elastic.co/elasticsearch/elasticsearch:9.5.0
```

The Podman VM needs headroom for this: at its default 2 GiB the JVM cannot
start (exit 70 on an oversized heap, exit 137 OOM-kill otherwise). Sized to
7.45 GiB here.

| Index | Docs | Store |
| --- | --- | --- |
| `translator_kg_2026-07-19_nodes` | 1,759,470 | 248.2 MB |
| `translator_kg_2026-07-19_edges` | 28,105,517 | 3.8 GB |

Indexed in 1207 s at ~28–35k docs/s. `refresh_interval` was set to `-1` for the
load and restored to `1s` afterwards — `_INDEX_SETTINGS` does not set it, so a
default build pays continuous segment flushes across ~30M docs.

## gandalf artifacts

Under `~/tmp/gandalf_data/`:

| Artifact | Size |
| --- | --- |
| `kgx_2026-07-19/` (extracted `nodes.jsonl` + `edges.jsonl`) | 24 GB |
| `graph_2026-07-19_mmap/` | 29 GB |

gandalf's `KGXJsonlSource` reads **plain uncompressed** jsonl (`open(path)`), so
the archive must be extracted first. Built with:

```bash
cd ~/tmp/gandalf_latest   # ranking-agent/gandalf @ 82a1fb2 (v1.0.0, 2026-07-21)
.venv/bin/python -m scripts.build_graph \
  --edges ~/tmp/gandalf_data/kgx_2026-07-19/edges.jsonl \
  --nodes ~/tmp/gandalf_data/kgx_2026-07-19/nodes.jsonl \
  --output ~/tmp/gandalf_data/graph_2026-07-19_mmap
```

Build ran ~12 min: Pass 1 vocabulary 55 s, Pass 2 arrays/property stores, meta-KG
+ SRI testing data 103 s (3368 unique triples, 42 categories), save 35.65 s. Its
LMDB store auto-resized 8 → 16 → 32 GB during Pass 2. Loads via `load_mmap` in
2.63 s. Directory holds the dual-CSR layout (`fwd_targets`/`fwd_predicates`/
`fwd_offsets` plus `rev_sources`/`rev_predicates`/`rev_offsets`/`rev_to_fwd`),
interned property pools, and four LMDB stores.

### Installing gandalf for a build

Its declared core dependencies (`bmt`, `lmdb`, `msgpack`, `numpy`) are **not
sufficient to import the package** — three `[server]`-only modules are imported
unconditionally by core code:

| Module | Imported by | Reached via |
| --- | --- | --- |
| `orjson` | `gandalf/normalize.py`, `sources/kgx_jsonl.py` | the KGX build path |
| `pydantic_settings` | `gandalf/config.py` | `biolink.py` → `search/lookup.py` → `__init__.py` |
| `httpx` | `plugins/literature_cooccurrence_annotator.py` | `plugins/__init__.py` → `__init__.py` |

So an offline graph build still needs `pip install -e ".[server]"`. Pinned
Biolink Model version 4.3.2; `settings.infores` defaults to
`infores:dogpark-tier0`.

## Backend parity

`check_backends.py` runs the same queries through LMDB and ES on this snapshot
and compares results:

```bash
.venv/bin/python check_backends.py --stem translator_kg_2026-07-19
```

Current status: **LMDB and ES agree on every compared query** —
`nodes_by_category`, category NodeSpec resolution, 1/2/3-hop associations,
`knowledge_level` edge filters, a 3-hop pinned-both-ends connect, and the
truncation signal.

Timing (same data, same snapshot; ES is a local container, so these are
lower bounds on network cost):

| Query | LMDB | ES |
| --- | --- | --- |
| `nodes_by_category(Disease)` → 51,704 | 0.034 s | 4.65 s |
| `nodes_by_category(Gene)` → 251,712 | 0.185 s | 2.37 s |
| `nodes_by_category(SmallMolecule)` → 1,056,957 | 0.800 s | 77.4 s |
| 1-hop associations | 0.155 s | 0.037 s |
| 2-hop associations | 0.817 s | 0.854 s |
| 3-hop associations | 0.219 s | 0.631 s |
| edge filter (`knowledge_level`) | 0.008 s | 0.171 s |
| 3-hop pinned connect | 0.373 s | 0.028 s |

**LMDB dominates whole-category enumeration** — an index prefix scan versus ES
paging 10k hits at a time (1,056,957 ids is ~106 round-trips). Traversal-shaped
queries are comparable, and ES sometimes wins. For a category-only first
NodeSpec against ES, materialising every CURIE is the remaining bottleneck worth
addressing.
