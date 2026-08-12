"""csrgraph half of the head-to-head against gandalf (run in the csrgraph venv).

Selects query fixtures, benchmarks csrgraph on them, and writes results to JSON
for compare_report.py to line up against the gandalf half.

Both engines are compared on *distinct node paths* rather than raw path objects:
gandalf returns node quadruples with duplicate triples preserved, csrgraph
returns predicate-annotated edges with duplicates collapsed, so the node-path
set is the only common denominator.

Usage::

    .venv/bin/python compare_csrgraph.py --out /tmp/cmp_csrgraph.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import time
from pathlib import Path

from csrgraph_kgx import CSRGraph
from metadata_db import LMDBMetadataBackend

DATA = Path("~/tmp/csrgraph_data").expanduser()
STEM = "translator_kg_2026-07-19"


def rss_gb() -> float:
    n = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return n / 1024**3 if os.uname().sysname == "Darwin" else n / 1024**2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cmp_csrgraph.json")
    ap.add_argument("--pairs-out", default="/tmp/cmp_pairs.json")
    args = ap.parse_args()

    logging.disable(logging.WARNING)  # truncation warnings are handled via stats

    t0 = time.perf_counter()
    g = CSRGraph.load(str(DATA / f"{STEM}.csrgraph.pkl.zst"))
    load_s = time.perf_counter() - t0
    db = LMDBMetadataBackend(str(DATA / f"{STEM}.metadata.lmdb"))
    g.set_db(db)

    out: dict = {
        "engine": "csrgraph",
        "load_s": round(load_s, 3),
        "rss_gb_after_load": round(rss_gb(), 2),
        "num_nodes": g.num_nodes,
        "distinct_edges": sum(int(m.nnz) for m in g.csr_by_relation.values()),
        "raw_triples": g.edge_count,
        "predicates": len(g.csr_by_relation),
    }

    indptr = g.csr_merged.indptr

    def out_deg(curie: str) -> int:
        i = g.node_to_id[curie]
        return int(indptr[i + 1] - indptr[i])

    # ---- fixtures: a start gene, then 3-hop-reachable endpoints ------------
    start = max(
        (n for n in g.node_to_id if n.startswith("NCBIGene:")),
        key=lambda n: out_deg(n) if out_deg(n) < 400 else -1,
    )
    print(f"start: {start} (out-degree {out_deg(start)})")

    # Endpoints at exactly 3 hops, spread across path-count difficulty.
    probe = g.match_path(
        [start, None, None, None, None, None, {"category": "biolink:Disease"}],
        limit=4000, node_subclassing=False, db=db,
    )
    seen: dict[str, int] = {}
    for p in probe:
        seen[p[-1][2]] = seen.get(p[-1][2], 0) + 1
    ranked = sorted(seen.items(), key=lambda kv: kv[1])
    picks = []
    for frac in (0.05, 0.35, 0.65, 0.95):
        if ranked:
            picks.append(ranked[min(int(len(ranked) * frac), len(ranked) - 1)][0])
    ends = list(dict.fromkeys(picks))
    print(f"endpoints: {ends}")

    json.dump({"start": start, "ends": ends}, open(args.pairs_out, "w"), indent=1)

    # ---- 1-hop expansion --------------------------------------------------
    one = []
    for node in [start] + ends[:2]:
        t0 = time.perf_counter()
        paths = g.match_path([node, None, None], limit=10**7, db=db)
        dt = time.perf_counter() - t0
        node_paths = {(e[0], e[2]) for p in paths for e in p}
        one.append({
            "node": node,
            "secs": round(dt, 4),
            "paths": len(paths),
            "distinct_node_paths": len(node_paths),
        })
        print(f"  1-hop {node}: {dt:.4f}s  {len(paths):,} paths, "
              f"{len(node_paths):,} distinct")
    out["one_hop"] = one

    # ---- 3-hop between two pinned endpoints (the head-to-head) ------------
    three = []
    for end in ends:
        spec = [start, None, None, None, None, None, end]
        # Warm first: the initial call also builds the reverse adjacency and
        # reach masks, which are cached per graph.  gandalf's side is measured
        # warm too, so timing both cold would compare setup, not traversal.
        cold0 = time.perf_counter()
        g.match_path(spec, limit=10**7, node_subclassing=False, db=db)
        cold_s = time.perf_counter() - cold0

        t0 = time.perf_counter()
        paths, stats = g.match_path(
            spec, limit=10**7, node_subclassing=False, db=db, return_stats=True,
        )
        dt = time.perf_counter() - t0
        node_paths = sorted({
            (p[0][0], p[0][2], p[1][2], p[2][2]) for p in paths
        })
        three.append({
            "start": start,
            "end": end,
            "secs": round(dt, 4),
            "cold_secs": round(cold_s, 4),
            "paths": len(paths),
            "distinct_node_paths": len(node_paths),
            "truncated": stats.truncated,
            "frontier_sizes": stats.frontier_sizes,
            "sample": [list(t) for t in node_paths[:5]],
        })
        print(f"  3-hop {start} -> {end}: warm {dt:.3f}s (cold {cold_s:.3f}s)  "
              f"{len(paths):,} paths, {len(node_paths):,} distinct node-paths, "
              f"truncated={stats.truncated}")
    out["three_hop"] = three
    out["rss_gb_peak"] = round(rss_gb(), 2)

    json.dump(out, open(args.out, "w"), indent=1)
    print(f"\nwrote {args.out}")
    db.close()


if __name__ == "__main__":
    main()
