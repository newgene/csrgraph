# csrgraph vs. gandalf — measured, on identical data

Head-to-head on the **Translator KG `2026-07-19`** dataset described in
[`local-data-2026-07-19.md`](local-data-2026-07-19.md). Both engines were built
from the same archive, so differences here are implementation, not data.

- **csrgraph**: this repo @ `8ef1581` (branch `batch-match-path-metadata-filtering`)
- **gandalf**: `ranking-agent/gandalf` @ `82a1fb2` (v1.0.0, 2026-07-21)

Harnesses: `compare_csrgraph.py`, `compare_gandalf.py`, `compare_hard.py` (each
engine runs in its own venv; fixtures are shared through JSON).

## The comparable query shape

gandalf's `_do_unfiltered_search(start, end)` is `start → n1 → n2 → end` over
forward edges: it expands the forward 2-hop frontier, then intersects it with
`incoming_neighbors(end)` via `np.isin` — a **vectorized meet-in-the-middle**.
csrgraph answers the same shape with
`match_path([start, None, None, None, None, None, end])`: a left-to-right
frontier walk with backward-reachability masking.

Results are compared as **distinct node paths**: gandalf returns node
quadruples and keeps duplicate triples, csrgraph returns predicate-annotated
edges and collapses duplicates, so the node-path set is the only common
denominator.

## Correctness: exact agreement, everywhere

Two independent implementations, same answers on every case tested — including
2.2M-path results:

| Query | csrgraph | gandalf | Agree |
| --- | --- | --- | --- |
| 3-hop → MONDO:0005516 | 1,232 | 1,232 | ✅ |
| 3-hop → MONDO:0006679 | 197 | 197 | ✅ |
| 3-hop → MONDO:0008159 | 590 | 590 | ✅ |
| 3-hop → MONDO:0012819 | 893 | 893 | ✅ |
| 3-hop → NCBIGene:1366 | 50,994 | 50,994 | ✅ |
| 3-hop → UBERON:0001062 (deg 13,788 start) | 936,305 | 936,305 | ✅ |
| 3-hop → UBERON:0001062 (deg 31,530 start) | 2,196,629 | 2,196,629 | ✅ |

1-hop expansion also agrees exactly (raw/distinct: 403/399, 21/19, 36/32).

## Load and memory

| | csrgraph | gandalf |
| --- | --- | --- |
| Load time | **0.75 s** (memmap) | 2.73 s (`load_mmap`) |
| RSS after load | **0.53 GB** | 3.26 GB |
| Peak RSS | **1.00 GB** | 3.28 GB |
| On-disk graph | **34.4 MB** pkl.zst (+745 MB memmap) | 29 GB mmap dir (incl. LMDB stores) |
| Edges held | 28,105,517 (deduped) | 28,925,258 (duplicates kept) |

csrgraph is ~3.6× faster to load and ~6× smaller resident. gandalf's footprint
buys the reverse CSR (`rev_*` + `rev_to_fwd`) and interned property pools; note
its 29 GB directory includes the edge/node LMDB stores that csrgraph keeps in a
separate 23 GB backend, so total-storage comparison is closer than it looks.

## Traversal speed: neither engine dominates

Small result sets (warm, both engines):

| 3-hop query | Paths | csrgraph | gandalf |
| --- | --- | --- | --- |
| → MONDO:0006679 | 197 | **0.008 s** | 0.009 s |
| → MONDO:0008159 | 590 | 0.021 s | **0.009 s** |
| → MONDO:0012819 | 893 | 0.029 s | **0.009 s** |
| → MONDO:0005516 | 1,232 | 0.029 s | **0.010 s** |

Large result sets, high-degree starts:

| 3-hop query | Start out-deg | End in-deg | Distinct paths | csrgraph | gandalf | Winner |
| --- | --- | --- | --- | --- | --- | --- |
| CHEBI:30614 → NCBIGene:1366 | 4,815 | 646 | 50,994 | **1.07 s** | 3.60 s | csrgraph **3.4×** |
| CHEBI:50924 → UBERON:0001062 | 13,788 | 24,271 | 936,305 | 9.27 s | **0.13 s** | gandalf **70×** |
| CHEBI:33216 → UBERON:0001062 | 31,530 | 24,271 | 2,196,629 | 20.39 s | **1.11 s** | gandalf **18×** |

