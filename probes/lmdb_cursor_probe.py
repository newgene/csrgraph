"""Isolate the cursor-scan path (filter_nodes with category) under threads."""
import sys, threading, time
from pathlib import Path
from metadata_db import LMDBMetadataBackend

D = Path("~/tmp/csrgraph_data").expanduser()
db = LMDBMetadataBackend(str(D / "translator_kg_2026-07-19.metadata.lmdb"))
ids = db.nodes_by_category("biolink:Disease", limit=200)
print(f"GIL enabled: {sys._is_gil_enabled()}", flush=True)

t0 = time.perf_counter()
n = len(db.filter_nodes(ids, category="biolink:Disease"))
single = time.perf_counter() - t0
print(f"single-threaded filter_nodes(category): {single:.3f}s -> {n} hits", flush=True)

done = [0] * 4
def work(i, budget):
    end = time.perf_counter() + budget
    while time.perf_counter() < end:
        db.filter_nodes(ids, category="biolink:Disease")
        done[i] += 1

for nt in (2, 4):
    done[:] = [0] * 4
    ts = [threading.Thread(target=work, args=(i, 6.0), daemon=True) for i in range(nt)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    for t in ts:
        t.join(timeout=25.0)
    stuck = sum(t.is_alive() for t in ts)
    dt = time.perf_counter() - t0
    total = sum(done)
    print(f"  {nt} threads: {total} calls in {dt:.1f}s = {total/dt:.2f} calls/s "
          f"(single={1/single:.2f}/s) stuck={stuck} per_thread={done[:nt]}", flush=True)
print("done", flush=True)
