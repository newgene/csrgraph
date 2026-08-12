"""Multi-PROCESS throughput for the real match_path query path.

The threaded PYTHON_GIL=0 option only matters if it beats plain processes, which
are fully supported. Same query as bench_concurrency.py so numbers line up.
"""
import argparse, json, logging, os, subprocess, sys, time
from pathlib import Path

DATA = Path("~/tmp/csrgraph_data").expanduser()
STEM = "translator_kg_2026-07-19"
START, CATEGORY = "NCBIGene:10425", "biolink:Disease"


def child(backend, seconds):
    logging.disable(logging.WARNING)
    from csrgraph_kgx import CSRGraph
    if backend == "lmdb":
        from metadata_db import LMDBMetadataBackend as B
        db = B(str(DATA / f"{STEM}.metadata.lmdb"))
    else:
        from metadata_db import ElasticsearchMetadataBackend as B
        db = B([f"http://localhost:{p}" for p in (9200, 9201, 9202)],
               index_prefix=STEM, connections_per_node=64)
    g = CSRGraph.load(str(DATA / f"{STEM}.csrgraph.pkl.zst"))
    spec = [START, None, None, None, {"category": CATEGORY}]
    g.match_path(spec, limit=500, node_subclassing=False, db=db)  # warm
    print("READY", flush=True)
    sys.stdin.readline()                      # wait for the go signal
    n, end = 0, time.perf_counter() + seconds
    while time.perf_counter() < end:
        g.match_path(spec, limit=500, node_subclassing=False, db=db)
        n += 1
    print(json.dumps({"count": n}), flush=True)


def parent(backend, workers, seconds):
    procs = []
    for _ in range(workers):
        p = subprocess.Popen([sys.executable, "-u", __file__, "child",
                              "--backend", backend, "--seconds", str(seconds)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        procs.append(p)
    for p in procs:                            # wait until all are warm
        while "READY" not in (p.stdout.readline() or "READY"):
            pass
    t0 = time.perf_counter()
    for p in procs:
        p.stdin.write("go\n"); p.stdin.flush()
    total = 0
    for p in procs:
        while True:
            line = p.stdout.readline()
            if not line:
                break
            try:
                total += json.loads(line)["count"]; break
            except json.JSONDecodeError:
                continue
    dt = time.perf_counter() - t0
    print(f"  {workers:>2} processes: {total/dt:8.2f} req/s")
    for p in procs:
        p.kill()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["parent", "child"])
    ap.add_argument("--backend", default="lmdb")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=6.0)
    a = ap.parse_args()
    child(a.backend, a.seconds) if a.mode == "child" else parent(a.backend, a.workers, a.seconds)
