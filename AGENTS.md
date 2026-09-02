# csrgraph — working notes for AI coding agents

Memory-efficient CSR-backed knowledge graph for Biolink/Translator data, with KGX
loading, pluggable metadata backends, path-finding, and a TRAPI server.

## Answering free-text KG questions

The intended workflow: the user asks a natural-language question about the graph
(e.g. *"find paths connecting gene X to any disease"*, *"what is drug Y associated
with"*, *"how are A and B connected"*) and you translate it into calls on the helpers
in **`kg_query.py`**, then run them in the project venv.

Map the question to a helper:

- **"What is X associated with / linked to (of some type)?"** → `associations(g, X, target_category, max_hops=…)`
- **"How are X and Y connected?" / "path between X and Y"** → `connect(g, X, Y)`
- **"What are the direct neighbours of X?"** → `neighbors(g, X, category=…)`
- **"What CURIE is this name/symbol?"** → `resolve(text, category=…)`

`kg_query.py` is the convenience layer; `csrgraph_kgx.py` (topology + path-finding) and
`metadata_db.py` (metadata backends) are the lower-level libraries underneath it.

## Environment (always use the project venv)

All deps live in the project venv — always invoke Python through it:

```bash
.venv/bin/python ...
```

Create it with any CPython **>= 3.11**, then install the package with all optional
backends plus test tools:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]" pytest httpx
```

`-e ".[all]"` pulls in numpy, scipy, lmdb, elasticsearch, duckdb, fastapi, and (on
Python < 3.14) `zstandard`. On Python >= 3.14 the stdlib `compression.zstd` is used for
`.pkl.zst` / `.tar.zst` instead, so `zstandard` is intentionally not installed there.

> If you build the venv from a free-threaded (`Py_GIL_DISABLED`) interpreter, importing
> some C extensions prints a harmless `RuntimeWarning: The global interpreter lock (GIL)
> has been enabled to load module ...`. Filter it from output if it's noisy.

## Prerequisites (data + Elasticsearch)

This repo is **code only** — graph snapshots and ES indices are not committed. To run
queries you need, in the data dir:

- a CSR snapshot `<graph>.csrgraph.pkl.zst` (built from a KGX archive via
  `CSRGraph.from_kgx_archive(...)` then `.save(...)`), and
- the matching Elasticsearch indices `<graph>_nodes` / `<graph>_edges`, loaded by the
  Elasticsearch backend in `metadata_db.py`.

A running Elasticsearch server is required for name lookup and category/text search.

Upstream KGX archives come from **<https://kgx-storage.ci.transltr.io/releases/>** —
see [`docs/kgx-data-releases.md`](docs/kgx-data-releases.md) for the layout, how to find
the current release, the download-and-build recipe, and the two ES build traps that
surface as empty results rather than errors.

## Defaults

`kg_query.get_graph()` and the TRAPI server share these defaults (all overridable):

- **Graph:** `translator_kg_2026-07-19` (loads `<DATA_DIR>/translator_kg_2026-07-19.csrgraph.pkl.zst`).
- **Metadata backend:** Elasticsearch at `http://localhost:9200`, indices
  `translator_kg_2026-07-19_nodes` / `translator_kg_2026-07-19_edges`.
- **Data dir:** `~/tmp/csrgraph_data`.
- Override via env vars `GRAPH_NAME`, `DATA_DIR`, `CSRGRAPH_ES_HOST`, or via arguments to
  `get_graph(name=…, data_dir=…, es_host=…)`. Any graph stem present in the data dir
  works (e.g. small sample graphs `dgidb`, `ttd`).

The ES endpoint variable is **`CSRGRAPH_ES_HOST`**, not `ES_HOST`. The prefix is
the point: `ES_HOST` is a name other tools on the same machine set for their own
clusters, and inheriting it would silently point csrgraph at the wrong cluster —
which returns empty results, not an error. A bare `ES_HOST` is therefore *not*
honoured as a fallback, since that would reintroduce the leak; it is ignored with
a warning on stderr naming the replacement (`metadata_db.es_host_from_env`).

