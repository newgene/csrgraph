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

## Defaults

`kg_query.get_graph()` and the TRAPI server share these defaults (all overridable):

- **Graph:** `translator_kg` (loads `<DATA_DIR>/translator_kg.csrgraph.pkl.zst`).
- **Metadata backend:** Elasticsearch at `http://localhost:9200`, indices
  `translator_kg_nodes` / `translator_kg_edges`.
- **Data dir:** `~/tmp/csrgraph_data`.
- Override via env vars `GRAPH_NAME`, `DATA_DIR`, `ES_HOST`, or via arguments to
  `get_graph(name=…, data_dir=…, es_host=…)`. Any graph stem present in the data dir
  works (e.g. small sample graphs `dgidb`, `ttd`).

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

## Tests

```bash
.venv/bin/python -m pytest -q          # data-free synthetic test suite
```

## TRAPI server (optional)

```bash
.venv/bin/python trapi_server.py       # defaults: translator_kg, ES on :9200, port 8000
```
