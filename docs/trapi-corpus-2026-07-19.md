# HelmsDeep TRAPI corpus — csrgraph vs gandalf

Running the Translator load-test corpus from
[TranslatorSRI/HelmsDeep](https://github.com/TranslatorSRI/HelmsDeep)
(`helmsdeep/trapi_corpus.py`) against both engines on the shared
**Translator KG `2026-07-19`** dataset.

Harness: `trapi_corpus_bench.py`. Entry points are the libraries, not HTTP —
`trapi.query()` for csrgraph, `gandalf.search.lookup()` for gandalf.

## Headline

**csrgraph returns zero results for the most common Translator query shape.**
When the pinned node is the edge *object* and the subject is open — "what
chemicals treat disease X?" — it answers nothing, on a non-empty answer set.
7 of the 11 runnable corpus queries are affected, including the entire MVP1
creative template. gandalf answers all of them.

## Corpus applicability

Both engines are KPs, so only one of the four segments is a fair test of intended
behaviour:

| Segment | Queries | Applicable? |
| --- | --- | --- |
| `retriever` (lookup mode) | 6 | **Yes** — this is what both engines are for |
| `shepherd` / `ars` (inferred/creative) | 5 | No — ARA-level reasoning; neither implements it. Submitted anyway, as the shapes are still valid lookups |
| `pathfinder` | 1 | No — uses `paths` instead of `edges` in the query graph |

Corpus entity coverage on this KG: **9 of 10 CURIEs present** with substantial
degree (T2D out-degree 4,481; metformin 5,423). Only `CHEBI:53289` (donepezil) is
absent, so the corpus exercises real data here.

## Results

| qtype | segment | csrgraph | gandalf | Answer sets |
| --- | --- | --- | --- | --- |
| `one_hop_lookup_pinned` | retriever | **0.1 ms**, 1 result | 420 ms, 1 result | **IDENTICAL** |
| `one_hop_lookup_open` | retriever | 0.0 ms, **0** | 523 ms, 140 | differ: csr 0 / gan 140 |
| `one_hop_no_predicate` | retriever | 6.5 ms, **0** | 755 ms, 2,957 | differ: csr 0 / gan 2,957 |
| `two_hop_lookup` | retriever | 52 ms, 200 | 3,461 ms, 62,536 | csr ⊂ gan (limit=200 cap) |
| `batch_lookup` | retriever | 0.0 ms, **0** | 542 ms, 548 | differ: csr 0 / gan 512 |
| `malformed_query` | retriever | `KeyError` | `KeyError` | both raise |
| `mvp1_heavy` | shepherd | 0.0 ms, **0** | 519 ms, 70 | differ: csr 0 / gan 70 |
| `mvp1_medium` | shepherd | 0.0 ms, **0** | 510 ms, 140 | differ: csr 0 / gan 140 |
| `mvp1_light` | shepherd | 0.0 ms, **0** | 483 ms, 49 | differ: csr 0 / gan 49 |
| `mvp2_chem_affects_gene` | shepherd | 0.4 ms, **0** | 480 ms, 979 | differ: csr 0 / gan 979 |
| `mvp2_chem_affects_open_gene` | shepherd | 11.3 ms, 108 | 515 ms, 804 | csr ⊂ gan (108 shared) |
| `pathfinder_drug_disease` | pathfinder | `KeyError` | `KeyError` | both raise |

## FIXED: the direction defect

`_linearise` now returns the per-hop direction it always computed, `_linear_query`
forwards it, and `match_path` accepts `hop_directions` and walks a reverse hop over
a lazily built transposed per-relation plan (`_reverse_expansion_plan`). Emitted
`PathEdge` tuples keep true `(subject, predicate, object)` orientation whichever way
the hop was walked, so knowledge-graph edges and bindings come out correct.

Re-running the corpus after the fix:

| qtype | before | after | gandalf | verdict |
| --- | --- | --- | --- | --- |
| `one_hop_lookup_pinned` | 1 | 1 | 1 | **IDENTICAL** |
| `one_hop_lookup_open` | **0** | 140 | 140 | **IDENTICAL** |
| `one_hop_no_predicate` | **0** | 200 | 2,957 | subset (limit cap) |
| `two_hop_lookup` | 200 | 200 | 62,536 | subset (limit cap) |
| `batch_lookup` | **0** | 200 | 548 | subset (limit cap) |
| `mvp1_heavy` | **0** | 70 | 70 | **IDENTICAL** |
| `mvp1_medium` | **0** | 140 | 140 | **IDENTICAL** |
| `mvp1_light` | **0** | 48 | 49 | 1 missing (predicate hierarchy) |
| `mvp2_chem_affects_gene` | **0** | 99 | 979 | subset (qualifier hierarchy) |
| `mvp2_chem_affects_open_gene` | 108 | 108 | 804 | subset (qualifier hierarchy) |

Four queries now match gandalf exactly. Every remaining difference is a strict
subset of gandalf's answers — csrgraph returns nothing gandalf does not — and each
has an identified cause:

* **`limit=200` cap** on three queries. Not a defect.
* **No Biolink predicate-hierarchy expansion.** `mvp1_light`'s single missing answer
  is `CHEBI:135939`, which connects to Alzheimer's by `applied_to_treat` and
  `treats_or_applied_or_studied_to_treat` — *not* by the queried `treats`. gandalf
  expands the queried predicate to its BMT descendants; csrgraph matches literally.
* **No qualifier-value hierarchy expansion**, as previously known, for the two MVP2
  rows.

The two hierarchy gaps are the same underlying feature — Biolink Model expansion —
and are now the largest remaining accuracy difference between the engines.

## Accuracy findings (as originally measured)

### 1. Open subject + pinned object returns nothing (csrgraph) — NOW FIXED

Isolated with one edge known to exist (`CHEBI:6801 -[treats]-> MONDO:0005148`,
confirmed present in the metadata store):

| Query | csrgraph |
| --- | --- |
| pinned **subject**, open object | 50 results |
| open subject, pinned **object** | **0 results** |
| both pinned | 1 result |

The both-pinned case proves the answer set is non-empty — metformin qualifies — so
the open-subject case returning nothing is a defect, not an empty graph.

**Root cause.** `trapi._linearise` builds an adjacency that records per-hop
direction (`adj[subj].append((ek, obj, True))` /
`adj[obj].append((ek, subj, False))`), picks the pinned node as the traversal
start, and then **returns only the node and edge ordering — the `is_fwd` flag is
discarded**. `_linear_query` therefore builds a direction-less `path_spec`, and
`match_path` follows only outgoing CSR edges. When the pinned start is the edge's
object, the walk proceeds the wrong way down the edge and matches nothing.

The information needed to fix it is already computed; it is thrown away one
function too early. A fix needs `_linearise` to return the per-hop direction and
`match_path` (or the spec builder) to honour it — the reverse adjacency added for
reachability pruning (`_reverse_merged`) is the obvious mechanism.

This is the single most consequential gap found in this session: the affected
shape is *the* Translator creative-mode template.

### 2. Qualifier-constrained results are a strict subset (csrgraph)

`mvp2_chem_affects_open_gene`: csrgraph 108, gandalf 804, and csrgraph's 108 are
all inside gandalf's 804. The query carries `object_aspect_qualifier` and
`object_direction_qualifier` constraints. gandalf expands qualifier values to
their Biolink descendants; csrgraph matches literally. Consistent with the design
comparison — csrgraph has no qualifier-value hierarchy expansion — and it shows up
as missing answers rather than wrong ones.

### 3. `two_hop_lookup` difference is the cap, not a defect

csrgraph returned exactly 200 (`limit=200`), a subset of gandalf's answers.
Expected.

### 4. Shared robustness gap: neither engine rejects malformed input

`malformed_query` (an edge referencing a nonexistent node) raises
`KeyError: 'n_missing'` in **both** engines, and the Pathfinder shape raises
`KeyError: 'edges'` in both. The corpus includes the malformed case specifically
to measure error-path latency, which implies a service is expected to answer with a
TRAPI error rather than a stack trace. Both libraries would need the server layer
to validate first — gandalf has `request_validation.py` for this; csrgraph's
`trapi_server.py` should be checked for equivalent coverage.

## Performance findings

Only `one_hop_lookup_pinned` is a clean latency comparison — both engines return
the same single result. There csrgraph is **0.1 ms against gandalf's 420 ms**, a
difference far outside measurement noise.

Every other row is confounded and should not be read as a speed comparison:

- The seven zero-result csrgraph rows are fast because they do no work.
- `two_hop_lookup` compares 200 results against 62,536.
- gandalf shows a floor of several hundred milliseconds on every query, including
  a both-pinned single-edge lookup. Disabling subclass expansion cut one sample
  from 1,630 ms to 1,050 ms, so subclass rewriting accounts for part but not most
  of it.
- **gandalf's absolute latencies here are noisy**: the same query measured 420 ms
  during the corpus run and 1,630 ms minutes later, because a 3-node Elasticsearch
  cluster is co-resident on the same 12-CPU VM. Treat gandalf's figures as
  order-of-magnitude only. csrgraph's sub-millisecond and tens-of-milliseconds
  numbers are far enough from the noise floor to stand.

## What to do next

1. **Fix the discarded traversal direction** (`trapi._linearise` →
   `_linear_query` → `match_path`). Highest-value correctness fix available; it
   unblocks 7 of 11 corpus queries.
2. **Add the corpus to regression testing** once (1) lands, asserting non-empty
   answers for the open-subject shapes so this cannot regress silently.
3. **Validate query graphs before execution** so malformed input yields a TRAPI
   error rather than `KeyError`.
4. Qualifier descendant expansion, to close the MVP2 subset gap.

---

# Re-run after qualifier-variant keying — 2026-08-13

Same graph data (`2026-07-19`), same gandalf results file. Both metadata stores
were rebuilt with edge metadata keyed on
`(subject, predicate, object, qualifier_fingerprint)`.

## Stores rebuilt and verified

`probes/verify_variants.py` counts the source `edges.jsonl` independently of
either store:

| | |
| --- | --- |
| raw edge records | 28,925,258 |
| distinct `(s, p, o)` | 28,105,517 |
| distinct `(s, p, o, fingerprint)` | **28,860,305** |
| assertions the old key dropped | **754,788** |
| records still collapsed (indistinguishable qualifiers) | 64,953 |

LMDB reports 28,860,305 entries and Elasticsearch 28,860,305 docs — three
independent counts agreeing exactly. Build cost: LMDB 3,119 s / 24 GB,
ES 1,240 s / 4 GB primary at 3 shards + 1 replica.

Variants per triple: **98.18%** have exactly one, mean **1.0269**, maximum
**128**. Earlier notes in this session said "max 14, 99.20% single-variant";
those figures were wrong, and the correction mattered — see below.

## Two ES defects the rebuild exposed

1. **`get_edge_variants` was bounded by `max_edges_per_pair` (default 100).**
   `CHEBI:33216 -affects-> GO:0008283` has 103 variants, so ES returned 100 where
   LMDB returned 103 — dropped answers on the qualifier path, and a divergence
   between the two supported backends. Now bounded by the result window instead,
   and it warns rather than truncating silently. This is a single-triple point
   lookup, so unlike `filter_edges` the bound costs nothing to raise.

2. **The wildcard-predicate branch of `filter_edges` shared one `size` budget**
   across every pair in a `should` query, so ES returned the globally top-N hits
   and one dense pair could crowd others out entirely — dropping whole predicates.
   Pre-existing, amplified ~100× by variant keying. Now an msearch with a per-pair
   `size`, for the same number of round-trips as the known-predicate branch.

## Ablation: what each change actually contributes

Same store, same code, `--no-variants` truncating retrieval to one edge per
triple. Answer counts:

| qtype | −var −exp | +var −exp | −var +exp | +var +exp |
| --- | --- | --- | --- | --- |
| `mvp2_chem_affects_gene` | 92 | 119 | 92 | **137** |
| `mvp2_chem_affects_open_gene` | 102 | 128 | 102 | **135** |
| all eight others | unchanged | unchanged | unchanged | unchanged |

Variant keying contributes **+45 / +33** and the Biolink expander **+18 / +7**,
on exactly the two qualifier-constrained queries and nothing else — the predicted
mechanism, with no side effects elsewhere.

Comparing against the previously saved `/tmp/tc_csr.json` would *not* have
isolated this: that run predates commit `30fa74e` (subclass expansion on by
default), so its deltas conflate three changes. The pre-variant LMDB store cannot
be read by the current code either — its 3-component keys don't match the
4-component prefix — which is why the ablation patches retrieval instead.

## The dominant cause was neither: constraints are applied after the cap

`limit` does not mean "return up to N answers". `match_path` enumerates N paths
and qualifier/attribute constraints are filtered *afterwards*, so a constrained
query returns "however many of the first N survive". Sweeping the limit on
`mvp2_chem_affects_gene`:

| limit | 200 | 1,000 | 5,000 | 20,000 |
| --- | --- | --- | --- | --- |
| results | 137 | 713 | **979** | 979 |

At `limit >= 5000` csrgraph returns **979**, and `mvp2_chem_affects_open_gene`
returns **804** — both *exactly* gandalf's answer sets, set-equal with zero
difference in either direction. To its credit `match_path` does emit its
truncation warning here, so the shortfall was reported rather than silent.

This is the same defect class as the multi-predicate bug fixed earlier
(filter-after-cap). It is the strongest argument for raising the default `limit`,
and better, for pushing constraints into enumeration.

## Final comparison, uncapped and expanded

`--expander --limit 100000` against unchanged gandalf results:

| qtype | csrgraph | gandalf | accuracy |
| --- | --- | --- | --- |
| `one_hop_lookup_pinned` | 0.2 ms, 1 | 420 ms, 1 | **IDENTICAL** |
| `one_hop_lookup_open` | 7.2 ms, 141 | 523 ms, 140 | csr superset (n1) |
| `one_hop_no_predicate` | 219 ms, 4,262 | 755 ms, 2,957 | csr superset (n1) |
| `two_hop_lookup` | 28,045 ms, 62,536 | 3,461 ms, 62,536 | **IDENTICAL** |
| `batch_lookup` | 33.6 ms, 659 | 542 ms, 548 | csr superset (n1) |
| `mvp1_heavy` | 2.9 ms, 71 | 519 ms, 70 | csr superset (n1) |
| `mvp1_medium` | 7.6 ms, 141 | 510 ms, 140 | csr superset (n1) |
| `mvp1_light` | 3.2 ms, 64 | 483 ms, 49 | csr superset (n1) |
| `mvp2_chem_affects_gene` | 90.6 ms, 979 | 480 ms, 979 | **IDENTICAL** |
| `mvp2_chem_affects_open_gene` | 63.3 ms, 804 | 515 ms, 804 | **IDENTICAL** |

Four exactly identical. Every remaining difference is csrgraph returning **more**
on the pinned node `n1`, and per-node set arithmetic shows five of the six are a
strict superset of gandalf:

| qtype | node | csr | gandalf | shared | csr-only | gan-only |
| --- | --- | --- | --- | --- | --- | --- |
| `one_hop_lookup_open` | n1 | 2 | 1 | 1 | 1 | 0 |
| `one_hop_no_predicate` | n1 | 4 | 1 | 1 | 3 | 0 |
| `batch_lookup` | n1 | 38 | 6 | 5 | 33 | **1** |
| `mvp1_heavy` | n1 | 2 | 1 | 1 | 1 | 0 |
| `mvp1_medium` | n1 | 2 | 1 | 1 | 1 | 0 |
| `mvp1_light` | n1 | 3 | 1 | 1 | 2 | 0 |

Three distinct causes, not one:

1. **No `query_id` on subclass-expanded bindings.** csrgraph binds subclass
   *descendants* of a pinned node: asked for `MONDO:0005148` it also binds
   `MONDO:0011072`, emitting `{"id": "MONDO:0011072", "attributes": []}`. gandalf
   binds only the queried CURIE. TRAPI has `NodeBinding.query_id` for exactly
   this; without it a downstream ARA cannot tell the bound node came back as a
   descendant of what it asked for, and strictly the result does not satisfy the
   query graph as written. This accounts for the bulk of the csr-only counts.

2. **Subclass descendants bypass the queried category.** `batch_lookup` constrains
   `n1` to `biolink:Disease`, yet csrgraph binds `HP:0000978` ("Easy Bruising"),
   whose category list does **not** contain `biolink:Disease`. Expanded nodes are
   not re-checked against the query node's `categories`. This is a plain
   conformance bug, independent of (1).

3. **The one gandalf-only answer is gandalf over-reporting.** `MONDO:0019293` has
   194 in-edges but **zero** under `biolink:treats` or either BMT descendant
   (`ameliorates_condition`, `preventative_for_condition`). What does point at it
   is `treats_or_applied_or_studied_to_treat` — an *ancestor* of `treats`. A
   weaker, broader assertion does not entail `treats`, so matching it widens the
   hierarchy upward. csrgraph is correct to exclude it.

`two_hop_lookup` at 28 s against gandalf's 3.5 s is the one place gandalf is
clearly faster on equal output (62,536 results both). Its vectorized 3-hop
bidirectional search is built for exactly this shape.

## Backend parity

Same config (expander, `limit=200`), LMDB vs Elasticsearch: **identical answer
sets on 9 of the 10 answering queries**. The one exception is `two_hop_lookup`,
where both return exactly 200 — *which* 200 depends on backend edge ordering, so
it is a truncation artifact rather than disagreement. LMDB is 20–80× faster
throughout (e.g. `mvp2_chem_affects_gene` 14 ms vs 1,146 ms), consistent with the
earlier backend benchmarks.

## Remaining work

1. **Emit `query_id` on subclass-expanded node bindings**, and **re-check expanded
   nodes against the query node's `categories`** (a Disease query currently returns
   `HP:` phenotype nodes). Together these account for every csr-only answer.