## Node subclassing is ON by default (semantic subtype links)

All `kg_query` query helpers default to `node_subclassing=True`: a node is expanded to
its semantic subtypes so edges attached to subtypes are also included. The recognised
"is-a" predicates are listed in `CSRGraph.SUBCLASS_PREDICATES`
(`biolink:subclass_of` → stored as `subclass_of`, and `rdfs:subClassOf`) and every
variant present in the graph is unioned — so subclassing works regardless of which
convention a given graph uses. Example: a parent disease term expands to its subtype
terms (e.g. *type 1/2*, *gestational*, …). Pass `node_subclassing=False`
(or `--no-subclassing` on the CLI) to disable.

## `kg_query` cheat-sheet

Values below are illustrative — substitute the entities from the user's question.

```python
import kg_query as kq
g = kq.get_graph()                                       # cached; default graph + ES

kq.resolve("<name or symbol>", category="biolink:Gene", graph=g)   # -> candidate CURIEs
kq.resolve_one("<disease name>", category="biolink:Disease", graph=g)

# 1-hop / multi-hop paths from an entity to any node of a category:
kq.associations(g, "<CURIE>", "biolink:Disease", max_hops=2, limit=500)

# shortest path(s) between two specific entities (both ends subtype-expanded):
kq.connect(g, "<CURIE A>", "<CURIE B>")

kq.neighbors(g, "<CURIE>", category="biolink:Disease")
print(kq.format_path(g, path))                           # "Name (CURIE) --[pred]--> Name (CURIE)"
```

CLI (subtype expansion on unless `--no-subclassing`; `--from`/`--to` accept a name or a CURIE):

```bash
.venv/bin/python kg_query.py resolve "<name>" --category biolink:Disease
.venv/bin/python kg_query.py assoc   --from "<entity>" --from-category biolink:Gene \
                                     --to-category biolink:Disease --max-hops 2 --limit 50
.venv/bin/python kg_query.py connect --from "<entity A>" --to "<entity B>" \
                                     --from-category biolink:Gene --to-category biolink:Disease
.venv/bin/python kg_query.py neighbors --of "<entity>" --of-category biolink:Gene \
                                       --category biolink:Disease
```

Tips:

- For "to *any* disease **or** phenotype", use category `biolink:DiseaseOrPhenotypicFeature`
  (the ancestor mixin both Disease and PhenotypicFeature nodes carry) to cover both at once.
- Pass a `category` to `resolve`/`--from-category` to disambiguate a symbol shared across
  entity types.

## HTML reports (optional)

`kg_report.py` runs an association query and renders a self-contained HTML page with an
interactive network graph plus grouped tables:

```bash
.venv/bin/python kg_report.py --from "<entity>" --from-category biolink:Gene \
    --to-category biolink:DiseaseOrPhenotypicFeature --max-hops 2 --out report.html
```

## Building a deployable release

`make_release.py` packages a KGX archive into an immutable, self-describing
release directory — the unit a deployment consumes:

```bash
.venv/bin/python make_release.py ~/tmp/csrgraph_data/dgidb.tar.zst \
    --version 2026-08-14 --graph-name dgidb --out-root ~/tmp/releases
```

```
2026-08-14/
  dgidb.csrgraph.pkl.zst
  dgidb.csrgraph.memmap/      # optional; --no-memmap skips it
  dgidb.metadata.lmdb/
  manifest.json               # written last
```

Everything is built in a staging directory and moved into place with one
`os.replace`, so a release appears atomically or not at all. This matters beyond
tidiness: `LMDBMetadataBackend.build()` starts by `rmtree`-ing its target, so a
build aimed at a live directory would destroy the running store before producing
a replacement.

Before publishing it recounts the archive and refuses to publish if the stores do
not match (`--no-gate-completeness` to skip the extra pass; `--gate-corpus` to
also run `tests/test_corpus.py` against the candidate).

`manifest.json` also carries `biolink_version`, copied from the archive's
`graph-metadata.json`. Predicate expansion is pinned to it, so expansion uses the
same model the data was normalised with — see
[`docs/kgx-data-releases.md`](docs/kgx-data-releases.md) for why that must be
re-checked whenever the source release moves. `--no-gate-completeness` leaves it
`null`, since that flag skips the pass that reads it.

