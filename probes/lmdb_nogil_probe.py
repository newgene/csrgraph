"""Bounded probe: does py-lmdb make progress with the GIL genuinely off?"""
import sys, threading, time
from pathlib import Path
from metadata_db import LMDBMetadataBackend

D = Path("~/tmp/csrgraph_data").expanduser()
db = LMDBMetadataBackend(str(D / "translator_kg_2026-07-19.metadata.lmdb"))
ids = db.nodes_by_category("biolink:Disease", limit=50)
print(f"GIL enabled: {sys._is_gil_enabled()}   ids={len(ids)}", flush=True)

progress = [0] * 4
stop = threading.Event()

def work(i):
    while not stop.is_set():
        for nid in ids:
            db.get_node(nid)
            progress[i] += 1
            if stop.is_set():
                return

print("single-threaded rate:", flush=True)
t0 = time.perf_counter()
for nid in ids:
    db.get_node(nid)
print(f"  {len(ids)/(time.perf_counter()-t0):,.0f} get_node/s", flush=True)

for n in (2, 4):
    progress[:] = [0] * 4
    stop.clear()
    ts = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    time.sleep(3.0)
    stop.set()
    for t in ts:
        t.join(timeout=5.0)
    alive = [t.is_alive() for t in ts]
    dt = time.perf_counter() - t0
    print(f"  {n} threads: {sum(progress)/dt:,.0f} get_node/s  "
          f"stuck_threads={sum(alive)}  progress={progress[:n]}", flush=True)
print("done", flush=True)
