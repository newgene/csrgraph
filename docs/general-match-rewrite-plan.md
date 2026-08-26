# Plan: move `_general_match` into index space

> Plan only — no code changes yet.

## Which queries this affects

`trapi.query` linearises what it can and sends the rest to `_general_match`.
Since symmetric predicates moved onto the vectorized path, the remainder is
exactly **branching and cyclic** query graphs — anything `_linearise` rejects.

That is currently **no HelmsDeep corpus query**, which is worth stating plainly:
this path is unmeasured by the corpus and less exercised than `match_path`. It is
also where both determinism bugs lived.

## Measured cost

On the 2026-07-19 graph, LMDB backend, `limit=200`:

| query shape | route | time | results |
| --- | --- | --- | --- |
| branching (3 nodes, 2 edges) | `_general_match` | **341 ms** | 200 |
| cyclic triangle (3 nodes, 3 edges) | `_general_match` | **1,945 ms** | 200 |
| comparable linear 2-hop | `match_path` | 7–192 ms | 141–200 |

One to two orders of magnitude slower than the fast path on comparable shapes.

## Where the time goes

`cProfile` on the cyclic query, 2.045 s total:

| function | cumtime | share | calls |
| --- | --- | --- | --- |
| `_get_edge_neighbors` | 1.674 s | 82% | 821 |
| ↳ `_reverse_neighbors` | 1.660 s | 81% | 410 |
| ↳↳ `scipy` `getcol` | 1.079 s | 53% | 410 |
| ↳↳ `nonzero` → `tocoo` | 0.580 s | 28% | 410 |

**81% of the runtime is one function**, and the cause is a data-structure
mismatch rather than anything algorithmic. `_reverse_neighbors` answers "who
points at this node?" with `csr.getcol(v)` per relation — and extracting a
*column* from a **row**-compressed matrix is O(nnz), so it scans every non-zero
in the matrix to find one column. It then calls `col.nonzero()`, which converts
to COO all over again.

Measured on one node, wildcard (all 63 relations):

| approach | time | nodes returned |
| --- | --- | --- |
| `getcol` per relation (current) | **222 ms** | 5,074 |
| slice a cached transposed matrix | **0.17 ms** | 5,074 |

**1,309× on identical output.** For a single node lookup. `_general_match` does
410 of these on a triangle.

## What already exists to reuse

`CSRGraph._reverse_expansion_plan()` already builds and caches exactly what is
needed — per-relation transposed `(indptr, indices, label)` triples — because
`match_path` needed reverse hops for the "what treats disease X?" shape. It costs
**232 ms to build once** and **535 MB** resident, against 563 MB for the forward
`csr_by_relation`.

So the expensive part is built, cached, and unused by this code path.

## Phasing

Three phases, in descending value per unit of risk. Phase 1 alone captures most
of the benefit; phases 2 and 3 are the structural work.

### Phase 1 — reverse lookups off the cached transpose

Rewrite `_reverse_neighbors` to slice `_reverse_expansion_plan()` instead of
calling `getcol`. Contained: one function, no change to the backtracking
structure, no change to any answer set.

* addresses ~81% of the runtime
* ~15 lines
* **cost:** a process that only ever serves branching queries now pays 535 MB and
  232 ms it did not before. A process that has served *any* reverse-direction
  linear query has already paid it — the plan is lazily built and shared.

### Phase 2 — backtrack over node indices, not CURIE strings

Candidates become `int` indices (or numpy arrays); intersection across bound
neighbours becomes set/array intersection on ints; CURIEs are materialised only
when a complete binding is recorded.

This is the structural fix for a bug class rather than a speedup:

* **The determinism fix stops being a patch.** `sorted(candidates)` exists only
  because iterating a `set[str]` is hash-seed dependent. `int.__hash__` is
  identity, so index-space sets iterate deterministically without sorting — which
  is precisely why `match_path` never had the bug. The `sorted()` call and the
  subprocess-based `PYTHONHASHSEED` test both become unnecessary rather than
  load-bearing.
* Removes ~1.7M dict lookups per triangle query (`graph.nodes[...]` and
  `node_to_id[...]` per candidate per level).
* **cost:** touches the whole function, and the ordering under truncation changes
  from alphabetical-by-CURIE to index order. See Impacts.

### Phase 3 — batch metadata filtering per level