`manifest.json` carries `store_format_version`, which is **not cosmetic**: edge
metadata is keyed `(subject, predicate, object, qualifier_fingerprint)`, and a
store built before that key is *silently* unreadable by current code — prefix
scans match nothing, so qualifier-constrained queries return empty with no error.
`trapi_server.py` reads the manifest at startup and **refuses to serve** on a
mismatch; `GET /version` reports the deployed release and readiness. A directory
without a manifest still works, unchecked, so hand-built stores keep serving.

Serve a release with the store it ships:

```bash
DATA_DIR=~/tmp/releases/2026-08-14 GRAPH_NAME=dgidb NO_ES=1 \
    .venv/bin/python trapi_server.py
```

`kg_query.get_graph()` takes `backend="auto"|"lmdb"|"es"|"hybrid"` and defaults to
`auto`, preferring the LMDB store when the directory has one. `resolve`/`resolve_one`
need `backend="es"` or `"hybrid"` — full-text lookup has no LMDB equivalent.
`"hybrid"` requires both stores and is what a long-lived server wants: point
lookups go to LMDB (400× faster — 0.004 ms vs 1.6 ms), full-text and large
filtered scans go to ES. Serving opens LMDB read-only, so a release directory is
never mutated and read-only mounts work.

Plan and remaining work: `docs/production-release-plan.md` (F1–F6 implemented;
the delivery layer is not).

## Tests

```bash
.venv/bin/python -m pytest -q          # data-free synthetic test suite
```

