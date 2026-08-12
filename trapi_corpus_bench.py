"""Run the HelmsDeep TRAPI corpus against csrgraph and gandalf.

Corpus: https://github.com/TranslatorSRI/HelmsDeep (helmsdeep/trapi_corpus.py)

Both engines are KPs answering ``lookup``-mode queries, so the directly
applicable segment is RETRIEVER_CORPUS. The inferred (Shepherd/ARS) and
Pathfinder segments target ARA-level creative reasoning that neither engine
implements; they are still submitted, to record *how* each one handles a shape it
does not support — graceful empty result, explicit rejection, or crash.

Accuracy is compared on the **answer set**: the CURIEs bound to each query node.
Result *counts* are not comparable directly, because gandalf preserves duplicate
triples and emits one result per distinct edge combination while csrgraph
collapses them (see docs/local-data-2026-07-19.md).

Each engine runs in its own venv, writing JSON:

    .venv/bin/python trapi_corpus_bench.py --engine csrgraph --out /tmp/tc_csr.json
    ~/tmp/gandalf_latest/.venv/bin/python trapi_corpus_bench.py \
        --engine gandalf --out /tmp/tc_gan.json
    .venv/bin/python trapi_corpus_bench.py --compare
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.expanduser("~/tmp"))  # trapi_corpus.py lives here

DATA = os.path.expanduser("~/tmp/csrgraph_data")
GANDALF_GRAPH = os.path.expanduser("~/tmp/gandalf_data/graph_2026-07-19_mmap")
STEM = "translator_kg_2026-07-19"
LIMIT = 200
REPS = 3


def build_cases():
    """(qtype, segment, query envelope) for every corpus builder, seeded."""
    import random

    import trapi_corpus as tc

    random.seed(0)  # the inferred builders sample entities at random
    cases = []
    for segment in ("retriever", "shepherd", "pathfinder"):
        for qtype, builder, _weight in tc.corpus_for(segment):
            try:
                cases.append((qtype, segment, builder()))
            except Exception as exc:  # a builder needing data we lack
                cases.append((qtype, segment, {"__builder_error__": repr(exc)}))
    return cases


def answer_sets(results: list[dict]) -> dict[str, list[str]]:
    """CURIEs bound to each query node, deduplicated and sorted."""
    out: dict[str, set] = {}
    for r in results or []:
        for qnode, bindings in (r.get("node_bindings") or {}).items():
            for b in bindings:
                if b.get("id"):
                    out.setdefault(qnode, set()).add(b["id"])
    return {k: sorted(v) for k, v in sorted(out.items())}


def run_csrgraph(cases):
    from csrgraph_kgx import CSRGraph
    from metadata_db import LMDBMetadataBackend
    import trapi

    db = LMDBMetadataBackend(f"{DATA}/{STEM}.metadata.lmdb")
    g = CSRGraph.load(f"{DATA}/{STEM}.csrgraph.pkl.zst")
    g.set_db(db)

    def one(env):
        qg = env["message"]["query_graph"]
        msg = trapi.query(g, qg, limit=LIMIT)
        return msg.get("results", []), msg

    out = execute(cases, one)
    db.close()
    return out


def run_gandalf(cases):
    from gandalf.graph import CSRGraph
    from gandalf.search import lookup

    g = CSRGraph.load_mmap(GANDALF_GRAPH)

    def one(env):
        # gandalf takes the whole TRAPI envelope and returns a full response.
        resp = lookup(g, {"message": {"query_graph": env["message"]["query_graph"]}})
        msg = resp.get("message", resp)
        return msg.get("results", []), msg

    return execute(cases, one)


def execute(cases, one):
    """Time each case, capturing errors rather than aborting the run."""
    recs = []
    for qtype, segment, env in cases:
        rec = {"qtype": qtype, "segment": segment}
        if "__builder_error__" in env:
            rec.update(status="builder_error", error=env["__builder_error__"])
            recs.append(rec)
            print(f"  {qtype:<30} builder_error")
            continue
        try:
            one(env)  # warm
            lat = []
            for _ in range(REPS):
                t0 = time.perf_counter()
                results, msg = one(env)
                lat.append(time.perf_counter() - t0)
            lat.sort()
            kg = msg.get("knowledge_graph") or {}
            rec.update(
                status="ok",
                secs=round(lat[len(lat) // 2], 4),
                n_results=len(results),
                kg_nodes=len(kg.get("nodes") or {}),
                kg_edges=len(kg.get("edges") or {}),
                answers=answer_sets(results),
            )
            print(f"  {qtype:<30} {rec['secs']*1000:9.1f} ms  "
                  f"results={rec['n_results']:<6} kg_edges={rec['kg_edges']}")
        except Exception as exc:
            rec.update(status="error", error=f"{type(exc).__name__}: {exc}",
                       trace=traceback.format_exc()[-600:])
            print(f"  {qtype:<30} ERROR {type(exc).__name__}: {str(exc)[:70]}")
        recs.append(rec)
    return recs


def compare(csr_path, gan_path):
    csr = {r["qtype"]: r for r in json.load(open(csr_path))}
    gan = {r["qtype"]: r for r in json.load(open(gan_path))}
    print(f"{'qtype':<30}{'segment':<12}{'csrgraph':>22}{'gandalf':>22}  accuracy")
    print("-" * 104)
    for qtype in csr:
        c, g = csr[qtype], gan.get(qtype, {})

        def cell(r):
            if r.get("status") == "ok":
                return f"{r['secs']*1000:8.1f}ms r={r['n_results']:<5}"
            return f"{r.get('status','absent'):>18}"

        verdict = "-"
        if c.get("status") == "ok" and g.get("status") == "ok":
            ca, ga = c.get("answers", {}), g.get("answers", {})
            if not ca and not ga:
                verdict = "both empty"
            else:
                keys = sorted(set(ca) | set(ga))
                same = all(set(ca.get(k, [])) == set(ga.get(k, [])) for k in keys)
                if same:
                    verdict = "IDENTICAL"
                else:
                    parts = []
                    for k in keys:
                        a, b = set(ca.get(k, [])), set(ga.get(k, []))
                        if a != b:
                            parts.append(f"{k}: csr={len(a)} gan={len(b)} shared={len(a & b)}")
                    verdict = "DIFFER  " + "; ".join(parts)
        print(f"{qtype:<30}{c.get('segment',''):<12}{cell(c):>22}{cell(g):>22}  {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["csrgraph", "gandalf"])
    ap.add_argument("--out")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--csr", default="/tmp/tc_csr.json")
    ap.add_argument("--gan", default="/tmp/tc_gan.json")
    a = ap.parse_args()
    logging.disable(logging.WARNING)

    if a.compare:
        compare(a.csr, a.gan)
    else:
        cases = build_cases()
        print(f"{len(cases)} corpus cases, engine={a.engine}")
        recs = (run_csrgraph if a.engine == "csrgraph" else run_gandalf)(cases)
        json.dump(recs, open(a.out, "w"), indent=1)
        print(f"wrote {a.out}")