`_filter_by_qnode` already batches one `filter_nodes` call per candidate set, so
this is smaller than it was for `match_path`. What remains is `_matching_predicates`,
called per candidate pair during cycle verification; those could be batched into
one `filter_edges` per level the way `_flush` does.

Worth doing only if profiling *after* phases 1–2 still shows it. On the current
profile metadata access does not appear in the top eleven, so this is speculative
and should stay unbuilt until measured.

## Benefits

| | |
| --- | --- |
| Branching queries | ~341 ms → expect low tens of ms (81% of time removed) |
| Cyclic queries | ~1,945 ms → expect ~350 ms after phase 1 |
| Reverse lookup primitive | 222 ms → 0.17 ms, verified identical output |
| Determinism | patched by `sorted()` → structural (phase 2) |
| Code shape | two matchers with unrelated internals → both in index space |

The projections are arithmetic from the profile, not measurements; they assume
the remaining 19% is unchanged and should be confirmed rather than trusted.

## Impacts and risks

**Answer sets must not change.** Phases 1 and 2 alter *how* candidates are found,
never which ones qualify. The verification below treats that as a hard invariant,
because it is the same guarantee the corpus work established at some cost.

**Truncation ordering will change (phase 2).** Today a capped branching query
keeps the alphabetically-first CURIEs; in index space it keeps ingest order. Both
are arbitrary, and both are deterministic — but they are *different*, so a client
comparing capped results across versions will see movement. Two notes:

* This is the same arbitrariness `docs/truncation-ranking-plan.md` proposes to
  replace with something defensible. If ranking lands first, phase 2 becomes
  ordering-neutral, because ordering would then be explicit rather than incidental.
  **That argues for sequencing ranking before phase 2**, or accepting one ordering
  change now and none later.
* It does *not* reintroduce nondeterminism. Index-keyed sets are stable across
  processes; the existing hash-seed test will keep passing, and should be kept as
  a regression guard even once `sorted()` is gone.

**Memory.** Phase 1 makes branching queries pay 535 MB for the transposed plan.
On a server that also answers reverse linear queries this is already resident and
the marginal cost is zero. On one that does not, it is a real 535 MB. Worth an
explicit decision rather than a silent regression: the alternative is transposing
only the relations a query actually names, which is cheaper but loses the caching.

**Low urgency, and it should be said.** No corpus query reaches this path, so
nothing currently measured gets faster. The case rests on branching and cyclic
shapes being legitimate TRAPI that some caller will eventually send — not on a
benchmark that improves today.

> **Update (2026-08-26): the caller arrived.** `mcp_server.py`'s `graph_query`
> tool routes every branching and cyclic pattern here through `kg_pattern` →
> `trapi.match`, and shared `?variable` patterns are the feature it exists to
> provide. Two premises above have shifted:
>
> - "Nothing currently measured gets faster" is no longer true in practice. A
>   two-triple branch (`[["CDK2","affects","?d:Disease"],["?drug","treats","?d"]]`,
>   157 matched paths) takes ~1.0 s against the 2026-07-19 release, and by the
>   profile above most of that is `_reverse_neighbors`. Phase 1's fifteen lines
>   are the obvious first move.
> - The 535 MB for the transposed plan is no longer hypothetical: the MCP server
>   is a long-lived process that answers *both* branching and reverse linear
>   queries, so the marginal cost really is near zero there — the "server that
>   does not" case now needs naming rather than assuming.
>
> Verification step 5 still holds and is now more useful, not less: the corpus
> exercises 12 query types and none route here, so it should stay bit-identical
> while `graph_query` results change. `tests/test_kg_pattern.py` covers the
> translation; the shared-variable identity test is the one that would catch a
> routing mistake.

## Verification

1. **Answer sets identical, uncapped.** Run the branching and cyclic queries above
   at a limit high enough not to truncate, before and after each phase, and require
   byte-identical answer sets. This is the invariant that makes the rest safe.
2. **Reverse-lookup equivalence.** `_reverse_neighbors` old vs new over a sample of
   a few thousand nodes, wildcard and single-predicate: identical sets, no
   exceptions. Cheap and it covers the one function phase 1 touches.
3. **The full suite, plus determinism.** 138 tests, including the
   `PYTHONHASHSEED` test, which must keep passing for the reason above.
4. **Re-profile.** Confirm the projections in Benefits, and only then decide
   whether phase 3 has anything left to fix.
5. **Corpus unchanged.** No corpus query routes here, so the corpus should be
   *bit-identical* before and after. If it moves, something is wrong with the
   routing rather than with this code.