2. **Push constraints into enumeration, or raise the default `limit`.** The
   filter-after-cap behaviour silently under-answers constrained queries.
3. Surface truncation in TRAPI `message.logs` (it currently only warns).
4. Split 400 vs 500 in `trapi_server.py`; put the corpus in CI behind a
   data-gated skip.

---

## Subclass-binding conformance fixed — 2026-08-13

`trapi._resolve_subclass_bindings` now runs between matching and the constraint
filters. Two changes, one root cause (expanded nodes were bound without ever
facing their QNode again):

* **`categories` enforced on expanded nodes.** `batch_lookup` no longer binds
  `HP:0000978` and five other `PhenotypicFeature` nodes to a `biolink:Disease`
  query node. Only the expanded CURIEs are re-checked — open nodes were already
  category-filtered during enumeration, so validating them too would add a large
  batched backend call per query node and find nothing.
* **`NodeBinding.query_id` emitted.** A descendant now declares the queried CURIE
  it stands in for: `{"id": "MONDO:0011072", "query_id": "MONDO:0005148"}`. Direct
  hits carry no `query_id`, so a client can tell them apart. On `batch_lookup`
  27 of the 32 `n1` bindings are expanded and 5 are direct.

Corpus effect — exactly one row moves, and nothing else:

| | n0 | n1 | results |
| --- | --- | --- | --- |
| before | 512 | 38 | 659 |
| after | 509 | **32** | 637 |