### Why the split

The two engines have different cost functions, and that is the whole story:

- **gandalf's cost tracks the forward 2-hop frontier size**, essentially
  independent of how many paths survive. It has no target-aware pruning of the
  first two hops: it always materializes the full 2-hop buffer, then intersects.
  Per-path cost in the vectorized regime is ~**0.11 µs**, but on the selective
  case (end in-degree 646) it degrades to ~57 µs/path because it pays for the
  whole forward expansion to return 51k paths.
- **csrgraph's cost tracks surviving work**, because the backward reach mask
  prunes using the target: with an in-degree-646 end, the middle hop is masked to
  646 candidate nodes and the search is cheap. But its per-element constant is
  ~**9–20 µs**, roughly 100× gandalf's, because expansion is per-node Python.

So target selectivity favours csrgraph; sheer volume favours gandalf. The
crossover here sits around an end in-degree of a few thousand.

### Where csrgraph's time actually goes

Profiling the 20.4 s case (2,238,910 paths):

```
ncalls     tottime  function
2,260,721   19.440  _mp_expand_edges          <- 77% of runtime
        1    2.133  match_path
      447    0.764  _flush
```

`_mp_expand_edges` is called **once per frontier node** — 2.26 M times — and each
call loops over all 63 per-predicate CSR rows in Python. That is the entire gap.
It is not path construction and not metadata: this workload makes zero backend
calls.

## What this means for the deferred item 2

Item 2 was "meet-in-the-middle bidirectional enumeration", parked earlier on the
grounds that admissible reachability pruning had already removed the dead
exploration bidirectional search exists to avoid. **That reasoning holds, and
the measurements support it** — case 1 is csrgraph's pruned forward walk beating
gandalf's bidirectional kernel by 3.4× precisely because pruning is the better
tool when the target is selective.

But the measurements also show the earlier conclusion was **incomplete**: it
explained why csrgraph does not need a backward *pass*, and then wrongly implied
there was nothing left to win. There is, and it is large (18–70×). The lever is
not bidirectionality — csrgraph already has backward information in its reach
masks — it is **vectorization**. gandalf is fast in cases 2–3 because it
expands and intersects whole frontiers in numpy; csrgraph is slow because it
calls a Python function per frontier node.

**Revised recommendation.** Close item 2 as originally specified and replace it
with *vectorized frontier expansion*:

1. Gather all frontier rows in one ragged numpy operation (the same
   `indptr`/`repeat`/`arange` gather already used in `_reach_masks`' BFS)
   instead of per-node `_mp_expand_edges` calls.
2. Apply the reach mask to the gathered array — keeping csrgraph's target-aware
   pruning, which is the part gandalf lacks.
3. Materialize Python path tuples only for survivors, or keep them in parent-
   pointer arrays (the `PathArrays`-style representation) and hydrate at the end.

That combination should beat gandalf on *all* three hard cases: pruning wins
case 1 today, and vectorization is what wins cases 2–3. Expected gain on the
20.4 s case is most of the 19.4 s currently spent inside `_mp_expand_edges`.

## Caveats

- Single-process, warm-cache, local ES/LMDB; no concurrency. gandalf's
  production story (mmap COW sharing across gunicorn workers, hot/cold property
  tiering) is not exercised here.
- Only the 3-hop pinned-both-ends shape is directly comparable. csrgraph's
  shortest-path/all-paths family has no gandalf equivalent, and gandalf's TRAPI
  query-graph engine (qualifiers, subclass support graphs, `set_interpretation`)
  has no csrgraph equivalent; neither is measured here.
- csrgraph returns predicate-annotated paths, gandalf node quadruples. csrgraph
  is doing modestly more work per path; that does not account for an 18–70× gap.
- gandalf keeps duplicate triples, so its raw path counts run higher
  (9,930,541 vs 2,238,910 on the largest case) for the same distinct answer.
