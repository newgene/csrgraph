# Concurrency and scalability — measured

How csrgraph and gandalf behave under concurrent load, how csrgraph's LMDB and
Elasticsearch backends differ, and what to do to serve higher request traffic.

All numbers from the shared **Translator KG `2026-07-19`** dataset
([`local-data-2026-07-19.md`](local-data-2026-07-19.md)) on one host: 8 cores,
128 GB RAM, macOS/Apple Silicon. csrgraph @ `2330c8f`; gandalf v1.0.0 @ `82a1fb2`.
Harnesses: `bench_concurrency.py`, `bench_concurrency_gandalf.py`.

> Single-host, warm-cache measurements. They characterise *scaling shape* —
> where a tier stops improving and why — not absolute production capacity.

## 1. The GIL is the first-order variable, and the backend decides it

The project venv is a **free-threaded** CPython 3.14.3 build (`Py_GIL_DISABLED`).
The GIL starts off and stays off through the whole csrgraph import chain, because
`metadata_db` imports `lmdb` lazily. But instantiating the LMDB backend triggers
`import lmdb`, and that **re-enables the GIL at runtime**:

```
RuntimeWarning: The global interpreter lock (GIL) has been enabled to load
module 'lmdb.cpython', which has not declared that it can run safely without
the GIL.
```

| Imports | GIL |
| --- | --- |
| `numpy`, `scipy` | off |
| `+ elasticsearch` | off |
| `+ csrgraph_kgx`, `metadata_db` | off |
| `+ lmdb` (i.e. using the LMDB backend) | **on** |
| `lmdb` with `PYTHON_GIL=0` | off (unsafe — see below) |

So **choosing a metadata backend also chooses a threading model.** gandalf runs
on a standard 3.13 build, where the GIL is always on.

## 2. Thread scaling

Throughput with N threads sharing one loaded graph, 5–20 s per configuration.

| Configuration | 1 thread | 8 threads | Scaling |
| --- | --- | --- | --- |
| csrgraph topology only, GIL **off** | 174.9 req/s | **954.9 req/s** | **×5.46** |
| csrgraph topology only, GIL on | 177.7 req/s | 200.5 req/s | ×1.13 |
| csrgraph + **ES**, GIL **off** | 4.9 req/s | **40.1 req/s** | **×8.16** |
| csrgraph + ES, GIL on | 5.0 req/s | 23.7 req/s | ×4.76 |
| csrgraph + **LMDB**, GIL on (free-threaded build) | 2.1 req/s | 0.1 req/s | **×0.03** |
| csrgraph + LMDB, GIL on (standard build) | 2.0 req/s | 0.1 req/s | **×0.04** |
| csrgraph + LMDB, `PYTHON_GIL=0` | 2.3 req/s | 2.5 req/s | ×1.09 (peak ×1.68 @ 4) |
| gandalf 3-hop kernel, GIL on | 113.5 req/s | 236.2 req/s | ×2.09 |

Three findings:

**The free-threaded build is a genuine advantage — where it applies.** Pure
topology traversal goes from ×1.13 to ×5.46 on 8 cores. ES-backed queries reach
×8.16, better than linear-per-core because they interleave I/O waits.

**LMDB under threads does not merely fail to scale, it collapses.** ×0.03 at 8
threads — 25× *less* aggregate throughput than a single thread. This reproduces
identically on a standard GIL build (×0.04), so it is **not** a free-threading
artifact; my first hypothesis that it was runtime GIL re-enablement was wrong.
The mechanism is GIL convoying: a category `filter_nodes` walks ~51,704
`cursor.next()` calls plus zstd-decompress and `json.loads` per hit, each a short
C call that releases and reacquires the GIL. With N threads the handoffs dominate.
Removing the GIL entirely (`PYTHON_GIL=0`) recovers it to ×1.68 peak, which is
consistent with that explanation.

