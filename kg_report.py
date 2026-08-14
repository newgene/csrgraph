"""Generate a self-contained HTML report for a csrgraph association query.

Runs the same query as ``kg_query.py assoc`` (an entity -> any node of a target
category, with node subclassing on) and renders an interactive network graph
(vis-network via CDN) plus grouped data tables into a single HTML file.

Example::

    .venv/bin/python kg_report.py --from FREM1 --from-category biolink:Gene \
        --to-category biolink:DiseaseOrPhenotypicFeature --max-hops 2 \
        --two-hop-limit 60 --out frem1_disease_report.html
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
from pathlib import Path
from typing import Optional

import kg_query as kq


# --------------------------------------------------------------------------- #
# Node classification -> visual group  (colour + legend bucket)
# --------------------------------------------------------------------------- #
GROUPS = {
    "source":       {"label": "Query entity",            "color": "#f59e0b"},
    "disease":      {"label": "Disease",                 "color": "#ef4444"},
    "phenotype":    {"label": "Phenotypic feature",      "color": "#14b8a6"},
    "trait":        {"label": "Measurement / trait",     "color": "#8b5cf6"},
    "clinical":     {"label": "Clinical concept",        "color": "#64748b"},
    "anatomy":      {"label": "Anatomy (intermediate)",  "color": "#22c55e"},
    "gene":         {"label": "Gene/protein (intermediate)", "color": "#3b82f6"},
    "chemical":     {"label": "Chemical (intermediate)", "color": "#06b6d4"},
    "other":        {"label": "Other",                   "color": "#94a3b8"},
}


def classify(curie: str, categories: list[str]) -> str:
    cats = set(categories or [])
    pref = curie.split(":", 1)[0]
    if "biolink:Disease" in cats:
        return "disease"
    if "biolink:PhenotypicFeature" in cats:
        return "phenotype"
    if {"biolink:Gene", "biolink:Protein", "biolink:GeneOrGeneProduct"} & cats:
        return "gene"
    if {"biolink:AnatomicalEntity", "biolink:GrossAnatomicalStructure", "biolink:Cell"} & cats:
        return "anatomy"
    if {"biolink:ChemicalEntity", "biolink:SmallMolecule", "biolink:Drug"} & cats:
        return "chemical"
    # prefix fallbacks
    if pref in ("MONDO", "DOID", "OMIM", "Orphanet"):
        return "disease"
    if pref == "HP":
        return "phenotype"
    if pref == "EFO":
        return "trait"
    if pref == "UBERON":
        return "anatomy"
    if pref in ("UMLS", "NCIT", "MESH"):
        return "clinical"
    return "other"


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
def collect(
    src_text: str,
    from_category: Optional[str],
    to_category: str,
    max_hops: int,
    two_hop_limit: int,
):
    g = kq.get_graph()
    src = src_text if (":" in src_text and " " not in src_text) else kq.resolve_one(
        src_text, category=from_category, graph=g
    )

    direct = kq.associations(g, src, to_category, max_hops=1, limit=5000)
    two = (
        kq.associations(g, src, to_category, max_hops=2, limit=two_hop_limit)
        if max_hops >= 2
        else []
    )

    # gather all curies for one batched name+category lookup
    curies = {src}
    for p in direct:
        for s, _pred, o in p:
            curies.update((s, o))
    for p in two:
        for s, _pred, o in p:
            curies.update((s, o))

    es = g.db._es
    idx = getattr(g.db, "_nodes_idx", "translator_kg_2026-07-19_nodes")
    resp = es.mget(index=idx, ids=list(curies), _source=["name", "category"])
    meta: dict[str, dict] = {}
    for doc in resp["docs"]:
        s = doc.get("_source") or {}
        cats = s.get("category", [])
        meta[doc["_id"]] = {
            "name": s.get("name") or doc["_id"],
            "category": [c if c.startswith("biolink:") else f"biolink:{c}" for c in cats],
        }

    def m(c):
        return meta.get(c, {"name": c, "category": []})

    # ---- build vis-network nodes / edges -------------------------------- #
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edges: set[tuple] = set()

    def add_node(curie, *, group):
        if curie not in nodes:
            info = m(curie)
            nodes[curie] = {
                "id": curie,
                "label": info["name"],
                "group": group,
                "title": f"{info['name']} ({curie})\n{', '.join(info['category']) or '—'}",
            }

    add_node(src, group="source")

    def add_edge(s, pred, o, hop):
        key = (s, pred, o)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({
            "from": s, "to": o,
            "label": pred.replace("biolink:", ""),
            "hop": hop,
        })

    direct_rows = []
    for p in direct:
        s, pred, o = p[0]
        grp = classify(o, m(o)["category"])
        add_node(o, group=grp)
        add_edge(s, pred, o, 1)
        direct_rows.append({
            "predicate": pred, "id": o, "name": m(o)["name"], "group": grp,
        })

    direct_targets = {r["id"] for r in direct_rows}
    two_rows = []
    for p in two:
        (s1, p1, mid), (s2, p2, o) = p[0], p[1]
        if o in direct_targets:
            continue
        add_node(mid, group=classify(mid, m(mid)["category"]))
        add_node(o, group=classify(o, m(o)["category"]))
        add_edge(s1, p1, mid, 2)
        add_edge(s2, p2, o, 2)
        two_rows.append({
            "mid_id": mid, "mid_name": m(mid)["name"], "p1": p1,
            "id": o, "name": m(o)["name"], "p2": p2,
            "group": classify(o, m(o)["category"]),
        })

    return {
        "src": src,
        "src_name": m(src)["name"],
        "src_categories": m(src)["category"],
        "to_category": to_category,
        "max_hops": max_hops,
        "nodes": list(nodes.values()),
        "edges": edges,
        "direct_rows": direct_rows,
        "two_rows": two_rows,
    }


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
def _esc(x) -> str:
    return html.escape(str(x))


def render_html(data: dict) -> str:
    src, src_name = data["src"], data["src_name"]
    direct_rows, two_rows = data["direct_rows"], data["two_rows"]

    # counts by group (direct endpoints only)
    by_group: dict[str, int] = {}
    for r in direct_rows:
        by_group[r["group"]] = by_group.get(r["group"], 0) + 1

    # group direct rows by group for the table
    direct_by_group: dict[str, list] = {}
    for r in sorted(direct_rows, key=lambda r: (r["group"], r["name"].lower())):
        direct_by_group.setdefault(r["group"], []).append(r)

    legend_html = "".join(
        f'<span class="chip"><span class="dot" style="background:{g["color"]}"></span>{_esc(g["label"])}</span>'
        for k, g in GROUPS.items()
    )

    cards = "".join(
        f'<div class="card"><div class="num">{c}</div><div class="lbl">{_esc(GROUPS[k]["label"])}</div></div>'
        for k, c in sorted(by_group.items(), key=lambda kv: -kv[1])
    )

    # direct tables
    direct_tables = []
    for grp, rows in direct_by_group.items():
        body = "".join(
            f"<tr><td>{_esc(r['name'])}</td><td class='mono'>{_esc(r['id'])}</td>"
            f"<td class='mono'>{_esc(r['predicate'].replace('biolink:',''))}</td></tr>"
            for r in rows
        )
        direct_tables.append(
            f'<h4><span class="dot" style="background:{GROUPS[grp]["color"]}"></span>'
            f'{_esc(GROUPS[grp]["label"])} ({len(rows)})</h4>'
            f'<table><thead><tr><th>Name</th><th>CURIE</th><th>Predicate</th></tr></thead>'
            f'<tbody>{body}</tbody></table>'
        )
    direct_tables_html = "".join(direct_tables) or "<p>No direct associations.</p>"

    two_body = "".join(
        f"<tr><td>{_esc(r['p1'].replace('biolink:',''))}</td>"
        f"<td>{_esc(r['mid_name'])} <span class='mono dim'>({_esc(r['mid_id'])})</span></td>"
        f"<td>{_esc(r['p2'].replace('biolink:',''))}</td>"
        f"<td>{_esc(r['name'])} <span class='mono dim'>({_esc(r['id'])})</span></td></tr>"
        for r in two_rows
    )
    two_section = (
        f'<h3>2-hop paths <span class="dim">(sample of {len(two_rows)} additional endpoints '
        f'via an intermediate)</span></h3>'
        f'<table><thead><tr><th>predicate</th><th>intermediate</th><th>predicate</th>'
        f'<th>endpoint</th></tr></thead><tbody>{two_body}</tbody></table>'
        if two_rows else ""
    )

    groups_js = json.dumps({k: v["color"] for k, v in GROUPS.items()})
    nodes_js = json.dumps(data["nodes"])
    edges_js = json.dumps(data["edges"])
    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(src_name)} &rarr; {_esc(data['to_category'])} &mdash; csrgraph report</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root {{ --bg:#0f172a; --panel:#1e293b; --ink:#e2e8f0; --muted:#94a3b8; --line:#334155; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--ink); }}
  header {{ padding:20px 28px; border-bottom:1px solid var(--line); }}
  h1 {{ margin:0 0 4px; font-size:20px; }}
  h1 .gene {{ color:#f59e0b; }}
  .sub {{ color:var(--muted); font-size:13px; }}
  .wrap {{ padding:20px 28px; max-width:1200px; margin:0 auto; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 16px; min-width:120px; }}
  .card .num {{ font-size:26px; font-weight:700; }}
  .card .lbl {{ color:var(--muted); font-size:12px; }}
  #net {{ height:560px; background:#0b1222; border:1px solid var(--line); border-radius:12px; }}
  .legend {{ margin:12px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
  .chip {{ display:inline-flex; align-items:center; gap:6px; background:var(--panel); border:1px solid var(--line);
           border-radius:20px; padding:4px 10px; font-size:12px; cursor:pointer; user-select:none; }}
  .chip.off {{ opacity:.4; }}
  .dot {{ width:11px; height:11px; border-radius:50%; display:inline-block; }}
  table {{ width:100%; border-collapse:collapse; margin:6px 0 18px; background:var(--panel);
           border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
  th {{ background:#172033; color:var(--muted); font-weight:600; font-size:12px; }}
  tr:last-child td {{ border-bottom:none; }}
  h3 {{ margin:26px 0 8px; }} h4 {{ margin:18px 0 6px; display:flex; align-items:center; gap:8px; }}
  .mono {{ font-family:ui-monospace,Menlo,monospace; font-size:12px; }}
  .dim {{ color:var(--muted); }}
  .note {{ color:var(--muted); font-size:12px; margin-top:6px; }}
</style>
</head>
<body>
<header>
  <h1><span class="gene">{_esc(src_name)}</span> ({_esc(src)}) &rarr; {_esc(data['to_category'])}</h1>
  <div class="sub">csrgraph &middot; graph <b>translator_kg_2026-07-19</b> &middot; node-subclassing on &middot;
    {len(direct_rows)} direct endpoints, {len(two_rows)} additional via 2-hop &middot; generated {generated}</div>
</header>
<div class="wrap">
  <div class="cards">{cards}</div>

  <div class="legend" id="legend"></div>
  <div id="net"></div>
  <div class="note">Drag nodes to explore; scroll to zoom. Click a legend chip to toggle that node type.
    Dashed grey edges are 2-hop links. Node typing uses biolink category, falling back to CURIE prefix.</div>

  <h3>Direct associations ({len(direct_rows)})</h3>
  {direct_tables_html}

  {two_section}
</div>

<script>
const GROUP_COLORS = {groups_js};
const ALL_NODES = {nodes_js};
const ALL_EDGES = {edges_js};

const container = document.getElementById('net');
const nodes = new vis.DataSet();
const edges = new vis.DataSet();
const network = new vis.Network(container, {{nodes, edges}}, {{
  nodes: {{ shape:'dot', size:14, font:{{color:'#e2e8f0', size:13}}, borderWidth:0 }},
  edges: {{ color:{{color:'#475569', highlight:'#cbd5e1'}}, font:{{color:'#94a3b8', size:10, strokeWidth:0}},
            arrows:'to', smooth:{{type:'dynamic'}} }},
  physics: {{ stabilization:true, barnesHut:{{ gravitationalConstant:-12000, springLength:130 }} }},
  interaction: {{ hover:true, tooltipDelay:120 }},
}});

const enabled = new Set(Object.keys(GROUP_COLORS));

function styleNode(n) {{
  const color = GROUP_COLORS[n.group] || GROUP_COLORS.other;
  const isSrc = n.group === 'source';
  return Object.assign({{}}, n, {{
    color: {{ background:color, border:color }},
    size: isSrc ? 26 : 14,
    shape: isSrc ? 'star' : 'dot',
    font: {{ color:'#e2e8f0', size: isSrc ? 16 : 12 }},
  }});
}}

function rebuild() {{
  const keep = ALL_NODES.filter(n => enabled.has(n.group));
  const keepIds = new Set(keep.map(n => n.id));
  nodes.clear(); edges.clear();
  nodes.add(keep.map(styleNode));
  edges.add(ALL_EDGES
    .filter(e => keepIds.has(e.from) && keepIds.has(e.to))
    .map(e => Object.assign({{}}, e, e.hop === 2 ? {{dashes:true}} : {{}})));
}}

// legend with toggles
const legend = document.getElementById('legend');
for (const [key, color] of Object.entries(GROUP_COLORS)) {{
  const present = ALL_NODES.some(n => n.group === key);
  if (!present) continue;
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.innerHTML = `<span class="dot" style="background:${{color}}"></span>` + key;
  chip.onclick = () => {{
    if (enabled.has(key)) {{ enabled.delete(key); chip.classList.add('off'); }}
    else {{ enabled.add(key); chip.classList.remove('off'); }}
    rebuild();
  }};
  legend.appendChild(chip);
}}

rebuild();
</script>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate an HTML report for a csrgraph association query")
    ap.add_argument("--from", dest="src", required=True, help="entity name or CURIE")
    ap.add_argument("--from-category", default=None)
    ap.add_argument("--to-category", default="biolink:DiseaseOrPhenotypicFeature")
    ap.add_argument("--max-hops", type=int, default=2)
    ap.add_argument("--two-hop-limit", type=int, default=60)
    ap.add_argument("--out", default=None, help="output HTML path")
    args = ap.parse_args(argv)

    data = collect(args.src, args.from_category, args.to_category, args.max_hops, args.two_hop_limit)
    out = args.out or f"{data['src_name'].replace(' ', '_')}_report.html"
    Path(out).write_text(render_html(data), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"  {len(data['direct_rows'])} direct endpoints, {len(data['two_rows'])} additional 2-hop endpoints, "
          f"{len(data['nodes'])} nodes / {len(data['edges'])} edges in the graph")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
