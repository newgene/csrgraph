"""Does threaded LMDB reading produce CORRECT results, or just slow ones?

Distinguishes thread-safety (correctness) from GIL contention (performance).
Each thread performs the same reads; every thread must agree with the
single-threaded baseline.
"""
import hashlib, json, os, sys, threading
from pathlib import Path
from metadata_db import LMDBMetadataBackend

D = Path("~/tmp/csrgraph_data").expanduser()
db = LMDBMetadataBackend(str(D / "translator_kg_2026-07-19.metadata.lmdb"))

# Deterministic workload: point lookups + a category filter + edge lookups.
ids = db.nodes_by_category("biolink:Disease", limit=300)

def work():
    h = hashlib.sha256()
    for nid in ids:
        n = db.get_node(nid)
        h.update(json.dumps(n, sort_keys=True, default=str).encode())
    got = db.filter_nodes(ids, category="biolink:Disease")
    h.update(str(len(got)).encode())
    return h.hexdigest()

baseline = work()
for nthreads in (2, 4, 8):
    results, errors = [None] * nthreads, [None] * nthreads
    def run(i):
        try:
            results[i] = work()
        except BaseException as e:
            errors[i] = repr(e)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(nthreads)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ok = all(r == baseline for r in results) and not any(errors)
    print(f"  {nthreads} threads: correct={ok}  distinct_digests={len(set(results))}"
          f"  errors={[e for e in errors if e] or 'none'}")
db.close()
print(f"GIL enabled: {sys._is_gil_enabled() if hasattr(sys,'_is_gil_enabled') else 'n/a'}")
