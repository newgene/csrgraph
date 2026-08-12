# csrgraph vs. `ranking-agent/gandalf` — August 2026 update

A refreshed comparison of this repository's CSR-backed knowledge-graph implementation with
[**GANDALF**](https://github.com/ranking-agent/gandalf) — the RENCI / `ranking-agent`
team's CSR graph for fast Biolink/Translator pathfinding.

> **Snapshots compared**
> - **gandalf**: `main` @ `82a1fb2` (2026-07-21, *"Bump major version!"*) — PyPI
>   `gandalf-csr` **v1.0.0**, author Max Wang. Previous report covered v0.3.9.
> - **csrgraph**: `main` @ `dc54bc4` (2026-06-08) — unchanged *as of writing*.
>
> This supersedes [`COMPARISON.md`](../COMPARISON.md) (June 2026), which remains accurate for v0.3.9. The
> deep-architecture verdicts there still hold: `gandalf/graph.py`, `query_planner.py`,
> `search/path_finder.py`, `search/query_edge.py`, `search/reconstruct.py`,
> `search/path_arrays.py`, the node/LMDB stores and the mmap serialization format are all
> **byte-for-byte unchanged** between the two snapshots. Everything new is in ingest,
> subclass fidelity, transport, and a brand-new explorer UI.

> ### ⚠️ Superseded in places — read this first
>
> This document is a **design/code comparison written before either system was
> measured**. Both premises above have since moved:
>
> 1. **csrgraph has advanced.** Branch `batch-match-path-metadata-filtering`
>    carries 13 commits that change several performance claims here: batched
>    metadata filtering, vectorized frontier expansion, admissible reachability
>    pruning, truncation reporting, and a backend category index.
> 2. **Both systems were then benchmarked on identical data** (Translator KG
>    `2026-07-19`), which **contradicted two conclusions below** — most importantly
>    the per-worker memory comparison, which turned out to be backwards.
>
> Where this file and the measurements disagree, **the measurements win**:
>
> - [`docs/benchmarks-vs-gandalf-2026-07-19.md`](benchmarks-vs-gandalf-2026-07-19.md)
>   — head-to-head correctness and speed
> - [`docs/concurrency-and-scalability-2026-07-19.md`](concurrency-and-scalability-2026-07-19.md)
>   — threading, per-worker memory, scaling strategy
> - [`docs/local-data-2026-07-19.md`](local-data-2026-07-19.md) — the shared dataset
>
> Corrections are marked **[CORRECTED]** inline below.

---

## What changed since the June 2026 report

### gandalf 0.3.9 → 1.0.0

| Change | Where | Why it matters here |
| ------ | ----- | ------------------- |
| **Major version bump to 1.0.0** + GitHub Actions PyPI publish workflow | `pyproject.toml`, `.github/workflows/publish-pypi.yml` | Signals API stability, though the PyPI classifier is still `Development Status :: 3 - Alpha`. |
| **Mithrandir** — a new interactive graph-explorer GUI, ~2.6k lines | `mithrandir/` (`server.py` 581 L, `app.js` 1035 L, `style.css` 783 L, `index.html`, `test_harness.mjs`) | **The biggest strategic change.** gandalf now ships an exploration/visualisation front-end, entering what was csrgraph's clearest differentiator. |
| **Pluggable ingest sources** — `GraphSource` ABC + `KGXJsonlSource` + `MongoSource`, with a validated normalized-record contract | new `gandalf/sources/` (`base.py` 202 L, `kgx_jsonl.py`, `mongo.py`); `loader.py` refactored (−443 L of inline logic) | gandalf gains a *pluggability axis* — but on **ingest**, orthogonal to csrgraph's pluggable **serving/metadata** backends. |
| **Normalization extracted** into its own module | new `gandalf/normalize.py` (317 L) | Owns KGX→internal restructuring (TRAPI `sources`/`qualifiers`/`attributes`, `biolink:` prefixing, `category`→`categories`). **Despite the name, this is not entity resolution** — see the caveat below. |
| **Subclass-inference correctness overhaul** (3 commits) | `gandalf/search/lookup.py` (+62/−33) | Support graphs are now emitted **even when a direct superclass↔superclass edge exists** (matching Tier 1); subclass edges attach **per derivation** so distinct inferred edges no longer collapse (#39); new `_append_edge_binding` dedup guard. |
| **Subclass provenance** — composite inferred edges attributed to `infores:obie` (primary) with gandalf as aggregator | new `settings.subclass_inference_infores` | Ontology-based entailments are now honestly attributed rather than credited to gandalf's own graph. |
| **Qualifier / Biolink fidelity** | `loader.py`, meta-KG build, `config.py` | Biolink Model pinned to the Tier 1 driver's version (4.3.2); list-valued qualifier values normalized to scalars; `qualified_predicate` values expanded to Biolink descendants. |
| **zstd request *and* response compression** as a raw ASGI middleware; max request body raised to 1 GB | `gandalf/compression.py`, `server.py` | Aimed squarely at very large TRAPI payloads (batch/MCQ queries and huge knowledge-graph responses). |
| **OpenTelemetry rework** | new `gandalf/otel.py`; post-fork per-worker init; explicit baggage propagation + PK validation | Correct tracing under gunicorn's fork model. |
| **Deployment identity flip** | `config.py`: `infores` `infores:gandalf` → **`infores:dogpark-tier0`**; `otel_service_name` → `dogpark-tier0` | gandalf is now being deployed as Translator's **"Dogpark Tier 0"** service, not as a generically-named KP. Hard-coded infores literals were removed from docs/tests in favour of `settings.infores`. |
| **`large_result_threshold` 50,000 → 10,000,000** | `config.py` | Response auto-dehydration is now effectively **off by default** — an operational bet that full hydration is affordable, reversing the June default. |
| `gandalf-build-mongo` CLI + `mongo` extra (`pymongo`) | `pyproject.toml`, `scripts/build_graph_mongo.py` | A second production ingest path that never loads BMT at build time. |

### csrgraph

**No code changes.** The only new artifact is `docs/production-release-plan.md`, a written
(not yet implemented) plan for a production release story: a `make_release.py` packaging
step, an immutable `manifest.json` with per-artifact SHA-256, a `GET /version` +
readiness endpoint on `trapi_server.py`, a read-only LMDB open option, and two delivery
models (systemd updater with atomic symlink swap / auto-rollback, and Kubernetes rolling
update). This matters to the comparison because **artifact versioning and health-gated
swap is one dimension where csrgraph is now ahead in design intent** — gandalf serves a
build-time `metadata.json` verbatim from `/metadata` but ships no integrity manifest,
version endpoint, or version-gated readiness probe.

---

## Overview — how the positioning has shifted

Both projects still independently target the **same problem**: *a Compressed Sparse Row
graph for fast pathfinding over Translator/Biolink knowledge graphs*. The June framing —
**gandalf for throughput at fixed scale in production, csrgraph for flexibility,
resolution, and exploration** — is still broadly right, but **the exploration half is now
contested**.

**gandalf** remains the RENCI/`ranking-agent` team's production Translator service:
Plater-compatible TRAPI 1.5 endpoints, Dockerfile, gunicorn config, OTel/Jaeger tracing,
Automat heartbeat registration, async query with callbacks, rate limiting, a plugin
system, and a ~30-file pytest suite (now including 375 lines of new source-contract tests
and ~1,000 lines of subclass tests). v1.0.0 plus the `dogpark-tier0` identity says this is
now a named tier in a live deployment. And with **Mithrandir** it has an interactive
front-end for the first time.

**This repo** is still the leaner, more general library: scipy-backed graph algorithms, a
pluggable multi-backend metadata layer, **built-in Elasticsearch name resolution**,
single-file portable snapshots, natural-language query helpers, and HTML reports.

**Net shift:** csrgraph's differentiators have narrowed from three to two-and-a-half.
What remains genuinely distinctive:

1. **Algorithmic breadth** — `shortest_path`, `all_shortest_paths`, `all_paths`,
   `paths_by_predicate_sequence`. Mithrandir does *not* close this: it walks **one triple
   at a time** and has no pathfinding at all.
2. **Built-in entity resolution** — still absent from gandalf, and Mithrandir *proves* it:
   its node-name search proxies the external **SRI Name Resolver**
   (`GANDALF_NAME_RESOLVER`), and node info cards pull from Wikipedia.
3. **Backend pluggability + snapshot portability** — half-contested: gandalf now has
   pluggable *ingest*, csrgraph has pluggable *serving*.

What csrgraph no longer uniquely owns: **an interactive way to look at the graph.**

### Caveat: `gandalf/normalize.py` is *not* entity resolution

Worth stating explicitly because the filename invites the wrong conclusion. gandalf's new
`normalize.py` restructures **raw KGX records at build time** into gandalf's internal
form. It does no CURIE equivalence, no clique merging, no name→CURIE lookup. gandalf still
delegates identifier normalization and name resolution to upstream Translator services
(NodeNormalizer, Name Resolver). csrgraph's `kg_query.resolve` /
`ElasticsearchBackend` remain a capability gandalf has **no in-repo equivalent for**.

---

## Target use cases (unchanged)

The rest of this comparison should be read against this operational profile:

- **Multi-hop graph queries** — typically 2–3 hops (e.g. *drug → gene → disease*). Both
  materialize the full set of connecting paths, not just one shortest path.
- **Property filtering** — constraints on **edge** properties (predicate, qualifiers,
  knowledge level, sources) and/or **node** properties (category, degree, …), on both
  endpoints *and* intermediates, applied *during* traversal to contain path explosion.
- **Relatively high concurrent access** — many simultaneous queries against a shared
  read-only graph; per-query latency and workers-per-host both matter.
- **Scalability to a large source KG** — full Translator KG ≈ **10M nodes / 38M edges**.

---

## Core CSR data structure — still the biggest divergence

**Unchanged in both repos since June.** `gandalf/graph.py` (1,526 lines) has not been
touched in this window.

|                | **gandalf** (`gandalf/graph.py`)                                              | **this repo** (`csrgraph_kgx.py`)                                                        |
| -------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Backing store  | **Hand-rolled flat numpy arrays** — no scipy                                  | **`scipy.sparse.csr_matrix`**                                                            |
| Layout         | **Dual CSR**: forward (`fwd_targets`/`fwd_predicates`/`fwd_offsets`) *and* reverse (`rev_sources`/`rev_predicates`/`rev_offsets` + `rev_to_fwd`) | **Per-predicate** dict of `uint8` CSR matrices + a merged `csr_merged` with an aligned `edge_predicate_ids` array |
| Predicate      | `int32` column parallel to the edge arrays; filtering = boolean mask          | Predicate is its own matrix; filtering = pick the matrix                                 |
| Direction      | Both directions materialized (≈2× topology RAM) for fast bidirectional search | Forward + scipy transpose on demand; weakly-connected-component labels cached            |
| Prefix         | predicates kept as `biolink:` strings, interned to ids                        | `biolink:` prefix stripped at ingest, restored on output                                 |

gandalf's design is optimized for one access pattern — **vectorized bidirectional k-hop
expansion at 10M-node/38M-edge scale** — paying for it with explicit reverse arrays plus
an `int64` offset array and a `rev_to_fwd` index for O(1) reverse→forward property
lookups. This repo leans on scipy's mature primitives (`connected_components`,
`shortest_path`, `csgraph`) and gets predicate-filtered traversal essentially "for free"
via per-predicate matrices — at the cost of a scipy dependency and many small matrices.

---

## Ingest & loading — new in this update

This is a genuinely new axis of comparison.

**gandalf** now defines a formal ingest contract (`gandalf/sources/base.py`):

- `GraphSource` is an ABC with three methods — `iter_edge_triples()` (cheap pass 1, for
  vocabulary collection), `iter_edges()` and `iter_nodes()` (pass 2, fully normalized).
- Implementations must be **re-iterable** and preserve a documented **ordering invariant**
  (edge *i* from `iter_edges()` must correspond to position *i* in `iter_edge_triples()`) —
  the docstring warns that a mismatch *"silently corrupts the graph."*
- Every yielded record is **validated** against a documented normalized schema
  (`validate_normalized_edge` / `validate_normalized_node`), raising
  `SourceValidationError` on violation.
- Two concrete sources: `KGXJsonlSource` (normalizes via `gandalf.normalize`) and
  `MongoSource` (documents pre-normalized upstream, sorted by `_id` for stable ordering,
  `pymongo` imported lazily). A pure-Mongo build **never loads BMT or fetches the Biolink
  schema** — a meaningful build-time saving, at the cost of trusting the upstream pipeline's
  Biolink version.

**This repo** loads from a KGX archive via `CSRGraph.from_kgx_archive(...)` then
`.save(...)`, with metadata built separately by a `MetadataBackend.build()`. There is no
source abstraction and no record-contract validation layer — KGX is the one input format.

| Dimension | **gandalf** | **this repo** |
| --------- | ----------- | ------------- |
| Input formats | KGX jsonl **+ MongoDB** (pluggable via `GraphSource`) | KGX archive only |
| Record validation | Explicit schema validation with a typed error | Implicit / best-effort at parse time |
| Normalization | Isolated module (`normalize.py`), BMT-derived qualifier field set with a static fallback | Inline in the loader; `biolink:` prefix stripping |
| BMT at build time | Required for jsonl; **skipped entirely** for Mongo | Never required |
| Release/versioning of the built artifact | `metadata.json` served verbatim from `/metadata`; no integrity manifest | None today; `docs/production-release-plan.md` specifies a SHA-256 manifest + `/version` + readiness gate |

**Assessment:** gandalf's source layer is the better-engineered ingest story and directly
serves the reality that Translator data arrives from more than one place. csrgraph's is
simpler and adequate for KGX-only use; the ordering-invariant hazard gandalf documents is
one csrgraph avoids by construction.

---

## Property / metadata storage (unchanged)

**gandalf** splits edge properties into two tiers:

- **Hot path** — `EdgePropertyStore`: qualifiers + sources interned into pools with
  `int32` index arrays, exploiting the very high dedup ratio (~10K unique qualifier
  combos, ~50 unique source configs). Returned dicts are zero-GC pool references.
- **Cold path** — `LMDBPropertyStore`: attributes/publications, msgpack-encoded, keyed by
  edge index, touched only during response enrichment.

Nodes live in an LMDB `NodeStore` (id↔idx + properties); there is also a plugin-owned
`TraversalMetadataStore` and an LMDB-backed edge-ID store. Deliberate memory engineering
for a fixed, huge production graph.

**This repo** keeps topology and metadata cleanly separated behind a pluggable
`MetadataBackend` with **five** implementations (`metadata_db.py`): SQLite, DuckDB, LMDB,
**Elasticsearch**, and Hybrid. The Elasticsearch backend provides **full-text
name/symbol → CURIE resolution** (`kg_query.resolve`), which gandalf still has no
in-repo equivalent for.

---

## Path-finding (unchanged)

**gandalf** has **two distinct path-finding surfaces, and only one of them is 3-hop-bound**:

1. **A fixed-arity direct-traversal API** (`search/path_finder.py`) — `do_one_hop` (1 edge)
   and `find_3hop_paths_with_properties` / `find_3hop_paths_filtered` /
   `find_mechanistic_paths` (exactly 3 edges: a vectorized bidirectional search doing
   forward 2 hops + backward 1 hop with an `np.isin` intersection, started from the
   higher-degree endpoint). There is **no hop-count parameter** — no 2-hop, no 4-hop
   variant. This is a hand-tuned kernel for the single most common Translator shape, not a
   general path API.
2. **The TRAPI query-graph engine** (`search/lookup.py`, `reconstruct.py`, `query_edge.py`,
   `expanders.py`) — **arbitrary hop count**. `lookup()` loops
   `while len(subqgraph["edges"]) > 0`, asking the planner for the next-best qedge and
   solving one edge at a time until the query graph is exhausted; `PathArrays` is generic
   over its `qnode_to_col` / `qedge_to_col` maps (arbitrary width), and `reconstruct_paths`
   does a multi-way join under `compute_join_order`. **No hop-count ceiling is validated
   anywhere** in `validation.py` or the search package, so an N-edge query graph is
   supported for any N. The pipeline is three-stage: **topology search → filtering → batch
   enrichment**.

So gandalf supports >3 hops — just via TRAPI query graphs rather than a `max_hops`
argument, and without the vectorized fast path (a 4+-edge query falls back entirely to the
pure-Python edge-at-a-time solver). Its real constraint is structural, not numeric: **every
qedge must have ≥1 pinned endpoint when it is solved** (`query_edge.py` raises *"Both nodes
unpinned"*). Pins propagate as each edge resolves, so a long chain pinned at one or both
ends does work — but intermediate cardinality compounds hop over hop with no `max_depth`-style
brake, which is what `GANDALF_MAX_PATH_LIMIT` and `gandalf-diagnose` exist to contain.

**This repo** offers general graph algorithms on `CSRGraph` (`csrgraph_kgx.py`):
`shortest_path`, `all_shortest_paths`, `all_paths` (DFS), `paths_by_predicate_sequence`,
and a pattern-based `match_path` — all scipy-backed, with component-cache reachability
pruning. A friendlier `kg_query` layer (`resolve` / `associations` / `connect` /
`neighbors`) sits on top, plus HTML reports (`kg_report.py`).

**Hop depth is a first-class, unbounded parameter here**, which is the real difference:
`shortest_path` / `all_shortest_paths` find paths of *any* length (scipy BFS, no ceiling);
`all_paths(max_depth=k)` takes arbitrary `k` and **defaults to `max_depth=None`, i.e.
unbounded**; `paths_by_predicate_sequence` accepts a predicate template of any length;
`match_path` accepts any odd-length `path_spec >= 3` (≥1 hop) with no upper bound; and
`kg_query.associations(..., max_hops=N)` takes any `N >= 1`. Only `match_path` self-limits,
via an intermediate frontier cap of `max(limit * 50, 50_000)` — the raw DFS methods have no
such brake, so the flip side of that unbounded default is genuine explosion risk.

### Which is "broader" depends on the axis

| Axis | **gandalf** | **this repo** |
| ---- | ----------- | ------------- |
| Max hops, TRAPI/pattern query | Unlimited (N qedges, planner-ordered) | Unlimited (N-hop `path_spec` / `max_hops`) |
| Max hops, direct traversal API | **Fixed: 1 or exactly 3** | Unlimited, and parameterized |
| Hop count as an explicit argument | ❌ expressed by building an N-edge query graph | ✅ `max_hops=` / `max_depth=` |
| Vectorized fast path | ✅ but **only** at the 3-edge shape | ❌ (scipy C for shortest-path/components only) |
| General shortest path, any length | ❌ not exposed at all | ✅ |
| Structural precondition | ≥1 pinned endpoint **per qedge** | Only the *first* node must be pinned |
| Depth safety brake | `GANDALF_MAX_PATH_LIMIT` + diagnose tooling | `match_path` frontier cap only; raw DFS uncapped |

Net: **neither is limited to 3 hops for real queries.** gandalf is broader in *planning
sophistication* at depth (cost-based edge ordering keeps a deep chain's intermediate
cardinality down) but narrower in *interface* — deep search is only reachable by
constructing a query graph, and its one hand-optimized kernel exists solely at 3 edges.
csrgraph is broader in *interface and algorithm coverage* — depth is a plain argument, and
it owns the general shortest-path/all-paths family gandalf never exposes at any length —
but it walks strictly left-to-right with no cost-based reordering, so a deep query from a
high-degree start degrades faster.

---

## Query model & supported query types (unchanged)

**gandalf — a Strider-style, cost-planned, edge-at-a-time constraint solver.** It does not
"find paths"; it *solves a TRAPI query graph*. The planner
(`query_planner.py::get_next_qedge`) scores each query edge by the **log of its expected
result cardinality** (pinnedness − traversal effort) and solves the most-constrained edge
first. Each edge is resolved by `query_edge` from a **pinned endpoint** — forward,
backward, or both-pinned (intersection) — with **every constraint applied inline during
traversal**: predicate, category, node-degree/IC plugin filters, node attribute
constraints, qualifier constraints, and edge attribute constraints (the last from the cold
LMDB tier, checked only for survivors). Discovered node IDs propagate as new pins, and
`reconstruct_paths` performs a multi-way **join** into `PathArrays`. Because at least one
endpoint of *every* edge must be pinned (`query_edge.py` raises *"Both nodes unpinned —
bad query planning"*), gandalf is a **constraint-propagation lookup engine**, not an
open-ended path explorer. In return it covers arbitrary query-graph shapes with **full
TRAPI 1.5 semantics**: inverse/symmetric predicate expansion, qualifier-value hierarchies,
attribute constraints, `set_interpretation` (BATCH / ALL / COLLATE / MCQ), and subclass
expansion via query-graph rewriting.

**this repo — general graph algorithms + a left-to-right frontier walk.** `CSRGraph`
exposes classic algorithms gandalf does *not*: `shortest_path` / `all_shortest_paths`
(BFS), `all_paths` (bounded DFS), `paths_by_predicate_sequence` (fixed predicate
template). `match_path` is a **frontier BFS from a single pinned start**, expanding hop by
hop, applying node/edge **metadata filters in batches against the backend** at each hop,
capped at 50k intermediate candidates. For arbitrary shapes, `trapi.py` adds a
`_general_match` backtracker plus a `_linear_query` fast path delegating to `match_path`.

| Query / question | gandalf | this repo |
| ---------------- | ------- | --------- |
| Pinned → pinned pattern (does A connect to B via predicate P?) | ✅ both-pinned intersection, inline-filtered | ✅ `match_path` / `all_paths` |
| Pinned → category, N hops ("what is X associated with?") | ✅ propagate pins edge-by-edge, any N | ✅ `associations(max_hops=N)` → `match_path` |
| **Paths deeper than 3 hops** | ✅ via an N-edge TRAPI query graph (no ceiling); ⚠️ but no vectorized kernel beyond 3 edges, and no `max_hops`-style argument | ✅ native `max_depth=` / `max_hops=` / N-hop `path_spec` |
| Arbitrary branching / cyclic query graph | ✅ planner + join (native strength) | ✅ `trapi.py::_general_match` backtracker |
| **Shortest path between two entities** | ❌ no shortest-path search at any length (the 3-edge kernel is fixed-arity enumeration, not a shortest-path algorithm) | ✅ `shortest_path` (scipy BFS, any length) |
| **All shortest paths** | ❌ not exposed | ✅ `all_shortest_paths` |
| **All simple paths ≤ k hops** | ❌ not exposed | ✅ `all_paths(max_depth=k)` |
| **Fixed predicate-sequence path** | ⚠️ expressible as a multi-edge query graph | ✅ `paths_by_predicate_sequence` |
| Fully-unpinned (all category-only) query | ❌ rejected ("bad query planning") | ⚠️ `match_path` needs the first node pinned, rest may be wildcards |
| Qualifier-constrained edges (aspect/direction/…) | ✅ with BMT value-hierarchy expansion, **now including `qualified_predicate` descendant expansion** | ⚠️ predicate/category + KL/agent-type filters; no qualifier-hierarchy expansion |
| Attribute / node-degree / IC constraints | ✅ inline + plugin node filters | ⚠️ `trapi.py` post-filters via backend (`AttributeConstraint`) |
| **Interactive one-hop-at-a-time exploration with a GUI** | ✅ **new: Mithrandir** | ✅ static HTML reports (`kg_report.py`) |

Net: **gandalf is uniquely strong at fully-constrained, multi-edge TRAPI lookups**; **this
repo is uniquely strong at open reachability/exploration** (shortest path, all paths,
predicate templates) and at *answering how two things connect* without a fully specified
query graph.

---

## Exploration UX — Mithrandir vs. `kg_report.py`

New section; this is where the two projects newly overlap.

**Mithrandir** (`mithrandir/`) is a prototype GUI for walking the Translator KG **one
triple at a time** against a *local* gandalf graph. Notable design points:

- The backend **imports gandalf and calls `lookup()` in-process** — no TRAPI server
  needed. Neighbours for node `X` come from two predicate-agnostic one-hop query graphs
  (`X --?--> n1` and `n1 --?--> X`, since edges are directed), read out of
  `message.knowledge_graph` and grouped by `(predicate, direction)`.
- Uses **dehydrated lookup** for the one-hop expand (commit `886756b`) to avoid paying for
  full enrichment on an interactive click.
- Node **degrees are read straight from the CSR offsets** and cached — a nice use of the
  data structure that csrgraph's report layer does not do.
- Connected nodes are partitioned by primary Biolink category and ranked by degree; the
  path you build shows in a filmstrip with Back/Forward navigation.
- It is a **plain `http.server` app** (`do_GET`/`do_POST`, static file serving), not
  FastAPI, and is **not packaged** as a console script — it's explicitly a prototype.
- **Mock mode** (`GANDALF_MOCK=1`) serves synthetic data with no graph and no network — a
  genuinely good idea for demo/CI.
- Stated limitations: degree ranking is **sampled** (first 300 per relationship), grouping
  uses **primary category only**, subclass inference **off by default**.
- External dependencies at runtime: **SRI Name Resolver** for search, **Wikipedia** for
  info cards.

**`kg_report.py`** (this repo) renders a **self-contained static HTML page** from an
association query: an interactive network graph plus grouped tables, produced in one
batch command.

| | **Mithrandir** | **`kg_report.py`** |
| --- | --- | --- |
| Interaction model | Live, click-to-step, one triple at a time | One-shot render of a whole result set |
| Output | Long-running local server + browser app | Single portable HTML file (shareable, no server) |
| Multi-hop | ❌ one hop per step (the *user* is the path-finder) | ✅ renders `max_hops`-deep association results |
| Name search | External SRI Name Resolver | Built-in ES resolution |
| Degree ranking | ✅ from CSR offsets, cached | ❌ |
| Maturity | Prototype (mock mode, sampling caps, unpackaged) | Small but complete and packaged with the repo |

**Assessment:** these are complementary rather than equivalent. Mithrandir is a better
*browsing* experience; `kg_report.py` is a better *answer artifact*. The honest read is
that gandalf now has the more compelling interactive demo, while csrgraph retains the
better "compute an answer and hand it to a colleague" path — and the two obvious
cross-pollinations are for csrgraph to add CSR-offset degree ranking + a click-to-step
mode, and for Mithrandir to gain path-finding and a self-contained export.

---

## Query performance — pros & cons (unchanged mechanisms, one new default)

**gandalf**

- **Pros**
  - **Cost-based planning** (`get_next_qedge`) solves the cheapest edge first and can start
    from *either* pinned end — the single biggest lever against path explosion.
  - **Filter-during-traversal on every dimension**; expensive **cold-path LMDB
    edge-attribute lookups hit only survivors** of the cheap checks.
  - `neighbors_filtered_by_targets` avoids edge-property dict allocations entirely in the
    both-pinned case; hot-path qualifiers/sources are zero-alloc pool references.
  - Response building does a **single batched LMDB prefetch** (`get_batch`).
  - `PathArrays` (~50 B/path) + node-binding grouping keep huge result sets affordable.
  - **GC disabled for the whole query** (`search/gc_utils.py`) to avoid multi-second Gen-2
    scans over the long-lived CSR arrays.
  - Vectorized numpy kernel for the common 3-hop case (`np.isin` intersection).
  - **New:** zstd response compression cuts wire cost for very large knowledge graphs.
- **Cons**
  - Core neighbor iteration in `query_edge` is **pure-Python and GIL-bound**; high-degree
    hubs dominate latency (the code tracks ">0.1 s" *slow nodes*).
  - **Requires ≥1 pinned endpoint per edge** — no fully-open exploration.
  - **No scipy**: BFS and the general subgraph matcher are hand-rolled Python.
  - **New risk:** `large_result_threshold` at 10M effectively disables auto-dehydration, so
    a pathological query returns a fully hydrated response instead of a compact one. The
    1 GB body limit and zstd compression are the mitigations — memory, not just wire, is
    the exposure.

**this repo**

- **Pros**
  - **scipy C** powers `shortest_path` and `connected_components` (releases the GIL on the
    heavy topology work).
  - **Component-cache reachability pruning** (`_can_possibly_reach`) short-circuits entire
    searches across disconnected components — O(1) after the first labeling.
  - **[CORRECTED — this was false when written]** `match_path` was described here as
    issuing "one batched metadata-backend query per hop for the whole frontier". It did
    not: `filter_edges`/`filter_nodes` sat *inside* the per-frontier-node loop, so a hop
    cost one call pair **per node**. On a 3-hop association that meant up to 785 calls
    where 32 sufficed. Fixed in `75b276e`; the description above is now accurate.
  - **Per-predicate CSR matrices** make a predicate-filtered neighbor lookup a direct
    matrix-row slice — no masking pass.
  - **[NEW]** Admissible backward-reachability pruning for pinned tails, and vectorized
    frontier expansion — see the corrections below.
- **Cons**
  - `all_paths` / `all_shortest_paths` / `paths_by_predicate_sequence` are **pure-Python
    DFS/backtracking, exponential and *uncapped*** — only `match_path` enforces a cap.
    (Still true; `all_paths` still defaults to `max_depth=None`.)
  - The general algorithms filter **topology-only**; metadata filtering lives in
    `match_path` and `trapi.py`, not the raw path methods.
  - `trapi.py` attribute/qualifier constraints are applied **per binding, not batched**.
  - ~~`match_path` walks strictly left-to-right with no cost-based reordering~~
    **[CORRECTED]** — it still walks left-to-right, but it is no longer blind: a pinned
    tail now drives a bounded reverse BFS whose mask is applied *inside* CSR row
    expansion. That is target-aware pruning rather than cost-based reordering, and it is
    enough to **beat gandalf's bidirectional kernel by 3.9×** when the tail is selective.
  - Per-path Python tuple construction is now the dominant remaining cost on large
    result sets — the real gap to gandalf, not the traversal order.

**Net, against the target profile [CORRECTED].** The original verdict — "gandalf's
planner + inline filter + hot/cold tiering remains the more proven design, csrgraph
needs cost-based edge ordering, hard caps, and batched constraint prefetch" — was
**partly wrong on mechanism and partly overtaken**:

- **Batched constraint prefetch**: correct, and it was the single largest win
  (2-hop association 2.21 s → 0.05 s, 335 backend calls → 1).
- **Hard caps**: still open for the raw DFS methods.
- **Cost-based edge ordering**: *not* what csrgraph needed. Admissible reachability
  pruning delivered the same goal more cheaply, and measurement shows csrgraph's pruned
  forward walk beating gandalf's cost-planned bidirectional kernel on selective targets.
- What the head-to-head actually revealed is that the engines have **different cost
  functions**: gandalf's tracks the forward frontier and ignores the target until the
  intersection; csrgraph's tracks surviving work but with a ~100× larger per-element
  constant. Selectivity favours csrgraph, volume favours gandalf, and vectorization —
  not planning — was the lever that closed most of the volume gap.

---

## Subclass expansion — gandalf pulled clearly ahead here

This is the area of most concentrated change in gandalf this window, and it widens a real
gap.

- **gandalf** uses the **Biolink Model Toolkit (BMT)** with a configurable
  `subclass_depth`, implemented by **rewriting the query graph** (synthetic superclass
  nodes + auxiliary graphs + composite inferred edges). New in v1.0.0:
  - Subclass **support graphs are emitted even when a direct superclass↔superclass edge
    exists** — previously the direct edge suppressed the inference, which diverged from the
    Tier 1 driver. Both are now emitted, each on its own merits.
  - Subclass edges attach **per derivation**: an attached subclass edge is kept only if its
    child matches *this* base edge's expanded endpoint. Previously every sibling subclass
    edge attached to every base edge, cross-contaminating support graphs and **collapsing
    distinct inferred edges into one** (#39).
  - Edge bindings are **deduplicated on append**, so one QEdge binding never references the
    same KG edge twice.
  - Composite inferred edges are attributed to **`infores:obie` as primary knowledge
    source** with gandalf as `aggregator_knowledge_source` — correct provenance for
    ontology-derived entailments.
- **this repo** is **graph-derived**: `SUBCLASS_PREDICATES` (`subclass_of`,
  `rdfs:subClassOf`) drive a transposed-adjacency BFS, unioning every variant present in
  the graph, with **no BMT dependency**. Lighter, works on whatever subclass edges the
  graph actually contains, and on by default in every `kg_query` helper.

| | **gandalf** | **this repo** |
| --- | --- | --- |
| Source of truth | Biolink Model via BMT (pinned to 4.3.2) | Subclass edges present in the graph |
| Dependency cost | Requires `bmt` + schema fetch | None |
| Depth control | `subclass_depth` setting | Full transitive BFS |
| Result provenance | ✅ composite inferred edges + **support graphs** + `infores:obie` attribution + `knowledge_level=logical_entailment` | ❌ results are silently widened; no support graph, no inference marker |
| Correctness testing | ~1,000 lines across 4 subclass test files | Covered by the synthetic suite |

**Assessment:** csrgraph's approach is the more portable and the cheaper, and for
analytical use ("also show me the subtypes") it's the right call. For **TRAPI-compliant
answer generation it is now materially behind**: a consumer of csrgraph's TRAPI output
cannot tell which results came from a literal edge and which from subclass widening,
because no support graph or inference attribution is emitted. If csrgraph's TRAPI server
is meant for real Translator consumption, this is the highest-value gap on the list.

---

## Serialization & deployment

- **gandalf**: `save_mmap` / `load_mmap` → a **directory** of `.npy` files (memory-mapped,
  COW-shared across gunicorn workers) + LMDB stores + JSON for `meta_kg` /
  `sri_testing_data` / `metadata.json` + a small pickle for `predicate_to_idx` /
  `num_nodes`. Loads in ~1–2 s. `GANDALF_LOAD_MMAPS_INTO_MEMORY` optionally faults
  everything in. No artifact integrity manifest or version endpoint; `/metadata` returns
  the build-time JSON verbatim.
- **this repo**: a single portable **`.pkl.zst`** (4-byte `CSRG` magic header + versioned
  protocol), with an optional memmap fast-path (`.memmap/` companion dir). More portable;
  less multi-worker-optimized. `docs/production-release-plan.md` now specifies (but has not
  built) immutable versioned release dirs, a SHA-256 `manifest.json`, `GET /version`,
  read-only LMDB open, and atomic-swap / rolling-update delivery.

---

## Dependencies / Python

|             | **gandalf v1.0.0**                                                     | **this repo**                                                       |
| ----------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Core deps   | `bmt>=1.4.8`, `lmdb`, `msgpack`, `numpy` — **no scipy**                 | `numpy`, **`scipy`**                                                |
| Optional    | `server` (fastapi/httpx/orjson/psutil/pydantic/pydantic-settings/uvicorn/**zstandard**), **`mongo` (pymongo)**, `dev` | `lmdb`, `es`, `duckdb`, `server` (fastapi), `zstandard` (< 3.14) |
| Python      | **3.8+**                                                               | **3.11+**                                                           |
| Server      | gunicorn + FastAPI, port 6429, **`infores:dogpark-tier0`**              | FastAPI TRAPI server (`trapi_server.py`)                            |
| CLIs        | `gandalf-build`, **`gandalf-build-mongo`**, `gandalf-query`, `gandalf-diagnose` | `kg_query.py`, `kg_report.py`, `predicate_audit.py`         |
| Distribution| PyPI `gandalf-csr` **1.0.0**, published by GitHub Actions               | Source install (`pip install -e ".[all]"`)                          |

---

## Scalability & concurrency

Measured against the target profile. Mechanisms are unchanged; the new entries are
transport-level.

| Dimension                | **gandalf**                                                                                                                           | **this repo**                                                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Topology memory          | Hand-rolled `int32` arrays, dual-direction (≈2× topology); proven at 10M/38M                                                           | Per-predicate `uint8` scipy CSR + merged matrix + `edge_predicate_ids`; compact, but many small matrices add per-predicate overhead        |
| Property memory          | **Tiered**: interned hot pool + disk-backed LMDB cold store → bounded per-query footprint                                               | Pushed **out of process** into the metadata backend (ES/DuckDB/LMDB/SQLite); the graph object stays topology-only                          |
| Multi-worker serving **[CORRECTED]** | `load_mmap` shares the ~700 MB of `.npy` CSR arrays COW across workers — but **2.99 GB per worker is private**, because `edge_property_pools.pkl` (586 MB) unpickles into Python objects that cannot be shared | The `.memmap/` path shares the CSR arrays too; **0.50 GB per worker is private**, dominated by `id_maps` (388 MB of Python dict/list). **~6× cheaper per worker than gandalf** |
| In-process concurrency **[CORRECTED]** | Standard GIL build. Gets **×2.09 at 8 threads** — partial parallelism, because numpy releases the GIL inside its `concatenate`/`isin` kernels | Free-threaded build keeps the GIL **off** unless `lmdb` is imported: topology **×5.46**, ES backend **×8.16** at 8 threads. With the LMDB backend the GIL returns and threading **collapses to ×0.03** — use processes (**×7.57**) |
| Filtering strategy       | **Three-stage**: topology search → filter (predicate/qualifier/node-degree plugins) → **batch enrich only final paths**; intermediate cap (`GANDALF_MAX_PATH_LIMIT`) | Predicate filter = matrix selection; node/edge metadata filter inside `match_path` via **backend queries**, now **batched per hop** rather than per frontier node |
| Load time **[CORRECTED]** | **2.73 s** measured via `load_mmap`                                                                                     | **0.75 s** measured (0.30–0.45 s for the memmap read itself); 34 MB snapshot                                                          |
| Result-set scaling       | `PathArrays` (~50 B/path); **auto-dehydration now effectively disabled** (`large_result_threshold` = 10M)                               | Python lists of `PathEdge`/dicts + `limit=` caps; no compact intermediate representation                                                    |
| Wire cost                | **New: zstd request + response compression**; 1 GB max body                                                                            | No body compression on the TRAPI server                                                                                                    |
| Observability            | OTel in its own module, **post-fork per-worker init**, baggage propagation, `profiler.py`, `diagnostics.py`, `?profile=true`             | No tracing; `bench_backends.py` for backend benchmarking                                                                                   |
| Release/rollback         | Container image + Automat heartbeat; no version-gated readiness                                                                        | Planned only (`docs/production-release-plan.md`): manifest + `/version` readiness gate + auto-rollback                                     |

**Assessment [CORRECTED].** The original assessment here credited gandalf's
mmap/COW design with serving many gunicorn workers "from a single in-RAM copy",
and charged csrgraph with per-process copies. **Measurement reversed that.**

gandalf's COW sharing is real but covers only the ~700 MB of CSR arrays. Its
hot-path interned property pools ship as a 586 MB pickle that expands to
**2.91 GB of Python objects** per worker — verified in isolation (USS 0.010 GB →
2.914 GB from that one file). Python objects do not share across processes, so the
interning that makes gandalf's property memory compact is exactly what stops it
being shared. Measured private cost: **2.99 GB/worker**.

csrgraph's snapshot *is* memmap-shared; its private cost is **0.50 GB/worker**,
dominated by `id_maps` (388 MB of Python dict + list). On a 16 GB host that is
roughly **24 csrgraph workers against 4 gandalf workers** — the opposite of what
this section originally implied.

What survives from the original reading: csrgraph's topology genuinely is compact,
and offloading bulk property filtering and name resolution onto independently
scalable backends is the right way to scale the metadata tier. What does not
survive: the claim that csrgraph is the one paying per-process duplication.

The second original concern — Python-level traversal and per-path construction —
was **half right and has been half fixed**. Vectorizing frontier expansion cut a
2.2M-path query from 20.4 s to 2.8 s. What remains is per-path Python tuple
construction, which is the standing gap to gandalf on large result sets and would
need a `PathArrays`-style representation to close.

The one thing to watch in gandalf v1.0.0 is still the raised
`large_result_threshold` — it trades a memory-safety default for response
completeness.

---

## Bottom line

**gandalf 1.0.0** is the heavier, production-hardened, *opinionated* artifact — and this
release is a **consolidation, not a redesign**. The CSR core, planner, and search engine
did not change; what changed is everything around them: a formal pluggable ingest layer
with a validated record contract, a second production data source (MongoDB), a
correctness overhaul of subclass inference with honest `infores:obie` provenance,
qualifier fidelity pinned to the Tier 1 driver, zstd transport compression, fork-correct
tracing, a PyPI release pipeline, a deployment identity as **Dogpark Tier 0**, and a first
interactive explorer in **Mithrandir**. It still does one thing — serve TRAPI path queries
over a fixed massive KG — extremely well, and remains tightly coupled to the Translator
deployment model.

**This repo** is unchanged in code and remains leaner and more *general/flexible*:
scipy-backed algorithms including the shortest-path/all-paths family gandalf still lacks,
a genuinely pluggable multi-backend metadata layer, **built-in Elasticsearch name
resolution** (gandalf still delegates this to the SRI Name Resolver — as Mithrandir
demonstrates), single-file portable snapshots, NL-style query helpers, and HTML reporting.
It is better as an interactive/analytical library and easier to point at arbitrary graphs
(`dgidb`, `ttd`, …).

The competitive picture has moved in two ways since June, both against csrgraph:

1. **Exploration is no longer csrgraph's alone** — Mithrandir gives gandalf a live
   graph-browsing UI (though not multi-hop pathfinding, and not without external name
   resolution).
2. **The TRAPI-fidelity gap widened** — gandalf's subclass work now emits support graphs
   and inference provenance that csrgraph's silent subclass widening does not.

And in csrgraph's favour:

3. **Release engineering** — csrgraph now has a concrete artifact-versioning /
   health-gated-swap / rollback design; gandalf ships no artifact manifest or version
   endpoint. This is design intent versus shipped code, so it's a soft advantage until
   `make_release.py` and `/version` exist.
4. **[ADDED after measurement] Per-worker memory** — 0.50 GB vs 2.99 GB private, roughly
   24 workers vs 4 in 16 GB. This was the reverse of what this report originally claimed.
5. **[ADDED after measurement] Load time and artifact size** — 0.75 s from a 34 MB
   snapshot vs 2.73 s from a 29 GB directory, which matters for autoscaling and rolling
   restarts.
6. **[ADDED after measurement] In-process concurrency headroom** — on a free-threaded
   build csrgraph reaches ×5.46 (topology) and ×8.16 (ES backend) at 8 threads against
   gandalf's ×2.09, *provided* the LMDB backend is not what forces the GIL back on.

**Also worth recording: the two engines agree.** Run on identical data they returned
exactly the same distinct node paths on every query tested, up to 2,196,629 paths. For two
independent implementations of Biolink path-finding, that is the most reassuring result in
either document.

### What each could borrow from the other

Ideas this repo could adopt from gandalf, **highest value first** — revised after
measurement:

1. **Subclass support graphs + inference provenance** — emit composite inferred edges with
   a support graph and an attribution (à la `infores:obie`) so consumers can distinguish
   literal from subclass-derived results. **Still the biggest TRAPI-compliance gap**, and
   still open.
2. **`PathArrays`-style compact path buffers** — promoted from #4. Now the *measured*
   remaining performance gap: per-path Python tuple construction is what keeps csrgraph
   2.6–9.2× behind gandalf on million-path results.
3. **Hard caps** on the DFS/backtracking methods — still open; `all_paths` still defaults
   to `max_depth=None`. ~~cost-based edge ordering~~ **[CORRECTED]** dropped from this
   item: reachability pruning achieved the goal, and beats gandalf's planner on selective
   targets.
4. **A `GraphSource`-style validated ingest contract** if csrgraph ever needs a non-KGX input.
5. **zstd request/response compression** on `trapi_server.py` — cheap, and TRAPI bodies are large.
6. **CSR-offset degree lookups** for ranking in `kg_report.py` (Mithrandir's trick — nearly free).
7. **A mock/synthetic mode** for the report and TRAPI layers, so demos and CI need no graph or ES.

**Two items were removed from this list because measurement contradicted them:**

- ~~*Directory-of-`.npy` mmap loading for multi-worker COW sharing*~~ — csrgraph's
  `.memmap/` path already shares the CSR arrays, and it is **6× cheaper per worker than
  gandalf** (0.50 GB vs 2.99 GB private). There is nothing to borrow here; if anything the
  borrowing should run the other way.
- ~~*Hot/cold edge-property tiering with interned pools*~~ — the interned pools are
  precisely what makes gandalf cost 2.99 GB *per worker*, because they are Python objects
  that cannot be shared. csrgraph's choice to push properties out of process into the
  metadata backend is the better one for multi-worker serving. **Copying this would be a
  regression.**

**Something gandalf could borrow from csrgraph:** move the interned property pools into
memmapped arrays so they share across workers. That single change would cut its
per-worker footprint by roughly 2.9 GB. csrgraph has the mirror-image opportunity in
`id_maps` (388 MB of its 500 MB private cost) — see
[`docs/concurrency-and-scalability-2026-07-19.md`](concurrency-and-scalability-2026-07-19.md) §5.2.

Where this repo is still ahead of gandalf:

1. **General path algorithms** — `shortest_path`, `all_shortest_paths`, `all_paths`,
   `paths_by_predicate_sequence`. Untouched by v1.0.0.
2. **Pluggable serving/metadata backends** (SQLite / DuckDB / LMDB / ES / Hybrid).
3. **Built-in entity (name/symbol → CURIE) resolution** — gandalf still has none in-repo;
   `normalize.py` is ingest normalization, not resolution.
4. **Single-file portable snapshots** and a natural-language query layer.
5. **Self-contained shareable HTML answer artifacts** (`kg_report.py`).
6. **No BMT dependency** — subclassing works from whatever the graph contains.
7. **A written production release/versioning plan** (manifest, `/version`, rollback).