**gandalf gets partial thread parallelism (×2.09)** despite the GIL, because
numpy releases it during the large `concatenate`/`isin` operations its kernel is
built from. Its curve is non-monotonic (×1.61 at 2, ×1.12 at 4, ×2.09 at 8),
reproducible across runs — likely performance/efficiency-core placement on Apple
Silicon rather than anything in gandalf.

## 3. Memory per worker — the multi-process picture reverses expectations

Both engines memory-map their arrays read-only (`np.memmap(mode="r")` /
`np.load(mmap_mode="r")`), so those pages are shared through the OS page cache.
RSS counts them in every worker and therefore overstates cost; **USS (unique set
size) is the marginal cost of an extra worker.**

| | RSS/worker | **USS/worker** | Dominated by |
| --- | --- | --- | --- |
| csrgraph | 0.62 GB | **0.50 GB** | `id_maps` — 388 MB of Python dict + list |
| gandalf | 3.28 GB | **2.99 GB** | `edge_property_pools.pkl` |

Constant across 1–4 workers in both cases, so these are true per-worker figures.

**csrgraph costs ~6× less per worker than gandalf**, which contradicts the
design-level reading in `COMPARISON-2026-08.md` — that gandalf's mmap
copy-on-write gives "one in-RAM copy regardless of worker count" while csrgraph
is "unpickled into each process's heap (N workers ≈ N copies)". Both halves need
correcting:

- gandalf's mmap sharing is real but covers only the ~700 MB of CSR arrays. Its
  **hot-path property pools are a 586.6 MB pickle that unpickles into 2.91 GB of
  Python objects** — measured directly: USS 0.010 GB → 2.914 GB from that one
  file. Python objects do not share across processes, so every gunicorn worker
  pays it in full.
- csrgraph's snapshot *is* memmapped (~700 MB shared); what it pays privately is
  the node↔index maps, 388 MB of Python objects.

The irony is that the interning gandalf uses to shrink property memory is
precisely what stops that memory from being shared.

Practical capacity on a 16 GB host, leaving 4 GB headroom: roughly **24 csrgraph
workers** versus **4 gandalf workers**.

## 4. LMDB vs Elasticsearch

| Dimension | LMDB | Elasticsearch |
| --- | --- | --- |
| Location | in-process, mmapped | out-of-process, HTTP |
| GIL | forces it **on** | leaves it off |
| Thread scaling | **×0.03** (collapses) | **×8.16** (near-linear) |
| Whole-category enumeration (1.06M ids) | **0.80 s** | 77.4 s |
| Filter a candidate list by category | 2.0 req/s | **5.0 req/s** |
| Edge filter (`knowledge_level`) | **0.008 s** | 0.171 s |
| Scaling unit | the host (vertical) | the cluster (horizontal) |
| Extra infrastructure | none | a cluster to run and size |

They are good at opposite things, and the crossover is not subtle:

- **LMDB wins on per-call latency** when the operation is an index scan it can do
  locally — whole-category enumeration is 97× faster.
- **ES wins on concurrency and on candidate-list filtering.** Its per-call cost is
  higher, but it releases the GIL, so aggregate throughput scales while LMDB's
  falls off a cliff. On this benchmark's shape (filter ≤500 candidates by
  category) ES is also faster per call, because LMDB's category `filter_nodes`
  scans the entire category index regardless of how few ids you pass.

