# Plan: rank results before truncating

> Plan only — no code changes yet.

## Problem

Truncation is now deterministic, but the kept subset is arbitrary. Determinism
made it *reproducible*; it did not make it *defensible*. The two matchers even
truncate on two different arbitrary orders:

| matcher | selection point | order today |
| --- | --- | --- |
| `match_path` | `next_frontier[:hop_cap]`, `frontier[:limit]` (`csrgraph_kgx.py`) | CSR expansion order — i.e. **KGX ingest order** |
| `_general_match` | `for curie in candidates` until `len(results) >= limit` (`trapi.py`) | **alphabetical by CURIE**, from the determinism fix |

So "the first 1000" means "whichever happened to be ingested first" in one path
and "whichever sort first as strings" in the other. Neither has anything to do
with answer quality, and a client cannot recover what was dropped.

## What "better" should mean here

Translator practice gives a clear steer, and gandalf encodes it directly. Its
`lookup()` takes `filter_config` for `NodeFilter` plugins, documented as
`{"max_node_degree": 50, "min_information_content": 0.8}` — so in this domain:

- **hub nodes are noise.** A node with degree 1,530 connects to everything and
  discriminates nothing.
- **low information content is noise.** Broad ontology terms ("disease",
  "chemical entity") are less useful answers than specific ones.

Note gandalf uses these as *filters*, not as ranking. Filtering discards; ranking
reorders. Ranking is the better fit for truncation, because it degrades
gracefully — an uncapped query returns everything either way.

## Available signals, measured

| signal | coverage | cost | source |
| --- | --- | --- | --- |
| **out-degree** | 100% | **4.84 µs/node** | `csr_merged.indptr[i+1] - indptr[i]`, in memory |
| **in-degree** | 100% | one `np.bincount` over `csr_merged.indices` at load — O(E) once, ~6.7 MB | not currently computed |
| **information_content** | **~54–69%** (108/200 and 346/500 sampled) | **free** | already returned by `filter_nodes` and discarded |
| `knowledge_level` / `agent_type` | high | free in the batch path | already in `filter_edges` results |

The two findings that make this cheap: `filter_nodes` already returns full node
metadata including `information_content`, so IC ranking adds **no round trips**;
and degree comes straight from the CSR index arrays.

The IC coverage gap is the main modelling problem. Ranking on IC naively would
systematically demote the ~30–45% of nodes that simply lack the field, which is a
metadata gap, not a statement about relevance.

## The structural difficulty

Ranking requires seeing candidates before choosing among them, but `match_path`
truncates **while streaming**: `_flush` appends to `next_frontier`, and expansion
stops as soon as it reaches `hop_cap`. There is no point at which the full
candidate set exists. Three ways round it:

**A. Rank within each flushed batch.** Order candidates inside `_flush` before
extending `next_frontier`. Nearly free, no extra memory — but it only orders
*within* a batch, so the result is "best of each arbitrary batch", which is
barely better than today.

**B. Over-collect, then select top-N.** Enumerate `k × limit` (k ≈ 2–4), rank the
whole set, keep the best `limit`. Genuinely global within the over-collection
window. Costs k× the enumeration work and memory on exactly the queries that are
already the expensive ones.

**C. Rank the frontier before the last hop.** Order the *sources* going into the
final expansion, so the best-connected/most-specific ones get expanded first.
Cheap (frontier is already materialised) and biases which paths are built at all,
but it ranks intermediates rather than answers.

## Recommendation

**B, with k configurable and defaulting to 1 (i.e. off).** Ranking that only sees
a window is a half-measure that is hard to reason about; ranking that sees `k ×
limit` is explainable — "we considered 4,000 and returned the best 1,000". Making
k default to 1 means the default behaviour is exactly today's, and ranking is
something a caller opts into with a known cost multiplier.

Scoring function, deliberately simple:

```
score = w_ic * ic_norm + w_deg * degree_penalty
  ic_norm        = information_content / 100, or the *median* of the
                   candidate set when absent (so a missing field is neutral,
                   not disqualifying)
  degree_penalty = 1 / (1 + log10(1 + total_degree))
```

Missing IC imputed to the batch median is the important detail — it stops the
metadata gap from becoming a ranking signal. Weights start at `w_ic = w_deg =
0.5` and are tunable; they should not be tuned by intuition (see validation).

## Phasing

1. **Precompute total degree.** `in_degree = np.bincount(csr_merged.indices,
   minlength=num_nodes)` at load, stored alongside `csr_merged`; add to the
   memory report. ~6.7 MB, O(E) once. Independently useful.
2. **Stop discarding IC.** Thread the node metadata that `_flush` already
   receives from `_mp_filter_nodes_batch` through to the frontier entry, instead
   of keeping only the CURIE.
3. **Add `rank_by` / `overcollect` parameters** to `match_path` and `trapi.query`,
   defaulting to off. Off must be byte-identical to today — assert that.
4. **Apply the same ordering in `_general_match`**, replacing `sorted(candidates)`
   with the score-ordered list, so the two matchers stop disagreeing.
5. **Report it.** Extend the `ResultsTruncated` log entry to name the ranking
   used, so a partial answer says *how* it was chosen, not just that it was.

## How to validate — the part that matters

Ranking changes which answers come back, so "it looks better" is not evidence.
Three checks, in order of strength:

1. **Uncapped results must be unchanged.** Ranking may only affect *which*
   subset is kept, never the full set. Run the corpus at `limit=300000` before
   and after and require byte-identical answer sets. This is the safety net that
   makes the rest low-risk.
2. **Recall against the uncapped truth.** For each corpus query, compute the full
   answer set, then measure what fraction the capped run recovers under
   (a) today's order, (b) alphabetical, (c) each candidate ranking. A ranking is
   only justified if it beats arbitrary order on this metric — and it is worth
   knowing that for a *uniformly random* relevance distribution, no ranking beats
   arbitrary order, so a null result here is a real possibility and should be
   accepted rather than tuned around.
3. **Overlap with gandalf under truncation.** Both engines capped at the same
   limit should agree more under a good ranking than under an arbitrary one,
   since gandalf's filters encode the same domain intuition.

## Risks

- **It changes answers.** Answer-set parity with gandalf was hard-won this
  session; check 1 above is what protects it, and off-by-default is what keeps
  the blast radius at zero until someone opts in.
- **Tuning without a metric is how you get a worse ranking that feels better.**
  Weights must move only on check 2.
- **IC coverage may be non-random.** If the ~30–45% lacking IC are concentrated
  in particular namespaces or categories, median imputation is not neutral. Worth
  measuring coverage by category before trusting it.

## Sequencing note (added 2026-08-14)

`docs/general-match-rewrite-plan.md` phase 2 moves `_general_match` into index
space, which changes truncation ordering in that matcher from alphabetical-by-CURIE
to ingest order — arbitrary either way. **If ranking lands first, that phase
becomes ordering-neutral**, because ordering would be explicit rather than
incidental, and callers see one change instead of two. That is the only argument
for doing this work before the rewrite; on its own merits it is still the lower
priority of the two.

## Honest priority

This is the **lowest-value** item on the current list. Truncation now reports
itself, the default limit is 1000, and most corpus queries converge below that —
`one_hop_no_predicate` (4,262) and `two_hop_lookup` (62,536) are the only ones
that genuinely exceed it. So ranking would change the answer to two of ten corpus
queries, on a metric nobody has yet asked to optimise.

The production release plan (`docs/production-release-plan.md`, F1–F4, entirely
unimplemented) blocks deployment; this does not block anything. Sequence
accordingly.
