"""Concurrency evaluation for csrgraph: thread scaling and per-worker memory.

Two things decide how csrgraph serves concurrent traffic:

1. **Whether the GIL is actually off.** The project venv is a free-threaded
   build, but ``import lmdb`` re-enables the GIL because ``lmdb.cpython`` has not
   declared free-threaded safety. So the *backend choice* changes the threading
   ceiling: ES-only keeps the GIL off, LMDB puts it back on.
2. **Whether the graph is shared across processes.** Both the snapshot memmap and
   the LMDB store are mapped read-only, so extra worker processes should cost
   far less than a full copy.

Modes::

    bench_concurrency.py threads --backend none|lmdb|es --workers 1,2,4,8
    bench_concurrency.py procs   --workers 1,2,4       # per-worker memory cost
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DATA = Path("~/tmp/csrgraph_data").expanduser()
STEM = "translator_kg_2026-07-19"
ES_HOSTS = [f"http://localhost:{p}" for p in (9200, 9201, 9202)]

# Fixtures: a pinned 3-hop pair (pure topology, no backend calls) and a
# category-tail association (one backend call per hop batch).
START = "NCBIGene:10425"
END = "MONDO:0006679"
CATEGORY = "biolink:Disease"


def sys_used_gb() -> float:
    """System-wide memory in use, for measuring the true cost of a worker."""
    try:
        import psutil

        vm = psutil.virtual_memory()
        return (vm.total - vm.available) / 1024**3
    except ImportError:
        return float("nan")


def rss_gb(pid: int | None = None) -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid or os.getpid())],
        capture_output=True, text=True,
    ).stdout.strip()
    return (int(out) / 1024**2) if out else float("nan")


def load(backend: str):
    from csrgraph_kgx import CSRGraph

    g = CSRGraph.load(str(DATA / f"{STEM}.csrgraph.pkl.zst"))
    db = None
    if backend == "lmdb":
        from metadata_db import LMDBMetadataBackend

        db = LMDBMetadataBackend(str(DATA / f"{STEM}.metadata.lmdb"))
    elif backend == "es":
        from metadata_db import ElasticsearchMetadataBackend

        # All cluster nodes so the client round-robins, and a pool larger than
        # any concurrency we drive: connections_per_node caps in-flight requests
        # per node, so leaving it at the default 10 measures the client's queue
        # rather than Elasticsearch.
        db = ElasticsearchMetadataBackend(
            ES_HOSTS, index_prefix=STEM, connections_per_node=64
        )
    if db is not None:
        g.set_db(db)
    return g, db


def make_query(g, db, backend: str):
    """Return a zero-arg callable representing one request."""
    if backend == "none":
        spec = [START, None, None, None, None, None, END]

        class _Stub:  # match_path requires a db even when no spec consults it
            def filter_nodes(self, ids, **kw):
                return [{"id": i} for i in ids]

            def filter_edges(self, edges, **kw):
                return [{"subject": s, "predicate": p, "object": o}
                        for s, p, o in edges]

        stub = _Stub()
        return lambda: g.match_path(spec, limit=5000, db=stub)

    spec = [START, None, None, None, {"category": CATEGORY}]
    return lambda: g.match_path(spec, limit=500, node_subclassing=False, db=db)


def run_threads(args):
    logging.disable(logging.WARNING)
    g, db = load(args.backend)
    gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    print(f"backend={args.backend}  GIL enabled={gil}  "
          f"free-threaded build={bool(__import__('sysconfig').get_config_var('Py_GIL_DISABLED'))}")

    query = make_query(g, db, args.backend)
    n0 = len(query())  # warm caches (expansion plan, reach masks, page cache)
    print(f"one request returns {n0:,} paths\n")

    results = []
    base = None
    for w in [int(x) for x in args.workers.split(",")]:
        counts = [0] * w
        stop = threading.Event()

        def worker(i):
            c = 0
            while not stop.is_set():
                query()
                c += 1
            counts[i] = c

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(w)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(args.seconds)
        stop.set()
        for t in threads:
            t.join()
        dt = time.perf_counter() - t0

        total = sum(counts)
        qps = total / dt
        base = qps if base is None else base
        results.append({"workers": w, "qps": round(qps, 1),
                        "scaling": round(qps / base, 2)})
        print(f"  {w:>2} threads: {qps:8.1f} req/s   scaling x{qps / base:.2f}")

    if db is not None:
        db.close()
    json.dump({"backend": args.backend, "gil_enabled": gil, "results": results},
              open(f"/tmp/conc_threads_{args.backend}.json", "w"), indent=1)


def _child(backend: str):
    """Load the graph, report memory, and idle until killed.

    USS (unique set size) is the number that matters: RSS counts the read-only
    memmap pages every worker shares through the page cache, so it overstates
    what an extra worker actually costs.
    """
    g, db = load(backend)
    make_query(g, db, backend)()  # touch pages a request would touch
    import psutil

    fi = psutil.Process().memory_full_info()
    print(json.dumps({
        "rss_gb": round(fi.rss / 1024**3, 3),
        "uss_gb": round(fi.uss / 1024**3, 3),
    }), flush=True)
    time.sleep(3600)


def run_procs(args):
    """Fork N workers one at a time, measuring the marginal cost of each."""
    print("worker      RSS      USS (private)")
    procs: list[subprocess.Popen] = []
    before = sys_used_gb()
    prev = before
    for i in range(1, max(int(x) for x in args.workers.split(",")) + 1):
        p = subprocess.Popen(
            [sys.executable, __file__, "child", "--backend", args.backend],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        procs.append(p)
        # The child also prints load banners; take the first line that is JSON.
        info = None
        while info is None:
            line = p.stdout.readline() if p.stdout else ""
            if not line:
                raise RuntimeError("child exited before reporting")
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue
        print(f"  {i:>3}   {info['rss_gb']:7.3f} GB  {info['uss_gb']:7.3f} GB")
    print("\nRSS includes shared read-only memmap pages; USS is the private cost\n"
          "of each additional worker.")
    for p in procs:
        p.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["threads", "procs", "child"])
    ap.add_argument("--backend", default="none", choices=["none", "lmdb", "es"])
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--seconds", type=float, default=5.0)
    a = ap.parse_args()
    if a.mode == "threads":
        run_threads(a)
    elif a.mode == "procs":
        run_procs(a)
    else:
        logging.disable(logging.WARNING)
        _child(a.backend)