At 16 threads the ES path plateaus at ~50 req/s. ES itself reported **0 rejected**
search requests and 20% heap, but the podman VM hosting it consumed ~7 of 8 host
cores (`krunkit` at 686%) against the client's ~2.4. So the ceiling here is the
single local ES node, not csrgraph — the tier is saturated but healthy, which is
exactly the condition horizontal scaling addresses. (`docker stats` under podman
reported a bogus 3.07% for the container; don't trust it.)

## 5. Strategies for higher request traffic

Ordered by measured value per unit of effort.

### 5.1 Match the concurrency model to the backend — free, today

- **LMDB → processes only.** `gunicorn`/`uvicorn` with N *single-threaded*
  workers. Threads must be avoided outright: ×0.03 means a threaded server
  degrades under exactly the load it is meant to absorb. At 0.50 GB USS a worker
  is cheap, so process-per-core is affordable.
- **ES → threads are fine and preferable.** ×8.16 on 8 cores, and one process
  serving many concurrent requests keeps the graph loaded once. A threaded or
  async server plus a scaled ES cluster is the higher-ceiling configuration.
- **Do not run the free-threaded build with the LMDB backend and threads.** It
  gives the worst of both: the GIL comes back at runtime *and* you lose the
  free-threaded build's guarantees. Either use processes, or use ES.

### 5.2 Move `id_maps` out of the Python heap — the biggest single win

388 MB of the 500 MB private per-worker cost is `node_to_id` (dict) plus `nodes`
(list). Replacing them with memmapped structures — a sorted CURIE blob plus
offsets, searched with `np.searchsorted`, or an FST/perfect hash — would make
them shared and cut per-worker cost to roughly **0.11 GB**, a ~4.5× increase in
workers per host. Same technique gandalf already uses for its node store
(`node_store.lmdb`), and the reason its node ids are not part of its 2.99 GB.

### 5.3 Precompute and share the derived structures

`_reverse_merged()` (~150 MB), the reach masks, and `_expansion_plan()` are built
lazily *per process*. Every worker pays the ~0.26 s reverse-transpose and holds
its own private copy. Building them at snapshot time and memmapping them would
remove both the startup cost and the duplication.

### 5.4 Tune the Hybrid backend on these numbers

`HybridMetadataBackend` already routes between LMDB and ES on input size, with
`node_threshold=2000` and `edge_threshold=None` (always LMDB). The measurements
argue for routing on *operation shape* rather than size alone:

- whole-category enumeration → **LMDB** (0.80 s vs 77.4 s)
- candidate-list category filtering → **ES** (5.0 vs 2.0 req/s)
- edge metadata filters → **LMDB** (0.008 s vs 0.171 s)
- anything on a threaded server → **ES**, because LMDB collapses

A hybrid that keeps edge filters local and pushes category work to ES would beat
either backend alone, and would keep the GIL off if the LMDB env is opened only
in worker processes that need it.

### 5.5 Fix the LMDB category scan

LMDB's `filter_nodes(ids, category=…)` scans the whole category index
independently of `len(ids)` — 51,704 entries to filter 500 candidates. Intersecting
the other way (look up each candidate's categories, or keep a per-node category
bitmap) would turn an O(|category|) scan into O(|ids|), improving both latency and
the GIL convoying that destroys thread scaling.

### 5.6 Reduce per-path cost before adding hardware

The remaining gap to gandalf on large result sets is per-path Python tuple
construction (see [`benchmarks-vs-gandalf-2026-07-19.md`](benchmarks-vs-gandalf-2026-07-19.md)).
A `PathArrays`-style representation would cut CPU per request, which multiplies
across every worker — cheaper than provisioning more of them.

### 5.7 Admission control, using what already exists

`MatchStats.truncated` already reports when a hop cap bit. Under load that is a
usable signal for shedding or degrading: reject or downgrade requests whose
frontier explodes rather than letting one 20-second query occupy a worker. Pair
it with a per-request deadline, since the cost function is now well understood —
selective targets are cheap, high-fan-in targets are not.

### 5.8 Deployment shape

- **csrgraph + ES**: threads inside each process, processes to fill cores, ES
  scaled independently. Highest ceiling; ES becomes the thing to size and watch.
- **csrgraph + LMDB**: single-threaded processes, vertical scaling, no external
  dependency. Lowest operational surface, hard per-host ceiling.
- Either way the graph snapshot is 34 MB and loads in 0.75 s, so workers are
  cheap to start and to replace — a real advantage for autoscaling and rolling
  updates that the release plan in
  [`production-release-plan.md`](production-release-plan.md) can build on.