The six `HP:` nodes go, and with them three chemicals (`CHEBI:32184`,
`CHEBI:59477`, `CHEBI:74947`) whose only route to the query was through those
phenotypes. Every other query's answer set is unchanged.

### The 3 lost n0 answers are a deliberate trade, not a regression

gandalf reports those three; csrgraph now does not. This follows directly from the
binding representation and is worth stating plainly:

* Emitting the **descendant** as `id` means the descendant is the answer, so it
  must satisfy the query node's `categories`. A phenotype cannot be bound to a
  node constrained to `biolink:Disease`.
* gandalf instead binds the **queried** CURIE, so its `n1` is always one of the
  six MONDO diseases and the category holds trivially — which lets a chemical
  treating "Easy Bruising" answer "what treats skin vascular disease?".

Both are self-consistent; they differ on whether treating a phenotypic subtype
answers a question about the parent disease. csrgraph's reading is the stricter
one and is the only one compatible with reporting the descendant as the answer.
If the looser reading is wanted, the change is to bind the queried CURIE and move
the descendant into `query_id`'s place — not to drop the category check, which
would put contradictory answers back.

### Remaining differences after this fix

| qtype | node | csr | gandalf | note |
| --- | --- | --- | --- | --- |
| `one_hop_lookup_open` | n1 | 2 | 1 | expanded subtype, now with `query_id` |
| `one_hop_no_predicate` | n1 | 4 | 1 | same |
| `mvp1_heavy` / `mvp1_medium` | n1 | 2 | 1 | same |
| `mvp1_light` | n1 | 3 | 1 | same |
| `batch_lookup` | n0 / n1 | 509 / 32 | 512 / 6 | see trade-off above |

