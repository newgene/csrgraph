"""Audit a CSRGraph for directionality / data-quality issues per predicate.

For every predicate this inspects the per-predicate CSR adjacency matrix and
reports:

* **self-loops** — edges ``A --P--> A`` (usually meaningless for most
  predicates; legitimate only for a few, e.g. gene autoregulation).
* **mutual pairs** — distinct nodes with both ``A --P--> B`` and
  ``B --P--> A`` stored.  For predicates that *should* be antisymmetric or
  acyclic (``subclass_of``, ``located_in``, ``has_part`` …) these indicate
  likely source-data errors; for genuinely symmetric predicates they are
  expected.
* **materialization** — for symmetric predicates, whether the reverse edge is
  actually stored (``A<->B``) or whether the query engine must synthesise it.

The graph loader stores edges exactly as they appear in the KGX archive
(no auto-reversal), so anything flagged here reflects the *source* data.

Usage::

    python predicate_audit.py \
        --graph ~/tmp/csrgraph_data/translator_kg_2026-07-19.csrgraph.pkl.zst \
        --lmdb  ~/tmp/csrgraph_data/translator_kg_2026-07-19.metadata.lmdb \
        --out   translator_kg_2026-07-19_predicate_audit.html
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
from pathlib import Path

from csrgraph_kgx import CSRGraph, _add_biolink
from trapi import SYMMETRIC_PREDICATES

# Predicates that are inherently antisymmetric or strictly hierarchical:
# a mutual A<->B pair or a self-loop is almost certainly a data error.
# Predicates that encode the class hierarchy: mutual pairs are cycles and
# self-loops are reflexive-subclass errors (both high severity).
HIERARCHY = {"biolink:subclass_of", "rdfs:subClassOf", "biolink:superclass_of"}

ANTISYMMETRIC = {
    "biolink:subclass_of",
    "rdfs:subClassOf",
    "biolink:superclass_of",
    "biolink:part_of",
    "biolink:has_part",
    "biolink:located_in",
    "biolink:has_input",
    "biolink:has_output",
    "biolink:has_metabolite",
    "biolink:has_substrate",
    "biolink:causes",
    "biolink:caused_by",
    "biolink:predisposes_to_condition",
    "biolink:acts_upstream_of",
    "biolink:acts_upstream_of_or_within",
    "biolink:has_phenotype",
    "biolink:capable_of",
    "biolink:enables",
    "biolink:contributes_to",
}

# Non-symmetric predicates where mutual/self edges are biologically plausible.
PLAUSIBLE_BIDIRECTIONAL = {
    "biolink:affects",
    "biolink:regulates",
    "biolink:interacts_with",
}


def _stats(g: CSRGraph):
    import numpy as np  # noqa: F401  (scipy pulls it in; explicit for clarity)

    rows = []
    for r in g.relations:
        p = _add_biolink(r)
        M = g.csr_by_relation[r]
        Mb = M.copy()
        Mb.data[:] = 1
        Mb.eliminate_zeros()
        directed = Mb.nnz
        recip = Mb.multiply(Mb.T)
        self_loops = int((recip.diagonal() > 0).sum())
        mutual_pairs = (recip.nnz - self_loops) // 2
        rows.append(
            {
                "predicate": p,
                "rel": r,
                "directed": directed,
                "self_loops": self_loops,
                "mutual_pairs": mutual_pairs,
                "symmetric": p in SYMMETRIC_PREDICATES,
                "recip_directed": recip.nnz,  # incl. self-loops, both directions
            }
        )
    return rows


def _examples(g: CSRGraph, rel: str, want_self: bool, limit: int = 10):
    """Return up to *limit* example (u, v) pairs; self-loops if want_self."""
    M = g.csr_by_relation[rel]
    Mb = M.copy()
    Mb.data[:] = 1
    Mb.eliminate_zeros()
    recip = Mb.multiply(Mb.T).tocoo()
    out = []
    seen = set()
    for i, j in zip(recip.row.tolist(), recip.col.tolist()):
        if want_self and i == j and i not in seen:
            seen.add(i)
            out.append((g.nodes[i], g.nodes[i]))
        elif not want_self and i < j and (i, j) not in seen:
            seen.add((i, j))
            out.append((g.nodes[i], g.nodes[j]))
        if len(out) >= limit:
            break
    return out


def _name(g: CSRGraph, curie: str) -> str:
    try:
        if g.db is not None:
            meta = g.get_node(curie)
            nm = meta.get("name") if meta else None
            return nm or ""
    except Exception:
        pass
    return ""


def _classify(row):
    """Return (severity, category, note) for a flagged predicate, or None."""
    p = row["predicate"]
    sl, mp = row["self_loops"], row["mutual_pairs"]

    if row["symmetric"]:
        # Materialization check for symmetric predicates.
        recip_frac = row["recip_directed"] / row["directed"] if row["directed"] else 0
        if recip_frac >= 0.999:
            return None  # fully materialized — nothing to check
        return (
            "info",
            "Symmetric, not fully materialized",
            f"Only {recip_frac:.0%} of edges have their reverse stored; the query "
            "engine synthesises the rest at query time (expected, but worth knowing "
            "if you rely on raw CSR symmetry).",
        )

    if sl == 0 and mp == 0:
        return None  # clean directed predicate

    if p in HIERARCHY:
        return (
            "high",
            "Ontology cycle / reflexive subclass",
            "Subclass edges should form an acyclic, irreflexive hierarchy. Mutual "
            "pairs (A⊑B ∧ B⊑A) are cycles and self-loops (A⊑A) are reflexive errors; "
            "both corrupt subclass expansion. Almost certainly source errors.",
        )
    if p in ANTISYMMETRIC:
        return (
            "high",
            "Antisymmetry / self-reference violation",
            "This predicate should be antisymmetric and irreflexive; mutual pairs "
            "or self-loops are very likely source-data errors.",
        )
    if p in PLAUSIBLE_BIDIRECTIONAL:
        sev = "low" if mp else "medium"
        return (
            sev,
            "Mostly plausible",
            "Mutual edges can be legitimate (e.g. feedback loops, mutual effects, "
            "gene autoregulation). Self-loops are more likely noise — worth spot-checking.",
        )
    sev = "medium" if mp else "low"
    return (
        sev,
        "Unexpected bidirectionality",
        "Non-symmetric predicate with reverse and/or self edges stored — review "
        "whether the source intends a directed relationship.",
    )


_SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
_SEV_LABEL = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "info": "Info",
}


def _render_html(graph_path: str, rows, flagged) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    n_total = len(rows)
    n_sym = sum(1 for r in rows if r["symmetric"])

    def esc(x):
        return html.escape(str(x))

    # Build flagged rows HTML
    cards = []
    for row, (sev, cat, note), examples in flagged:
        p = row["predicate"]
        ex_html = ""
        if examples:
            items = []
            for kind, pairs in examples:
                for u, v in pairs:
                    un, vn = row["_name"](u), row["_name"](v)
                    ulabel = f"{esc(u)}" + (f" <span class='nm'>({esc(un)})</span>" if un else "")
                    vlabel = f"{esc(v)}" + (f" <span class='nm'>({esc(vn)})</span>" if vn else "")
                    arrow = "&#8594;&#8592;" if kind == "mutual" else "&#8635;"
                    label = "mutual" if kind == "mutual" else "self-loop"
                    items.append(
                        f"<li><span class='tag'>{label}</span> {ulabel} "
                        f"<span class='arrow'>{arrow}</span> {vlabel}</li>"
                    )
            ex_html = "<ul class='examples'>" + "".join(items) + "</ul>"
        cards.append(
            f"""
        <div class="card sev-{sev}">
          <div class="card-head">
            <span class="pred">{esc(p)}</span>
            <span class="badge badge-{sev}">{_SEV_LABEL[sev]}</span>
            <span class="cat">{esc(cat)}</span>
          </div>
          <div class="metrics">
            <span><b>{row['directed']:,}</b> directed edges</span>
            <span><b>{row['mutual_pairs']:,}</b> mutual A&#8596;B pairs</span>
            <span><b>{row['self_loops']:,}</b> self-loops</span>
          </div>
          <p class="note">{esc(note)}</p>
          {ex_html}
        </div>"""
        )

    # Full table of all predicates
    trows = []
    for row in sorted(rows, key=lambda r: -r["directed"]):
        kind = "symmetric" if row["symmetric"] else "directed"
        flag = "&#9888;" if (row["self_loops"] or row["mutual_pairs"]) and not row["symmetric"] else ""
        trows.append(
            f"<tr><td class='mono'>{esc(row['predicate'])}</td>"
            f"<td>{kind}</td>"
            f"<td class='num'>{row['directed']:,}</td>"
            f"<td class='num'>{row['mutual_pairs']:,}</td>"
            f"<td class='num'>{row['self_loops']:,}</td>"
            f"<td class='num'>{flag}</td></tr>"
        )

    counts = {s: 0 for s in _SEV_ORDER}
    for _, (sev, _, _), _ in flagged:
        counts[sev] += 1

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Predicate Audit — {esc(Path(graph_path).name)}</title>
<style>
  :root {{
    --high:#c0392b; --medium:#e67e22; --low:#2980b9; --info:#7f8c8d;
    --bg:#f7f8fa; --card:#fff; --ink:#222; --muted:#666; --line:#e3e6eb;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         margin:0; background:var(--bg); color:var(--ink); }}
  .wrap {{ max-width:980px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:24px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); margin:0 0 24px; font-size:13px; }}
  .summary {{ display:flex; gap:12px; flex-wrap:wrap; margin:0 0 28px; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; min-width:120px; }}
  .stat b {{ display:block; font-size:22px; }}
  .stat span {{ color:var(--muted); font-size:12px; }}
  .callout {{ background:#fff8e1; border:1px solid #ffe08a; border-radius:10px;
              padding:12px 16px; margin:0 0 28px; font-size:13.5px; color:#5b4a00; }}
  h2 {{ font-size:18px; margin:32px 0 12px; border-bottom:2px solid var(--line); padding-bottom:6px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-left-width:5px;
           border-radius:10px; padding:16px 18px; margin:0 0 14px; }}
  .card.sev-high {{ border-left-color:var(--high); }}
  .card.sev-medium {{ border-left-color:var(--medium); }}
  .card.sev-low {{ border-left-color:var(--low); }}
  .card.sev-info {{ border-left-color:var(--info); }}
  .card-head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .pred {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-weight:600; font-size:15px; }}
  .cat {{ color:var(--muted); font-size:13px; }}
  .badge {{ font-size:11px; font-weight:700; color:#fff; padding:2px 8px; border-radius:20px; letter-spacing:.3px; }}
  .badge-high {{ background:var(--high); }}
  .badge-medium {{ background:var(--medium); }}
  .badge-low {{ background:var(--low); }}
  .badge-info {{ background:var(--info); }}
  .metrics {{ display:flex; gap:18px; flex-wrap:wrap; margin:10px 0 6px; font-size:13px; color:var(--muted); }}
  .metrics b {{ color:var(--ink); }}
  .note {{ margin:6px 0 10px; font-size:13.5px; }}
  ul.examples {{ list-style:none; margin:0; padding:0; }}
  ul.examples li {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px;
                    padding:6px 10px; background:var(--bg); border-radius:7px; margin:4px 0; }}
  .tag {{ display:inline-block; font-size:10px; text-transform:uppercase; letter-spacing:.4px;
          background:#dfe4ea; color:#555; padding:1px 6px; border-radius:4px; margin-right:8px; }}
  .arrow {{ color:var(--high); font-weight:700; padding:0 4px; }}
  .nm {{ color:var(--muted); font-family:inherit; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
           border-radius:10px; overflow:hidden; font-size:13px; }}
  th,td {{ padding:7px 12px; border-bottom:1px solid var(--line); text-align:left; }}
  th {{ background:#eef1f5; font-size:12px; text-transform:uppercase; letter-spacing:.3px; color:#555; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  tr:last-child td {{ border-bottom:none; }}
  footer {{ margin-top:36px; color:var(--muted); font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Predicate directionality audit</h1>
  <p class="sub">Graph: <code>{esc(graph_path)}</code> &middot; generated {now}</p>

  <div class="summary">
    <div class="stat"><b>{n_total}</b><span>predicates</span></div>
    <div class="stat"><b>{n_sym}</b><span>symmetric</span></div>
    <div class="stat"><b style="color:var(--high)">{counts['high']}</b><span>high severity</span></div>
    <div class="stat"><b style="color:var(--medium)">{counts['medium']}</b><span>medium</span></div>
    <div class="stat"><b style="color:var(--low)">{counts['low']}</b><span>low</span></div>
  </div>

  <div class="callout">
    The graph loader stores edges <b>exactly</b> as they appear in the KGX archive
    (no automatic reversal). Everything flagged here therefore reflects the
    <b>source data</b>, and any fix belongs in KG preprocessing — not in csrgraph.
    The query engine treats all non-symmetric predicates as strictly directed.
  </div>

  <h2>Predicates worth checking ({len(flagged)})</h2>
  {''.join(cards) if cards else '<p>No issues flagged.</p>'}

  <h2>All predicates</h2>
  <table>
    <thead><tr><th>Predicate</th><th>Kind</th><th>Directed edges</th>
    <th>Mutual pairs</th><th>Self-loops</th><th></th></tr></thead>
    <tbody>{''.join(trows)}</tbody>
  </table>

  <footer>Generated by <code>predicate_audit.py</code>. Mutual pair = distinct
  nodes with edges stored in both directions for the same predicate;
  self-loop = an edge from a node to itself.</footer>
</div>
</body>
</html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph", required=True, help="Path to <name>.csrgraph.pkl.zst")
    ap.add_argument("--lmdb", default=None, help="Optional LMDB metadata dir for node names")
    ap.add_argument("--out", default="predicate_audit.html", help="Output HTML path")
    args = ap.parse_args()

    g = CSRGraph.load(args.graph)
    if args.lmdb and Path(args.lmdb).exists():
        try:
            from metadata_db import LMDBMetadataBackend
            g.set_db(LMDBMetadataBackend(args.lmdb))
            print(f"Attached LMDB for name resolution: {args.lmdb}")
        except Exception as exc:  # pragma: no cover
            print(f"Could not attach LMDB ({exc}); examples will omit names")

    rows = _stats(g)
    for row in rows:
        row["_name"] = lambda c, _g=g: _name(_g, c)

    flagged = []
    for row in rows:
        verdict = _classify(row)
        if verdict is None:
            continue
        sev, cat, note = verdict
        examples = []
        if not row["symmetric"]:
            # Show up to 10 examples per predicate, mutual pairs first
            # (more informative), then self-loops to fill any remaining slots.
            budget = 10
            if row["mutual_pairs"]:
                mut = _examples(g, row["rel"], want_self=False, limit=budget)
                examples.append(("mutual", mut))
                budget -= len(mut)
            if row["self_loops"] and budget > 0:
                examples.append(
                    ("self", _examples(g, row["rel"], want_self=True, limit=budget))
                )
        flagged.append((row, verdict, examples))

    flagged.sort(key=lambda f: (_SEV_ORDER[f[1][0]], -f[0]["mutual_pairs"] - f[0]["self_loops"]))

    out = Path(args.out)
    out.write_text(_render_html(args.graph, rows, flagged), encoding="utf-8")
    print(f"Wrote {out}  ({len(flagged)} predicates flagged)")


if __name__ == "__main__":
    main()
