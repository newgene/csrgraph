"""Hard-case 3-hop comparison: high-degree start, pinned end.

The earlier fixtures were small (a few hundred to ~1.5k paths) and showed
csrgraph within ~3x of gandalf. Meet-in-the-middle should pay off precisely
where the *forward* frontier explodes but the answer set stays small, so this
picks the highest-degree start nodes available and pins an end 3 hops away.

This is the measurement that decides whether csrgraph should adopt bidirectional
enumeration, so it deliberately favours the shape gandalf is built for.

Run the csrgraph side in the csrgraph venv, the gandalf side in gandalf's::

    .venv/bin/python compare_hard.py --engine csrgraph
    ~/tmp/gandalf_latest/.venv/bin/python compare_hard.py --engine gandalf
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

PAIRS = "/tmp/cmp_hard_pairs.json"


def run_csrgraph():
    from pathlib import Path

    from csrgraph_kgx import CSRGraph
    from metadata_db import LMDBMetadataBackend

    D = Path("~/tmp/csrgraph_data").expanduser()
    STEM = "translator_kg_2026-07-19"
    g = CSRGraph.load(str(D / f"{STEM}.csrgraph.pkl.zst"))
    db = LMDBMetadataBackend(str(D / f"{STEM}.metadata.lmdb"))
    g.set_db(db)

    indptr = g.csr_merged.indptr
    deg = indptr[1:] - indptr[:-1]

    # Three progressively wider starts, by out-degree percentile.
    import numpy as np

    order = np.argsort(deg)
    cases = []
    for pct in (0.9999, 0.99999, 1.0):
        i = int(order[min(int(len(order) * pct), len(order) - 1)])
        start = g.nodes[i]
        # Find a 3-hop endpoint that is genuinely reachable.
        probe = g.match_path(
            [start, None, None, None, None, None, None],
            limit=20000, node_subclassing=False, db=db,
        )
        if not probe:
            continue
        counts: dict[str, int] = {}
        for p in probe:
            counts[p[-1][2]] = counts.get(p[-1][2], 0) + 1
        end = max(counts.items(), key=lambda kv: kv[1])[0]
        cases.append({"start": start, "end": end, "start_degree": int(deg[i])})

    json.dump(cases, open(PAIRS, "w"), indent=1)

    out = []
    for c in cases:
        spec = [c["start"], None, None, None, None, None, c["end"]]
        g.match_path(spec, limit=10**7, node_subclassing=False, db=db)  # warm
        t0 = time.perf_counter()
        paths, stats = g.match_path(
            spec, limit=10**7, node_subclassing=False, db=db, return_stats=True
        )
        dt = time.perf_counter() - t0
        nodeset = {(p[0][0], p[0][2], p[1][2], p[2][2]) for p in paths}
        rec = {**c, "secs": round(dt, 4), "paths": len(paths),
               "distinct_node_paths": len(nodeset),
               "frontier_sizes": stats.frontier_sizes,
               "truncated": stats.truncated}
        out.append(rec)
        print(f"  deg={c['start_degree']:>6} {c['start']} -> {c['end']}: "
              f"{dt:8.3f}s  {len(paths):,} paths, {len(nodeset):,} distinct, "
              f"frontiers={stats.frontier_sizes}, truncated={stats.truncated}")
    json.dump(out, open("/tmp/cmp_hard_csrgraph.json", "w"), indent=1)
    db.close()


def run_gandalf():
    from gandalf.graph import CSRGraph
    from gandalf.search.path_finder import _find_3hop_paths_directed_idx

    g = CSRGraph.load_mmap(os.path.expanduser("~/tmp/gandalf_data/graph_2026-07-19_mmap"))
    cases = json.load(open(PAIRS))
    out = []
    for c in cases:
        si, ei = g.get_node_idx(c["start"]), g.get_node_idx(c["end"])
        if si is None or ei is None:
            print(f"  skip {c['start']} -> {c['end']} (not in graph)")
            continue
        _find_3hop_paths_directed_idx(g, si, ei)  # warm
        t0 = time.perf_counter()
        paths = _find_3hop_paths_directed_idx(g, si, ei)
        dt = time.perf_counter() - t0
        nodeset = {
            (g.get_node_id(p[0]), g.get_node_id(p[1]),
             g.get_node_id(p[2]), g.get_node_id(p[3])) for p in paths
        }
        out.append({**c, "secs": round(dt, 4), "paths": len(paths),
                    "distinct_node_paths": len(nodeset)})
        print(f"  deg={c['start_degree']:>6} {c['start']} -> {c['end']}: "
              f"{dt:8.3f}s  {len(paths):,} paths, {len(nodeset):,} distinct")
    json.dump(out, open("/tmp/cmp_hard_gandalf.json", "w"), indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["csrgraph", "gandalf"], required=True)
    a = ap.parse_args()
    logging.disable(logging.WARNING)
    run_csrgraph() if a.engine == "csrgraph" else run_gandalf()
