"""End-to-end parity + timing check across the LMDB and Elasticsearch backends.

Runs the same queries against both metadata backends on one CSR snapshot and
asserts they agree.  The two backends index the same archive but answer through
completely different machinery (LMDB prefix scans vs. ES term queries), so
disagreement means one of them is wrong.

Usage::

    .venv/bin/python check_backends.py [--stem translator_kg_2026-07-19]
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from csrgraph_kgx import CSRGraph, _resolve_node_candidates
from metadata_db import ElasticsearchMetadataBackend, LMDBMetadataBackend

DATA = Path("~/tmp/csrgraph_data").expanduser()

# Query fixtures. The gene is resolved from the graph at runtime so this works
# on any snapshot; the categories are Biolink terms every Translator KG carries.
CATEGORIES = ["biolink:Disease", "biolink:Gene", "biolink:SmallMolecule"]


def timed(label: str, fn):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    n = len(out) if hasattr(out, "__len__") else out
    print(f"    {label:<34} {dt:7.3f}s  -> {n:,}" if isinstance(n, int)
          else f"    {label:<34} {dt:7.3f}s")
    return out, dt


def pick_start(graph: CSRGraph) -> str:
    """Highest-degree NCBIGene node: stable across snapshots, wide enough to matter."""
    indptr = graph.csr_merged.indptr
    best, best_deg = None, -1
    for nid, idx in graph.node_to_id.items():
        if not nid.startswith("NCBIGene:"):
            continue
        deg = int(indptr[idx + 1] - indptr[idx])
        if 100 < deg < 400 and deg > best_deg:
            best, best_deg = nid, deg
    return best or next(iter(graph.node_to_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stem", default="translator_kg_2026-07-19")
    ap.add_argument("--es-host", default="http://localhost:9200,http://localhost:9201,http://localhost:9202",
                    help="comma-separated node URLs")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    snapshot = DATA / f"{args.stem}.csrgraph.pkl.zst"
    lmdb = LMDBMetadataBackend(str(DATA / f"{args.stem}.metadata.lmdb"))
    es = ElasticsearchMetadataBackend(
        args.es_host.split(","), index_prefix=args.stem, connections_per_node=64
    )

    g = CSRGraph.load(str(snapshot))
    print(f"graph: {g.num_nodes:,} nodes, {g.csr_merged.nnz:,} edges, "
          f"{len(g.csr_by_relation)} relations")

    start = pick_start(g)
    deg = int(g.csr_merged.indptr[g.node_to_id[start] + 1]
              - g.csr_merged.indptr[g.node_to_id[start]])
    print(f"start node: {start} (out-degree {deg})\n")

    failures: list[str] = []

    def compare(name, lmdb_val, es_val):
        ok = lmdb_val == es_val
        print(f"    {'AGREE' if ok else 'DISAGREE':<8} {name}")
        if not ok:
            only_l = set(lmdb_val) - set(es_val)
            only_e = set(es_val) - set(lmdb_val)
            print(f"      lmdb-only={len(only_l)} es-only={len(only_e)}")
            for s in list(only_l)[:3]:
                print(f"        lmdb-only e.g. {s}")
            for s in list(only_e)[:3]:
                print(f"        es-only   e.g. {s}")
            failures.append(name)

    # ---- 1. nodes_by_category (this session's item 7) ----------------------
    print("1. nodes_by_category")
    for cat in CATEGORIES:
        (l_ids, l_t) = timed(f"lmdb {cat}", lambda c=cat: lmdb.nodes_by_category(c))
        (e_ids, e_t) = timed(f"es   {cat}", lambda c=cat: es.nodes_by_category(c))
        compare(f"nodes_by_category({cat})", sorted(l_ids), sorted(e_ids))
        print(f"      lmdb {l_t:.3f}s vs es {e_t:.3f}s")

    # ---- 2. category NodeSpec resolution through the graph helper ----------
    print("\n2. _resolve_node_candidates with a category NodeSpec")
    for cat in CATEGORIES[:1]:
        cat_spec: dict = {"category": cat}
        (l_r, _) = timed(f"lmdb {cat}", lambda: _resolve_node_candidates(cat_spec, g, lmdb))
        (e_r, _) = timed(f"es   {cat}", lambda: _resolve_node_candidates(cat_spec, g, es))
        compare(f"resolve_candidates({cat})", sorted(l_r), sorted(e_r))

    # ---- 3. associations: wildcard middles, category tail ------------------
    print("\n3. associations (category tail)")
    for hops in (1, 2, 3):
        path: list = [start] + [None, None] * (hops - 1) + [None, {"category": "biolink:Disease"}]
        (l_p, l_t) = timed(f"lmdb {hops}-hop", lambda: g.match_path(
            path, limit=2000, node_subclassing=True, db=lmdb))
        (e_p, e_t) = timed(f"es   {hops}-hop", lambda: g.match_path(
            path, limit=2000, node_subclassing=True, db=es))
        compare(f"associations({hops}-hop)", sorted(map(tuple, l_p)),
                sorted(map(tuple, e_p)))
        print(f"      lmdb {l_t:.3f}s vs es {e_t:.3f}s")

    # ---- 4. edge-metadata filter (dict EdgeSpec) --------------------------
    print("\n4. edge metadata filter (knowledge_level)")
    path = [start, {"knowledge_level": "knowledge_assertion"}, None]
    (l_p, l_t) = timed("lmdb 1-hop KL", lambda: g.match_path(path, limit=2000, db=lmdb))
    (e_p, e_t) = timed("es   1-hop KL", lambda: g.match_path(path, limit=2000, db=es))
    compare("edge_filter(knowledge_level)", sorted(map(tuple, l_p)),
            sorted(map(tuple, e_p)))
    print(f"      lmdb {l_t:.3f}s vs es {e_t:.3f}s")

    # ---- 5. pinned-both-ends connect (exercises reachability pruning) -----
    print("\n5. pinned both ends (reachability pruning)")
    probe = g.match_path(
        [start] + [None, None] * 2 + [None, {"category": "biolink:Disease"}],
        limit=50, node_subclassing=True, db=lmdb,
    )
    if probe:
        end = probe[-1][-1][2]
        path = [start] + [None, None] * 2 + [None, end]
        (l_p, l_t) = timed(f"lmdb 3-hop -> {end}", lambda: g.match_path(
            path, limit=2000, node_subclassing=True, db=lmdb))
        (e_p, e_t) = timed(f"es   3-hop -> {end}", lambda: g.match_path(
            path, limit=2000, node_subclassing=True, db=es))
        compare("connect(3-hop pinned)", sorted(map(tuple, l_p)),
                sorted(map(tuple, e_p)))
        print(f"      lmdb {l_t:.3f}s vs es {e_t:.3f}s")
    else:
        print("    (no 3-hop disease endpoint found; skipped)")

    # ---- 6. ES-only: full-text name resolution ---------------------------
    print("\n6. ES-only full-text resolution (LMDB has no equivalent)")
    import kg_query as kq

    g.set_db(es)
    for text, cat in (("insulin", None), ("diabetes", "biolink:Disease")):
        hits, _ = timed(f"resolve({text!r})",
                        lambda t=text, c=cat: kq.resolve(t, category=c, graph=g, top=3))
        for h in hits[:3]:
            print(f"        {h.get('id')}  {h.get('name')}")

    # ---- 7. truncation reporting still works on both ---------------------
    print("\n7. truncation signal (item 4)")
    path = [start] + [None, None] + [None, {"category": "biolink:Disease"}]
    for label, backend in (("lmdb", lmdb), ("es", es)):
        _, st = g.match_path(path, limit=5, node_subclassing=True, db=backend,
                             return_stats=True)
        print(f"    {label}: truncated={st.truncated} hops={st.truncated_hops} "
              f"frontiers={st.frontier_sizes}")

    print("\n" + "=" * 62)
    if failures:
        print(f"RESULT: {len(failures)} DISAGREEMENT(S): {failures}")
    else:
        print("RESULT: LMDB and ES agree on every compared query")
    print("=" * 62)

    lmdb.close()
    es.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
