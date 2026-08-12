"""gandalf half of the head-to-head against csrgraph (run in the GANDALF venv).

Reads the fixtures compare_csrgraph.py chose and runs the equivalent queries
through gandalf, so both engines answer the same questions on the same graph.

gandalf's 3-hop kernel is start -> n1 -> n2 -> end over forward edges, with the
forward 2-hop frontier intersected against end's incoming neighbours -- i.e. a
vectorized meet-in-the-middle. csrgraph answers the same shape with a pruned
left-to-right frontier walk. Comparing them is also the empirical test of
whether bidirectional enumeration is worth adding to csrgraph.

Usage (from the gandalf checkout):

    .venv/bin/python /path/to/compare_gandalf.py --out /tmp/cmp_gandalf.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import time

GRAPH = os.path.expanduser("~/tmp/gandalf_data/graph_2026-07-19_mmap")


def rss_gb() -> float:
    n = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return n / 1024**3 if os.uname().sysname == "Darwin" else n / 1024**2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/cmp_gandalf.json")
    ap.add_argument("--pairs", default="/tmp/cmp_pairs.json")
    args = ap.parse_args()

    logging.disable(logging.INFO)

    from gandalf.graph import CSRGraph
    from gandalf.search.path_finder import _find_3hop_paths_directed_idx

    fx = json.load(open(args.pairs))
    start, ends = fx["start"], fx["ends"]

    t0 = time.perf_counter()
    g = CSRGraph.load_mmap(GRAPH)
    load_s = time.perf_counter() - t0

    out: dict = {
        "engine": "gandalf",
        "load_s": round(load_s, 3),
        "rss_gb_after_load": round(rss_gb(), 2),
        "num_nodes": int(g.num_nodes),
        "fwd_edges": int(g.fwd_offsets[-1]),
        "predicates": len(g.predicate_to_idx),
    }

    def idx(curie):
        return g.get_node_idx(curie)

    # ---- 1-hop expansion --------------------------------------------------
    one = []
    for node in [start] + ends[:2]:
        i = idx(node)
        t0 = time.perf_counter()
        nbrs = g.neighbors(i)
        dt = time.perf_counter() - t0
        one.append({
            "node": node,
            "secs": round(dt, 6),
            "paths": int(len(nbrs)),
            "distinct_node_paths": int(len(set(nbrs.tolist()))),
        })
        print(f"  1-hop {node}: {dt:.6f}s  {len(nbrs):,} nbrs, "
              f"{len(set(nbrs.tolist())):,} distinct")
    out["one_hop"] = one

    # ---- 3-hop between two pinned endpoints -------------------------------
    three = []
    for end in ends:
        si, ei = idx(start), idx(end)
        _find_3hop_paths_directed_idx(g, si, ei)  # warm
        t0 = time.perf_counter()
        paths = _find_3hop_paths_directed_idx(g, si, ei)
        dt = time.perf_counter() - t0
        node_paths = sorted({
            (g.get_node_id(p[0]), g.get_node_id(p[1]),
             g.get_node_id(p[2]), g.get_node_id(p[3]))
            for p in paths
        })
        three.append({
            "start": start,
            "end": end,
            "secs": round(dt, 4),
            "paths": len(paths),
            "distinct_node_paths": len(node_paths),
            "sample": [list(t) for t in node_paths[:5]],
            "checksum": hash(tuple(node_paths)),
        })
        print(f"  3-hop {start} -> {end}: {dt:.3f}s  {len(paths):,} paths, "
              f"{len(node_paths):,} distinct node-paths")
    out["three_hop"] = three
    out["rss_gb_peak"] = round(rss_gb(), 2)

    json.dump(out, open(args.out, "w"), indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
