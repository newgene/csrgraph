"""Benchmark: per-frontier-node vs. batched metadata filtering in ``match_path``.

Loads the local ``translator_kg_2026-07-19`` snapshot with the LMDB metadata backend once,
then runs the same multi-hop association query through two code paths:

* **old** — ``csrgraph_kgx.py`` at git HEAD, which calls ``filter_edges`` /
  ``filter_nodes`` once per frontier node.
* **new** — the working tree, which batches those calls per hop.

Both run against the *same* graph object, so the only difference is the
traversal code.  The new path runs first, so the old path gets the warmer LMDB
page cache — biasing the comparison against the change being measured.

Because the old ``match_path`` runs against a *current* graph instance, it can
only reach private helpers that still exist by that name.  If a ref renamed one,
compare against a ref from before that rename instead.

Usage::

    .venv/bin/python bench_match_path.py [--hops 2,3] [--limit 100000] [--old-ref REF]
"""

import argparse
import importlib.util
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from csrgraph_kgx import CSRGraph
from metadata_db import LMDBMetadataBackend, MetadataBackend

DATA = Path("~/tmp/csrgraph_data").expanduser()

# A real Gene with a moderate fan-out: 245 direct neighbours, ~4.1k at 2 hops.
START = "NCBIGene:11640"
TARGET_CATEGORY = "biolink:Disease"


def load_old_module(ref: str):
    """Import ``csrgraph_kgx.py`` as of *ref* under its own module name.

    Extracts the file from git rather than expecting a checkout, so the
    before/after comparison keeps working after the change is committed (point
    ``--old-ref`` at the commit before it).
    """
    src = subprocess.run(
        ["git", "show", f"{ref}:csrgraph_kgx.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp()) / "csrgraph_kgx_old.py"
    tmp.write_text(src)

    spec = importlib.util.spec_from_file_location("csrgraph_kgx_old", tmp)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {tmp}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["csrgraph_kgx_old"] = mod
    spec.loader.exec_module(mod)
    return mod


class CountingBackend(MetadataBackend):
    """Transparent proxy that tallies calls and time spent in the backend.

    Subclasses ``MetadataBackend`` only so the graph's type hints accept it;
    every method other than the two counted ones is delegated via
    ``__getattr__``.
    """

    def __init__(self, inner):
        self._inner = inner
        self.reset()

    def reset(self):
        self.calls = {"filter_nodes": 0, "filter_edges": 0}
        self.secs = {"filter_nodes": 0.0, "filter_edges": 0.0}

    def _timed(self, name, fn, *a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            self.calls[name] += 1
            self.secs[name] += time.perf_counter() - t0

    def filter_nodes(self, *a, **kw):
        return self._timed("filter_nodes", self._inner.filter_nodes, *a, **kw)

    def filter_edges(self, *a, **kw):
        return self._timed("filter_edges", self._inner.filter_edges, *a, **kw)

    # Remaining abstract methods: plain delegation (uncounted).
    def get_node(self, node_id):
        return self._inner.get_node(node_id)

    def get_edge(self, subject, predicate, obj):
        return self._inner.get_edge(subject, predicate, obj)

    def close(self):
        return self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def build_spec(hops: int, end: str | None = None) -> list:
    """Alternating spec: pinned start, wildcard middles, and a tail.

    The tail is a category filter (what ``kg_query.associations`` builds) unless
    *end* pins a specific node (what ``kg_query.connect`` needs).
    """
    spec: list = [START]
    for _ in range(hops - 1):
        spec += [None, None]
    spec += [None, end if end is not None else {"category": TARGET_CATEGORY}]
    return spec


def discover_end(g, db, hops: int) -> str | None:
    """Find a real node reachable from START in exactly *hops* hops, to pin as a tail."""
    paths = g.match_path(
        build_spec(hops), limit=200, node_subclassing=True, db=db
    )
    return paths[-1][-1][2] if paths else None


def run(label, fn, counter, spec, limit):
    counter.reset()
    t0 = time.perf_counter()
    paths = fn(spec, limit)
    wall = time.perf_counter() - t0
    print(
        f"  {label:5s} wall={wall:9.2f}s  paths={len(paths):6,d}  "
        f"filter_nodes={counter.calls['filter_nodes']:7,d} "
        f"({counter.secs['filter_nodes']:8.2f}s)  "
        f"filter_edges={counter.calls['filter_edges']:5,d}"
    )
    return paths, wall, dict(counter.calls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hops", default="2,3")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument(
        "--old-ref", default="HEAD",
        help="git ref to take the pre-batching csrgraph_kgx.py from",
    )
    ap.add_argument(
        "--pin-end", action="store_true",
        help="pin the tail to a reachable node (connect-style) instead of a category",
    )
    args = ap.parse_args()

    old = load_old_module(args.old_ref)

    inner = LMDBMetadataBackend(str(DATA / "translator_kg_2026-07-19.metadata.lmdb"))
    db = CountingBackend(inner)
    g = CSRGraph.load(str(DATA / "translator_kg_2026-07-19.csrgraph.pkl.zst"), db=db)
    print(f"graph: {len(g.nodes):,} nodes, {g.csr_merged.nnz:,} edges")
    print(f"start: {START}  target category: {TARGET_CATEGORY}  limit={args.limit}\n")

    # Warm the LMDB page cache so neither path pays first-touch costs.
    g.match_path([START, None, None], limit=10, db=db)

    for hops in [int(h) for h in args.hops.split(",")]:
        end = None
        if args.pin_end:
            end = discover_end(g, db, hops)
            if end is None:
                print(f"{hops}-hop: no reachable endpoint found, skipping\n")
                continue
        spec = build_spec(hops, end)
        print(f"{hops}-hop  spec={spec}")

        # NEW first, so OLD runs against the warmer cache.
        new_paths, new_wall, new_calls = run(
            "new",
            lambda s, lim: g.match_path(s, limit=lim, node_subclassing=True, db=db),
            db, spec, args.limit,
        )
        old_paths, old_wall, old_calls = run(
            "old",
            lambda s, lim: old.CSRGraph.match_path(
                g, s, limit=lim, node_subclassing=True, db=db
            ),
            db, spec, args.limit,
        )

        same = new_paths == old_paths
        print(f"  identical results: {same}")
        if not same:
            print(f"    new={len(new_paths)} old={len(old_paths)}")
            ns, os_ = {tuple(p) for p in new_paths}, {tuple(p) for p in old_paths}
            print(f"    new-only={len(ns - os_)} old-only={len(os_ - ns)}")
        call_drop = old_calls["filter_nodes"] / max(1, new_calls["filter_nodes"])
        print(
            f"  speedup: {old_wall / max(new_wall, 1e-9):,.0f}x wall, "
            f"{call_drop:,.0f}x fewer filter_nodes calls\n"
        )

    inner.close()


if __name__ == "__main__":
    main()