`tests/test_corpus.py` additionally runs the
[HelmsDeep](https://github.com/TranslatorSRI/HelmsDeep) TRAPI corpus — 12 query
types across its retriever, shepherd and pathfinder segments — against a **real**
graph. It skips wherever the data or the harness is absent (CI included), and a
skip is easy to mistake for a pass, so check for `5 passed` rather than a clean
exit.

Fetch the harness once (self-contained: stdlib imports only, no `helmsdeep`
package needed):

```bash
BASE=https://raw.githubusercontent.com/TranslatorSRI/HelmsDeep/main/helmsdeep
curl -sL -o ~/tmp/trapi_corpus.py $BASE/trapi_corpus.py
curl -sL -o ~/tmp/curie_list.json $BASE/curie_list.json   # 991 MONDO long-tail pool
```

`curie_list.json` must sit **beside** `trapi_corpus.py` — it is read relative to
the module's own directory. Without it the module still imports but silently
falls back to a 4-disease pool, so the long-tail queries stop being long-tail.

Then point at a built graph and put both on the path:

```bash
DATA_DIR=~/tmp/releases/2026-07-19 GRAPH_NAME=translator_kg_2026-07-19 PYTHONPATH=~/tmp \
    .venv/bin/python -m pytest tests/test_corpus.py -q
```

It asserts invariants rather than answer counts — every supported shape returns
something, bindings satisfy their queried categories, `query_id` marks exactly the
subclass-expanded nodes, and a capped result declares itself. Counts would turn it
into a tripwire for data changes rather than code changes.

## TRAPI server (optional)

```bash
.venv/bin/python trapi_server.py       # defaults: translator_kg_2026-07-19, ES on :9200, port 8000
```

## MCP server (optional)

`mcp_server.py` exposes the graph to agentic clients. It needs the optional `mcp`
extra (`pip install -e ".[mcp]"`, pulled in by `[all]`); without it the module
exits with an install hint rather than a traceback.

```bash
DATA_DIR=~/tmp/releases/2026-07-19 GRAPH_NAME=translator_kg_2026-07-19 \
    .venv/bin/python mcp_server.py     # stdio transport (default)

claude mcp add csrgraph -- /abs/path/.venv/bin/python /abs/path/mcp_server.py
```

stdio is the default and needs no lifecycle management — the client spawns its own
copy, so a server started by hand in a terminal is reachable by nobody.
`--http [--host --port]` serves streamable HTTP instead, which is worth it when
several sessions run at once: the graph is ~1.4 GB resident, so N stdio clients
cost N times that while N HTTP clients cost it once. The trade is that `_LOCK` is
shared too, so a slow query in one session delays the others.

```bash
.venv/bin/python mcp_server.py --http --port 8791
claude mcp add --transport http csrgraph http://127.0.0.1:8791/mcp
```

The port default is 8791, not 8000 — 8000 is `trapi_server.py`'s, and the clash is
silent until one of them fails to bind. Stateless by default, so independent
sessions need no affinity; loopback-only, and there is no auth.

Tools: `resolve_entity`, `find_associations`, `connect_entities`, `list_neighbors`,
`graph_query`, `describe_schema`, `graph_info`. Entity arguments accept a **CURIE
or a free-text name** (resolved via `resolve_one`), and paths come back as compact
`Name (CURIE) --[pred]--> Name` strings, because an agent pays tokens for every
result. Result caps are deliberately small (`MAX_HOPS_CEILING`, `LIMIT_CEILING`)
and a clamped result sets `truncated: true` so a subset is never read as
exhaustive.

### `graph_query` — full TRAPI expressiveness, no TRAPI format

`kg_pattern.py` translates a compact triple pattern into a TRAPI QueryGraph and
runs it through `trapi.match`, so branching, cycles, predicate unions and
qualifier constraints are all reachable without authoring TRAPI JSON:

```python
[["CDK2", "affects", "?d:Disease"], ["?drug", "treats", "?d"]]   # a branch
```

**A repeated `?variable` is one node** — that is what makes branching
expressible, and `tests/test_kg_pattern.py` pins it, because allocating two nodes
by mistake still returns plausible, wrong answers. Node terms: CURIE, free-text
name, `?var`, `?var:Category`, `biolink:Category`, `*`. Edge terms: `null`,
`"affects"`, `["affects","treats"]`, or
`{"predicate": …, "object_direction_qualifier": "increased"}` (short qualifier
names are aliased from `trapi._QUALIFIER_TYPE_TO_FIELD`, so the two cannot
drift).

Results project to `columns`/`rows` of the requested variables only — never a
knowledge graph. `trapi.match()` exists for exactly this: `trapi.query()` is now
`_build_message(match(...))`, and `_build_message` is what inflates every bound
node into KG entries with full metadata. Skipping it is the difference between
compact and verbose, and the metadata lookups are the expensive part.

Two guards worth keeping:

- **`require_pinned`** rejects an all-variable pattern, which would give the
  matcher no anchor and degenerate into scanning the graph. It runs *after*
  translation, so a malformed pattern reports its real error and a name-pinned
  node still counts.
- **`expand_predicates`** derives the expander's terms from the query.
  `BiolinkExpander.from_bmt()` resolves *only the terms passed to it*, so calling
  it with no arguments builds an expander that silently expands nothing.

`describe_schema` lists the graph's actual 63 predicates (most frequent first)
and its categories, so a model authors patterns from the real vocabulary instead
of guessing. Categories need an ES terms aggregation and come back `null` on
LMDB-only, rather than being guessed from the Biolink model.

Two behaviours are load-bearing, not incidental:

- **Calls are serialised** behind one lock. LMDB reads under concurrent threads
  collapse to ×0.03 of single-thread throughput — *less* aggregate work than one
  thread — from GIL convoying over cursor steps
  (`docs/concurrency-and-scalability-2026-07-19.md`). liblmdb and py-lmdb are both
  thread-safe; the GIL is the problem. MCP clients issue parallel tool calls
  freely, so removing the lock makes bursts slower, not faster.
- **ES is optional.** Startup prefers `backend="hybrid"` and falls back to
  LMDB-only if ES is unreachable, so a release directory works out of the box.
  Only `resolve_entity` then fails, with a message telling the caller to pass
  CURIEs; `graph_info().resolve_available` reports which mode is live.

Requires ES for `resolve_entity` only, so the local index must be built for the
*same* graph name (`<graph>_nodes`), not just any graph.
