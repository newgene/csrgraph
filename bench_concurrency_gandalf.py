"""Thread scaling and per-worker memory for gandalf (run in the gandalf venv).

gandalf targets a gunicorn fleet: many single-threaded worker *processes* sharing
one in-RAM copy of the graph via mmap copy-on-write. This measures both halves of
that claim -- what threads buy inside one process (gandalf runs on a standard
GIL build, so: little), and what an extra worker process actually costs.

    ~/tmp/gandalf_latest/.venv/bin/python bench_concurrency_gandalf.py threads
    ~/tmp/gandalf_latest/.venv/bin/python bench_concurrency_gandalf.py procs
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

GRAPH = os.path.expanduser("~/tmp/gandalf_data/graph_2026-07-19_mmap")
START = "NCBIGene:10425"
END = "MONDO:0006679"


def sys_used_gb() -> float:
    import psutil

    vm = psutil.virtual_memory()
    return (vm.total - vm.available) / 1024**3


def rss_gb(pid=None) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid or os.getpid())],
                         capture_output=True, text=True).stdout.strip()
    return (int(out) / 1024**2) if out else float("nan")


def load_and_query():
    from gandalf.graph import CSRGraph
    from gandalf.search.path_finder import _find_3hop_paths_directed_idx

    g = CSRGraph.load_mmap(GRAPH)
    si, ei = g.get_node_idx(START), g.get_node_idx(END)
    return g, (lambda: _find_3hop_paths_directed_idx(g, si, ei))


def run_threads(args):
    import sysconfig

    g, query = load_and_query()
    n0 = len(query())
    print(f"gandalf  free-threaded build="
          f"{bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}  "
          f"python={sys.version.split()[0]}")
    print(f"one request returns {n0:,} paths\n")

    base = None
    results = []
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
        qps = sum(counts) / dt
        base = qps if base is None else base
        results.append({"workers": w, "qps": round(qps, 1),
                        "scaling": round(qps / base, 2)})
        print(f"  {w:>2} threads: {qps:8.1f} req/s   scaling x{qps / base:.2f}")
    json.dump({"engine": "gandalf", "results": results},
              open("/tmp/conc_threads_gandalf.json", "w"), indent=1)


def _child():
    g, query = load_and_query()
    query()
    import psutil
    fi = psutil.Process().memory_full_info()
    print(json.dumps({"rss_gb": round(fi.rss / 1024**3, 3),
                      "uss_gb": round(fi.uss / 1024**3, 3)}), flush=True)
    time.sleep(3600)


def run_procs(args):
    print("worker      RSS      USS (private)")
    procs = []
    before = sys_used_gb()
    prev = before
    for i in range(1, max(int(x) for x in args.workers.split(",")) + 1):
        p = subprocess.Popen([sys.executable, __file__, "child"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
        procs.append(p)
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
    print("\nRSS includes shared read-only mmap pages; USS is the private cost.")
    for p in procs:
        p.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["threads", "procs", "child"])
    ap.add_argument("--workers", default="1,2,4,8")
    ap.add_argument("--seconds", type=float, default=5.0)
    a = ap.parse_args()
    logging.disable(logging.INFO)
    if a.mode == "threads":
        run_threads(a)
    elif a.mode == "procs":
        run_procs(a)
    else:
        _child()
