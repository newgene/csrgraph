"""Micro-benchmark: wildcard neighbour expansion, old vs new inner loop.

Isolates ``_mp_expand_edges`` from metadata filtering so the cost of the
per-relation iteration can be seen directly, and reports what share of a real
multi-hop query it actually accounts for.
"""

import argparse
import cProfile
import importlib.util
import pstats
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from csrgraph_kgx import CSRGraph, _mp_expand_edges
from metadata_db import LMDBMetadataBackend

DATA = Path("~/tmp/csrgraph_data").expanduser()
START = "NCBIGene:11640"


def load_old(ref: str):
    src = subprocess.run(
        ["git", "show", f"{ref}:csrgraph_kgx.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "csrgraph_kgx_old.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("csrgraph_kgx_old", tmp)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["csrgraph_kgx_old"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-ref", default="HEAD")
    ap.add_argument("--nodes", type=int, default=20000)
    args = ap.parse_args()

    old = load_old(args.old_ref)
    db = LMDBMetadataBackend(str(DATA / "translator_kg_2026-07-19.metadata.lmdb"))
    g = CSRGraph.load(str(DATA / "translator_kg_2026-07-19.csrgraph.pkl.zst"), db=db)
    print(f"relations: {len(g.csr_by_relation)}")

    # Sample frontier: the 2-hop neighbourhood of START, which is what a real
    # 3-hop query expands at its widest.
    frontier = [nbr for nbr, _ in _mp_expand_edges(g, START, None)]
    seen, wide = set(), []
    for n in frontier:
        for nbr, _ in _mp_expand_edges(g, n, None):
            if nbr not in seen:
                seen.add(nbr)
                wide.append(nbr)
        if len(wide) >= args.nodes:
            break
    wide = wide[:args.nodes]
    print(f"expanding {len(wide):,} distinct frontier nodes (wildcard edge spec)\n")

    for label, fn in (("old", old._mp_expand_edges), ("new", _mp_expand_edges)):
        fn(g, wide[0], None)  # warm
        t0 = time.perf_counter()
        total = 0
        for n in wide:
            total += len(fn(g, n, None))
        dt = time.perf_counter() - t0
        print(
            f"  {label}: {dt:7.3f}s for {total:,} pairs "
            f"({dt / len(wide) * 1e6:7.1f} us/node, {total / dt / 1e6:.2f}M pairs/s)"
        )

    # Where does a real 3-hop query spend its time?
    print("\ntop cumulative costs of a 3-hop associations query (limit=100000):")
    spec = [START, None, None, None, None, None, {"category": "biolink:Disease"}]
    pr = cProfile.Profile()
    pr.enable()
    g.match_path(spec, limit=100_000, node_subclassing=True, db=db)
    pr.disable()
    st = pstats.Stats(pr)
    st.sort_stats("cumulative").print_stats(12)

    db.close()


if __name__ == "__main__":
    main()