These are now a **representation** difference rather than a defect: csrgraph
reports which subtype matched, gandalf reports the queried term. Four queries
remain byte-identical.

The one genuine correctness item still open is backend-dependent truncation:
`limit` truncates enumeration in whatever order the backend yields, so LMDB and ES
keep different subsets (disjoint on `two_hop_lookup`'s `n1`). Both subsets are
provably inside the uncapped answer set, so neither is wrong, but the same query
should not depend on the configured backend.

---

## Truncation was nondeterministic, not backend-dependent — fixed 2026-08-13

The LMDB/ES divergence on `two_hop_lookup` was misdiagnosed. Three tests:

| Test | Result |
| --- | --- |
| LMDB vs ES at `limit=3000` | **identical** — same 2,404 `n0`, same 7 `n1` |
| LMDB alone, three separate runs at `limit=200` | **three different answers** (`NCBIGene:4137` / `:2` / `:1509`) |
| Same, with `PYTHONHASHSEED=0` | identical every run |

The backends never disagreed. They differed only because they ran in **separate
processes**. The real defect was that a truncated result depended on the process's
hash seed, so the same query could answer differently on consecutive calls.

### Root cause

`two_hop_lookup` queries `biolink:associated_with`, which is symmetric, so it
routes to `_general_match`, **not** `match_path` (verified deterministic: identical
200 paths across seeds). Two sites in the general matcher iterated sets of CURIE
**strings**, and Python randomises `str.__hash__` per process:

* `_general_match` collected candidates into a `set[str]` and passed
  `list(candidates)` to the constraint filter. That order decides which candidates
  are explored before `len(results) >= limit` halts the search.
* `_matching_predicates` built `list(set(actual_preds) | set(reverse_preds))` for
  symmetric edges, and callers take `preds[0]` as *the* predicate — so which
  predicate got reported varied between runs even with no truncation at all.

`match_path` escaped because it works in CSR index space, where the sets hold
`int` node indices and `int.__hash__` is identity. Every other set in these
modules is a membership test whose order never escapes.

Sorting by **CURIE** rather than node index is deliberate: index order depends on
KGX ingest order and would not survive a graph rebuild.

### gandalf, for comparison

Measured deterministic across three hash seeds (identical set *and* ordering). It
does **not** sort candidates — its `sorted()` calls canonicalise *keys*
(`tuple(sorted(key_pairs))` for grouping, `tuple(sorted(...))` over qualifiers and
sources for a stable edge key — the same trick as our `qualifier_fingerprint`).
It avoids the failure mode structurally: the search runs on integer node indices in
numpy arrays, and where it truncates it slices a numpy array
(`path_finder.py:120`). Note it did not truncate this query at all, returning all
62,536, so it sidesteps the question rather than answering it differently.

So sorting is a correct minimal fix, not parity with gandalf. The durable fix is
gandalf's: keep `_general_match` in index space. That is a rewrite of the general
matcher, worth doing only if symmetric-predicate queries matter enough — 1 of 12
corpus queries today, though 39 Biolink predicates are symmetric.

### Truncation is now reported

`_general_match` previously signalled nothing when it hit the cap, and neither
matcher surfaced it in the response. Both now return `(bindings, truncated)`, and
a capped result carries a `message.logs` entry:

```json
{"level": "WARNING", "code": "ResultsTruncated",
 "message": "Result set is incomplete: enumeration stopped at limit=200,
             returning 200 result(s). Raise the limit for a complete answer."}
```

This matters more than it looks: because constraints are applied *after* the cap,
a constrained query can return far fewer answers than exist, and previously
nothing in the response said so.

### Verified

* Three runs on the real graph now return identical `n1`
  (`['NCBIGene:10347', 'NCBIGene:1471']`) and report `ResultsTruncated`.
* **LMDB and ES now agree at `limit=200`** — `n0` 179 both, same `n1`. Original
  symptom gone.
* Uncapped corpus results are unchanged, including `two_hop_lookup` at 62,536
  identical to gandalf — ordering only matters when truncating.
* The determinism test fails on the pre-fix code (three different 5-CURIE sets
  across seeds), so it genuinely guards the behaviour.

### Still open

Determinism makes the arbitrary choice *reproducible*, not *good*: the kept subset
is now the alphabetically-first CURIEs. Two independent follow-ups remain —
applying constraints during enumeration so `limit` means "N answers" rather than
"N paths examined", and ranking before truncating so the kept subset is defensible.

---

## Symmetric predicates on the vectorized path — 2026-08-13

csrgraph already *supported* symmetric predicates: `query()` detected them and
routed to `_general_match`, which searches both directions. Verified against a
stored edge `CHEBI:100147 -interacts_with-> CHEBI:100241` — querying either way
returned the edge. And `two_hop_lookup`, the only symmetric corpus query, was
already answer-set identical to gandalf.

So symmetry was never an accuracy gap. Attribution of the remaining corpus
differences, per query node:

| qtype | node | csr-only | of which subclass-expanded | symmetric? |
| --- | --- | --- | --- | --- |
| `one_hop_lookup_open` | n1 | 1 | 1 | False |
| `one_hop_no_predicate` | n1 | 3 | 3 | False |
| `batch_lookup` | n1 | 27 | 27 | False |
| `mvp1_heavy` | n1 | 1 | 1 | False |
| `mvp1_medium` | n1 | 1 | 1 | False |
| `mvp1_light` | n1 | 2 | 2 | False |

**35 of 35** csr-only answers are subclass-expanded bindings carrying `query_id`,
and none of those queries uses a symmetric predicate.

### It was a performance gap instead

A single symmetric predicate anywhere in the query graph dropped the whole query
off the fast path, losing vectorized expansion, batched metadata filtering, and
reachability pruning. Measured on the `two_hop_lookup` shape:

| Path | Time |
| --- | --- |
| `match_path` (vectorized) | **0.33 s** |
| `_general_match` (what ran) | 26.7 s |
| gandalf | 3.5 s |

That was the entire explanation for the one benchmark gandalf won.

`match_path` now accepts a hop direction of `None`, meaning symmetric: it walks
both ways and unions the results, tagging each candidate with the direction it
came from so emitted edges keep their true stored orientation. `_linear_query`
sets `None` for hops whose predicates include a symmetric one — reviving a
`symmetric_edges` dict that had been computed and never read — and `query()` no
longer diverts. `_general_match` keeps its own symmetric handling for branching
and cyclic queries.

### Result

`two_hop_lookup`, at a limit high enough not to truncate:

| | before | after | gandalf |
| --- | --- | --- | --- |
| time | 26,665 ms | **2,336 ms** | 3,461 ms |
| `n0` / `n1` | 12,892 / 547 | 12,892 / 547 | 12,892 / 547 |

Answer sets unchanged and identical to gandalf; csrgraph is now **faster than
gandalf** on the query it previously lost by 8×. Every other corpus row is
byte-identical. The remaining `n2` difference (7 vs 1) is the same subclass
`query_id` representation as every other row — `match_path` expands a pinned
start node where `_general_match` did not, so this query now behaves like the
rest.

### Two things found on the way

* **A latent aliasing bug.** Each direction's `_mp_expand_frontier` returns its
  own `labels` sequence. Keeping the relation *index* and resolving it after the
  loop read forward-walk indices against reverse-walk labels. Predicates are now
  resolved at collection time.
* **Symmetric walks enumerate more paths for the same answers** — 129,186 against
  72,478 here, because this graph stores both `condition_associated_with_gene`
  and `gene_associated_with_condition` for the same pairs. Those are genuinely
  distinct edges, so both are kept; only an identical `(pair, predicate)` reached
  both ways is deduplicated. The practical consequence is that a symmetric query
  hits a fixed `limit` sooner: at `limit=100000` this query truncated to 43,292
  results and 309 genes, which the `ResultsTruncated` log now reports. Raising the
  limit recovers the full 547.
