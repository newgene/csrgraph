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
