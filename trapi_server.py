"""Lightweight TRAPI API server for internal testing.

Loads the Translator KG graph and hybrid metadata backend (LMDB + optional
Elasticsearch) at startup, then exposes TRAPI query via a single POST endpoint.

Usage::

    cd csrgraph
    python trapi_server.py                          # defaults: translator_kg_2026-07-19, port 8000
    python trapi_server.py --port 9000              # custom port
    python trapi_server.py --data-dir ~/my/data     # custom data directory
    python trapi_server.py --graph dgidb             # use DGIdb instead
    python trapi_server.py --no-es                   # skip Elasticsearch, LMDB only
    python trapi_server.py --es-host http://es:9200  # custom ES host

Example query (curl)::

    curl -X POST http://localhost:8000/query -H 'Content-Type: application/json' -d '{
      "message": {
        "query_graph": {
          "nodes": {
            "n0": {"ids": ["CHEBI:6801"]},
            "n1": {"categories": ["biolink:Gene"]}
          },
          "edges": {
            "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]}
          }
        }
      }
    }'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# Ensure sibling modules are importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env from the csrgraph directory (if present).
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key, _val = _key.strip(), _val.strip()
                if _val and _key not in os.environ:  # don't override existing env
                    os.environ[_key] = _val

_DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "~/tmp/csrgraph_data")).expanduser()

# Bounds on how many paths a single query may enumerate.  These are an
# enumeration budget, not an answer count: constraints are applied after the cap,
# so a constrained query can return far fewer results than the limit (see
# trapi.query).  The default was 100, at which five of the eight answering
# HelmsDeep corpus queries silently under-answered; at 1000 worst-case corpus
# latency is around 200 ms.  The ceiling leaves room for callers that need
# completeness on qualifier queries, which converge around 2000.
_MAX_LIMIT = 10_000
_DEFAULT_LIMIT = 1000

from csrgraph_kgx import CSRGraph
from metadata_db import (
    es_host_from_env,
    STORE_FORMAT_VERSION,
    ElasticsearchMetadataBackend,
    HybridMetadataBackend,
    LMDBMetadataBackend,
)
from trapi import display_query_graph, query

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Graph and DB are loaded once at startup (populated in main / lifespan).
_graph: CSRGraph | None = None
_db: HybridMetadataBackend | None = None
#: Release manifest for the loaded DATA_DIR, or ``None`` when serving an
#: unversioned directory built by hand (see F1/F2 in
#: docs/production-release-plan.md).
_manifest: dict | None = None


class StoreFormatMismatch(RuntimeError):
    """The release's store format is not one this code can read."""


def _read_manifest(data_dir: Path) -> dict | None:
    """Load and validate ``manifest.json`` from *data_dir*.

    Raises :class:`StoreFormatMismatch` when the release was built by code with a
    different key layout.  **Failing to start is the desired behaviour**: a
    version-1 store read by version-2 code matches nothing, so the server would
    otherwise come up healthy and answer every qualifier-constrained query with
    an empty result — silently wrong, and far harder to notice than a refusal.
    """
    path = data_dir / "manifest.json"
    if not path.exists():
        print(f"No manifest.json in {data_dir}; serving an unversioned directory. "
              f"Store format is unchecked — build releases with make_release.py "
              f"to get a version gate.")
        return None
    manifest = json.loads(path.read_text())
    found = manifest.get("store_format_version")
    if found != STORE_FORMAT_VERSION:
        raise StoreFormatMismatch(
            f"{path} declares store_format_version {found!r}, but this code reads "
            f"{STORE_FORMAT_VERSION}. Refusing to serve: the key layouts differ, so "
            f"metadata lookups would return nothing rather than fail. Rebuild the "
            f"release with make_release.py, or deploy matching code."
        )
    print(f"Release {manifest.get('graph_name')} {manifest.get('version')} "
          f"(store format {found})")
    return manifest


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Load the graph on startup when not already loaded by ``main()``.

    This makes ``uvicorn trapi_server:app`` work without calling ``main()``:
    configuration is taken from environment variables (DATA_DIR, GRAPH_NAME,
    CSRGRAPH_ES_HOST, NO_ES).  When ``main()`` has already populated ``_graph``
    this is
    a no-op.
    """
    if _graph is None:
        graph_name = os.environ.get("GRAPH_NAME", "translator_kg_2026-07-19")
        es_host = es_host_from_env()
        no_es = os.environ.get("NO_ES", "").lower() in {"1", "true", "yes"}
        _load_graph(_DEFAULT_DATA_DIR, graph_name, es_host=es_host, no_es=no_es)
    yield


app = FastAPI(
    title="CSRGraph TRAPI Server",
    description="Internal test server for TRAPI queries against the Translator KG.",
    version="0.1.0",
    lifespan=_lifespan,
)


def _load_graph(
    data_dir: Path,
    graph_name: str,
    *,
    es_host: str = "http://localhost:9200",
    no_es: bool = False,
) -> None:
    global _graph, _db, _manifest

    graph_path = data_dir / f"{graph_name}.csrgraph.pkl.zst"
    lmdb_path = data_dir / f"{graph_name}.metadata.lmdb"

    if not graph_path.exists():
        sys.exit(f"Graph file not found: {graph_path}")
    if not lmdb_path.exists():
        sys.exit(f"LMDB metadata not found: {lmdb_path}")

    # Check the release before loading anything: a format mismatch should cost
    # nothing to detect.
    _manifest = _read_manifest(data_dir)

    # -- LMDB (always required) --
    # Read-only: a release directory is immutable, and opening read-write would
    # write lock.mdb into it — which also fails outright on a read-only mount.
    print(f"Loading LMDB metadata from {lmdb_path} ...")
    lmdb_be = LMDBMetadataBackend(str(lmdb_path), readonly=True)

    # -- Elasticsearch (optional, graceful fallback) --
    es_be = None
    if not no_es:
        try:
            es_be = ElasticsearchMetadataBackend(
                host=es_host, index_prefix=graph_name,
            )
            # F6: an ES index cannot live inside the release directory, so it
            # carries its own format stamp.  Checked here rather than in the
            # backend constructor because it costs two round trips.  A version
            # mismatch is fatal for the same reason the manifest one is — the
            # index answers, just with fewer results than exist — while an
            # unreachable cluster is not, since LMDB alone can serve.
            compat = es_be.check_compatibility()
            print(f"ES connected: {es_host} "
                  f"(server {compat['server']}, client {compat['client']})")
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"ES unavailable ({exc}); falling back to LMDB-only")

    # -- Hybrid backend --
    _db = HybridMetadataBackend(
        lmdb=lmdb_be,
        es=es_be,
        mode="auto" if es_be is not None else "lmdb",
    )
    mode_label = "LMDB + ES (auto)" if es_be is not None else "LMDB-only"
    print(f"Metadata backend: {mode_label}")

    # -- Graph --
    print(f"Loading graph from {graph_path} ...")
    t0 = time.time()
    _graph = CSRGraph.load(str(graph_path), db=_db)
    print(
        f"Loaded in {time.time() - t0:.3f}s  --  "
        f"{_graph.num_nodes:,} nodes, {_graph.edge_count:,} edges, "
        f"{len(_graph.relations)} predicates"
    )


# ---------------------------------------------------------------------------
# Test UI
# ---------------------------------------------------------------------------

_EXAMPLES = [
    {
        "label": "1-hop: Metformin -[affects]-> Gene",
        "query": {
            "nodes": {
                "n0": {"ids": ["CHEBI:6801"], "categories": ["biolink:SmallMolecule"]},
                "n1": {"categories": ["biolink:Gene"]},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]},
            },
        },
    },
    {
        "label": "2-hop: Drug -> Gene -> Disease",
        "query": {
            "nodes": {
                "n0": {"ids": ["CHEBI:6801"]},
                "n1": {"categories": ["biolink:Gene"]},
                "n2": {"categories": ["biolink:Disease"]},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]},
                "e1": {"subject": "n1", "object": "n2", "predicates": ["biolink:gene_associated_with_condition"]},
            },
        },
    },
    {
        "label": "1-hop + qualifiers: affects decreased activity",
        "query": {
            "nodes": {
                "n0": {"ids": ["CHEBI:6801"]},
                "n1": {"categories": ["biolink:Gene"]},
            },
            "edges": {
                "e0": {
                    "subject": "n0", "object": "n1",
                    "predicates": ["biolink:affects"],
                    "qualifier_constraints": [{"qualifier_set": [
                        {"qualifier_type_id": "biolink:object_aspect_qualifier", "qualifier_value": "activity_or_abundance"},
                        {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "decreased"},
                    ]}],
                },
            },
        },
    },
    {
        "label": "Branching: Drug -> Gene, Drug -> Disease",
        "query": {
            "nodes": {
                "n0": {"ids": ["CHEBI:6801"]},
                "n1": {"categories": ["biolink:Gene"]},
                "n2": {"categories": ["biolink:Disease"]},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]},
                "e1": {"subject": "n0", "object": "n2", "predicates": ["biolink:treats_or_applied_or_studied_to_treat"]},
            },
        },
    },
    {
        "label": "Symmetric: MTOR -[interacts_with]-> Gene",
        "query": {
            "nodes": {
                "n0": {"ids": ["NCBIGene:2475"]},
                "n1": {"categories": ["biolink:Gene"]},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:physically_interacts_with"]},
            },
        },
    },
    {
        "label": "Cyclic: Drug -> Gene -> Disease -> Drug",
        "query": {
            "nodes": {
                "n0": {"ids": ["CHEBI:6801"]},
                "n1": {"categories": ["biolink:Gene"]},
                "n2": {"categories": ["biolink:Disease"]},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]},
                "e1": {"subject": "n1", "object": "n2", "predicates": ["biolink:gene_associated_with_condition"]},
                "e2": {"subject": "n0", "object": "n2", "predicates": ["biolink:treats_or_applied_or_studied_to_treat"]},
            },
        },
    },
]

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRAPI Test UI</title>
<style>
:root {
  --bg: #f0f2f5; --surface: #ffffff; --border: #d1d5db; --border-light: #e5e7eb;
  --text: #1f2937; --text-secondary: #6b7280; --accent: #2563eb; --accent-hover: #1d4ed8;
  --mono: "SF Mono", "Fira Code", "JetBrains Mono", "Consolas", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
  --radius: 8px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { height: 100%; }
body { font-family: var(--sans); background: var(--bg); color: var(--text);
       display: flex; flex-direction: column; padding: 16px 20px; gap: 12px;
       height: 100%; overflow: hidden; }

/* ── Header ─────────────────────────────────────────────────────────── */
header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; flex-shrink: 0; }
header h1 { font-size: 1.2em; font-weight: 700; white-space: nowrap; }
.graph-info { font-size: 0.78em; color: var(--text-secondary);
              background: var(--surface); border: 1px solid var(--border-light);
              border-radius: 20px; padding: 3px 12px; }

/* ── Examples ───────────────────────────────────────────────────────── */
.examples { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex-shrink: 0; }
.examples span { font-size: 0.8em; font-weight: 600; color: var(--text-secondary); }
.ex-btn { font-size: 0.78em; padding: 4px 10px; border: 1px solid var(--border);
          border-radius: 14px; background: var(--surface); cursor: pointer;
          color: var(--text); transition: all 0.15s; white-space: nowrap; }
.ex-btn:hover { border-color: var(--accent); color: var(--accent); background: #eff6ff; }

/* ── Main layout ────────────────────────────────────────────────────── */
.main { display: flex; gap: 16px; flex: 1; min-height: 0; }
.pane { display: flex; flex-direction: column; min-height: 0; }
.pane-input { flex: 0 0 42%; }
.pane-output { flex: 1; }

.pane-header { display: flex; align-items: center; justify-content: space-between;
               margin-bottom: 6px; min-height: 32px; flex-shrink: 0; }
.pane-label { font-size: 0.8em; font-weight: 600; color: var(--text-secondary);
              text-transform: uppercase; letter-spacing: 0.04em; }

/* ── Input textarea ─────────────────────────────────────────────────── */
#query-input { flex: 1; width: 100%; font-family: var(--mono); font-size: 0.82em;
               line-height: 1.5; border: 1px solid var(--border); border-radius: var(--radius);
               padding: 12px 14px; resize: none; background: var(--surface);
               color: var(--text); outline: none; tab-size: 2; }
#query-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,0.12); }

/* ── Controls ───────────────────────────────────────────────────────── */
.controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
            margin-top: 8px; flex-shrink: 0; }
.btn { padding: 6px 16px; border-radius: 6px; border: 1px solid var(--border);
       font-size: 0.82em; cursor: pointer; background: var(--surface);
       color: var(--text); transition: all 0.15s; font-family: var(--sans); }
.btn:hover { background: #f3f4f6; }
.btn-primary { background: var(--accent); color: #fff; border-color: var(--accent);
               font-weight: 600; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.field { font-size: 0.82em; color: var(--text-secondary); display: flex;
         align-items: center; gap: 4px; }
.field input { width: 56px; padding: 5px 6px; border: 1px solid var(--border);
               border-radius: 5px; font-size: 0.92em; text-align: center;
               font-family: var(--mono); }
.shortcut { font-size: 0.75em; color: var(--text-secondary); margin-left: auto; }
kbd { font-family: var(--sans); font-size: 0.9em; padding: 1px 5px;
      border: 1px solid var(--border); border-radius: 3px; background: #f9fafb; }

/* ── Tabs ───────────────────────────────────────────────────────────── */
.tab-bar { display: flex; gap: 0; }
.tab { padding: 5px 16px; border: 1px solid var(--border); border-bottom: none;
       border-radius: var(--radius) var(--radius) 0 0; font-size: 0.8em;
       cursor: pointer; background: #f3f4f6; color: var(--text-secondary);
       transition: all 0.15s; user-select: none; }
.tab:hover { color: var(--text); }
.tab.active { background: var(--surface); color: var(--text); font-weight: 600;
              position: relative; z-index: 1; }

/* ── Output ─────────────────────────────────────────────────────────── */
#result { flex: 1; border: 1px solid var(--border); border-radius: 0 var(--radius)
          var(--radius) var(--radius); padding: 14px 16px; background: var(--surface);
          overflow: auto; min-height: 0; margin-top: -1px; }
#result.summary-view { font-family: var(--mono); font-size: 0.82em; line-height: 1.55;
                       white-space: pre-wrap; word-break: break-word; color: var(--text); }
#status { font-size: 0.78em; color: var(--text-secondary); margin-top: 6px;
          min-height: 1.4em; flex-shrink: 0; }

/* ── JSON tree ──────────────────────────────────────────────────────── */
.jt { font-family: var(--mono); font-size: 0.82em; line-height: 1.5; }
.jt-key { color: #7c3aed; }
.jt-str { color: #059669; }
.jt-num { color: #2563eb; }
.jt-bool { color: #d97706; font-weight: 600; }
.jt-null { color: #9ca3af; font-style: italic; }
.jt-bracket { color: #6b7280; }
.jt-toggle { cursor: pointer; user-select: none; position: relative;
             padding-left: 14px; display: inline; }
.jt-toggle::before { content: "\\25B6"; position: absolute; left: 0; top: 0;
                     font-size: 0.6em; line-height: 1.5em; color: #9ca3af;
                     transition: transform 0.12s; display: inline-block; }
.jt-toggle.open::before { transform: rotate(90deg); }
.jt-block { margin-left: 18px; }
.jt-hidden { display: none; }
.jt-ellipsis { color: #9ca3af; cursor: pointer; }
.jt-comma { color: #6b7280; }

/* ── Collapse controls ──────────────────────────────────────────────── */
.json-controls { display: flex; gap: 8px; }
.json-controls .btn { font-size: 0.75em; padding: 2px 8px; }

/* ── Graph container ───────────────────────────────────────────────── */
#cy-container { width: 100%; height: 100%; }
.graph-controls { display: flex; gap: 8px; }
.graph-controls .btn { font-size: 0.75em; padding: 2px 8px; }
</style>
<script src="https://unpkg.com/cytoscape@3/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/layout-base@2/layout-base.js"></script>
<script src="https://unpkg.com/cose-base@2/cose-base.js"></script>
<script src="https://unpkg.com/cytoscape-fcose@2/cytoscape-fcose.js"></script>
</head>
<body>

<header>
  <h1>TRAPI Test UI</h1>
  <span class="graph-info" id="graph-info">loading...</span>
</header>

<div class="examples">
  <span>Examples:</span>
  %%EXAMPLE_BUTTONS%%
</div>

<div class="main">
  <!-- ── Input pane ──────────────────────────────────────── -->
  <div class="pane pane-input">
    <div class="pane-header">
      <span class="pane-label">Query Graph</span>
      <button class="btn" style="font-size:0.75em;padding:2px 8px" onclick="formatJSON()">Format</button>
    </div>
    <textarea id="query-input" spellcheck="false">%%DEFAULT_QUERY%%</textarea>
    <div class="controls">
      <button class="btn btn-primary" id="run-btn" onclick="runQuery()">Run Query</button>
      <span class="field">Limit <input id="limit" type="number" value="20" min="1" max="1000"></span>
      <span class="shortcut"><kbd>%%CMD%%</kbd>+<kbd>Enter</kbd> to run</span>
    </div>
  </div>

  <!-- ── Output pane ─────────────────────────────────────── -->
  <div class="pane pane-output">
    <div class="pane-header">
      <div class="tab-bar">
        <div class="tab active" id="tab-summary" onclick="switchTab('summary')">Summary</div>
        <div class="tab" id="tab-json" onclick="switchTab('json')">JSON</div>
        <div class="tab" id="tab-graph" onclick="switchTab('graph')">Graph</div>
      </div>
      <div class="json-controls" id="json-controls" style="display:none">
        <button class="btn" onclick="collapseAll()">Collapse All</button>
        <button class="btn" onclick="expandAll()">Expand All</button>
        <button class="btn" onclick="collapseToDepth(2)">Depth 2</button>
      </div>
      <div class="graph-controls" id="graph-controls" style="display:none">
        <button class="btn" onclick="cyFit()">Fit</button>
        <button class="btn" onclick="cyRelayout()">Re-layout</button>
      </div>
    </div>
    <div id="result" class="summary-view"></div>
    <div id="status"></div>
  </div>
</div>

<script>
/* ── State ──────────────────────────────────────────────────────────── */
const examples = %%EXAMPLES_JSON%%;
let currentTab = 'summary';
let lastSummary = '';
let lastJsonObj = null;

/* ── Graph info ─────────────────────────────────────────────────────── */
fetch('/meta').then(r=>r.json()).then(d=>{
  document.getElementById('graph-info').textContent =
    d.num_nodes.toLocaleString()+' nodes  \\u00b7  '+
    d.edge_count.toLocaleString()+' edges  \\u00b7  '+
    d.num_predicates+' predicates';
}).catch(()=>{});

/* ── Examples ───────────────────────────────────────────────────────── */
function loadExample(i){
  document.getElementById('query-input').value = JSON.stringify(examples[i], null, 2);
}

/* ── Format ─────────────────────────────────────────────────────────── */
function formatJSON(){
  const ta = document.getElementById('query-input');
  try { ta.value = JSON.stringify(JSON.parse(ta.value), null, 2); }
  catch(e){ setStatus('Invalid JSON: '+e.message); }
}

/* ── Tabs ───────────────────────────────────────────────────────────── */
function switchTab(tab){
  currentTab = tab;
  ['summary','json','graph'].forEach(t=>{
    document.getElementById('tab-'+t).className = t===tab?'tab active':'tab';
  });
  document.getElementById('json-controls').style.display = tab==='json'?'flex':'none';
  document.getElementById('graph-controls').style.display = tab==='graph'?'flex':'none';
  renderOutput();
}

function renderOutput(){
  const el = document.getElementById('result');
  destroyCy();
  if(currentTab==='summary'){
    el.className = 'summary-view';
    el.style.padding = ''; el.style.overflow = '';
    el.textContent = lastSummary;
  } else if(currentTab==='json'){
    el.className = '';
    el.style.padding = ''; el.style.overflow = '';
    el.innerHTML = '';
    if(lastJsonObj){
      const tree = document.createElement('div');
      tree.className = 'jt';
      tree.appendChild(buildTree(lastJsonObj, 0));
      el.appendChild(tree);
    }
  } else {
    el.className = '';
    el.style.padding = '0';
    el.style.overflow = 'hidden';
    el.innerHTML = '';
    if(lastJsonObj) renderGraph(el, lastJsonObj);
  }
}

/* ── Status ─────────────────────────────────────────────────────────── */
function setStatus(msg){ document.getElementById('status').textContent = msg; }

/* ── Query ──────────────────────────────────────────────────────────── */
async function runQuery(){
  const btn = document.getElementById('run-btn');
  btn.disabled = true;
  setStatus('Running...');
  lastSummary = ''; lastJsonObj = null;
  renderOutput();

  let qg;
  try { qg = JSON.parse(document.getElementById('query-input').value); }
  catch(e){ setStatus('Invalid JSON: '+e.message); btn.disabled=false; return; }

  const limit = parseInt(document.getElementById('limit').value)||20;
  const body = { message:{ query_graph: qg }, limit };

  try {
    const t0 = performance.now();
    const [sResp, jResp] = await Promise.all([
      fetch('/query?summary=true',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
      fetch('/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
    ]);
    const elapsed = ((performance.now()-t0)/1000).toFixed(2);

    lastSummary = await sResp.text();
    lastJsonObj = await jResp.json();
    renderOutput();

    const nr = lastJsonObj.message?.results?.length ?? '?';
    setStatus(nr+' results  ('+elapsed+'s round-trip)');
  } catch(e){
    setStatus('Error: '+e.message);
  } finally { btn.disabled=false; }
}

/* ── JSON tree builder ──────────────────────────────────────────────── */
function buildTree(val, depth){
  if(val===null)  return span('null','jt-null');
  if(val===true)  return span('true','jt-bool');
  if(val===false) return span('false','jt-bool');
  if(typeof val==='number') return span(String(val),'jt-num');
  if(typeof val==='string') return span('"'+escHtml(val)+'"','jt-str');
  if(Array.isArray(val))    return buildCompound(val, depth, true);
  if(typeof val==='object') return buildCompound(val, depth, false);
  return document.createTextNode(String(val));
}

function buildCompound(obj, depth, isArray){
  const entries = isArray ? obj.map((v,i)=>[i,v]) : Object.entries(obj);
  const open = isArray?'[':'{', close = isArray?']':'}';

  if(entries.length===0){
    const s = document.createDocumentFragment();
    s.appendChild(span(open+close,'jt-bracket'));
    return s;
  }

  const frag = document.createDocumentFragment();

  // toggle line
  const toggle = document.createElement('span');
  toggle.className = 'jt-toggle open';
  toggle.dataset.depth = depth;
  toggle.appendChild(span(open,'jt-bracket'));

  // collapsed preview
  const preview = document.createElement('span');
  preview.className = 'jt-ellipsis jt-hidden';
  const previewText = isArray
    ? entries.length+' items'
    : entries.slice(0,3).map(([k])=>k).join(', ')+(entries.length>3?', ...':'');
  preview.textContent = ' '+previewText+' ';
  preview.addEventListener('click', ()=>{ toggleNode(toggle); });

  // closing bracket for collapsed state
  const closeCollapsed = span(close,'jt-bracket');
  closeCollapsed.className += ' jt-hidden';
  closeCollapsed.dataset.role = 'close-collapsed';

  toggle.appendChild(preview);
  toggle.appendChild(closeCollapsed);
  toggle.addEventListener('click', function(e){
    if(e.target===this || e.target.classList.contains('jt-bracket'))
      toggleNode(this);
  });

  frag.appendChild(toggle);

  // child block
  const block = document.createElement('div');
  block.className = 'jt-block';

  entries.forEach(([key, val], idx)=>{
    const line = document.createElement('div');
    if(!isArray){
      line.appendChild(span('"'+escHtml(String(key))+'"','jt-key'));
      line.appendChild(document.createTextNode(': '));
    }
    line.appendChild(buildTree(val, depth+1));
    if(idx < entries.length-1){
      line.appendChild(span(',','jt-comma'));
    }
    block.appendChild(line);
  });

  frag.appendChild(block);
  frag.appendChild(span(close,'jt-bracket'));

  return frag;
}

function toggleNode(el){
  const isOpen = el.classList.contains('open');
  el.classList.toggle('open');
  // next sibling is the block, sibling after is closing bracket
  const block = el.nextElementSibling;
  const closeBracket = block?.nextElementSibling;
  const preview = el.querySelector('.jt-ellipsis');
  const closeCol = el.querySelector('[data-role="close-collapsed"]');
  if(isOpen){
    if(block) block.classList.add('jt-hidden');
    if(closeBracket) closeBracket.classList.add('jt-hidden');
    if(preview) preview.classList.remove('jt-hidden');
    if(closeCol) closeCol.classList.remove('jt-hidden');
  } else {
    if(block) block.classList.remove('jt-hidden');
    if(closeBracket) closeBracket.classList.remove('jt-hidden');
    if(preview) preview.classList.add('jt-hidden');
    if(closeCol) closeCol.classList.add('jt-hidden');
  }
}

/* ── Collapse / expand controls ─────────────────────────────────────── */
function collapseAll(){
  document.querySelectorAll('.jt-toggle.open').forEach(el=>toggleNode(el));
}
function expandAll(){
  document.querySelectorAll('.jt-toggle:not(.open)').forEach(el=>toggleNode(el));
}
function collapseToDepth(maxDepth){
  expandAll();
  document.querySelectorAll('.jt-toggle').forEach(el=>{
    const d = parseInt(el.dataset.depth);
    if(d >= maxDepth && el.classList.contains('open')) toggleNode(el);
  });
}

/* ── Helpers ─────────────────────────────────────────────────────────── */
function span(text, cls){
  const s = document.createElement('span');
  s.className = cls;
  s.textContent = text;
  return s;
}
function escHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* ── Cytoscape.js graph ────────────────────────────────────────────── */
const CAT_COLORS = {
  Gene:'#3b82f6', Disease:'#ef4444', SmallMolecule:'#10b981', Drug:'#10b981',
  Protein:'#8b5cf6', BiologicalProcess:'#f59e0b', Pathway:'#f59e0b',
  Cell:'#ec4899', AnatomicalEntity:'#06b6d4', PhenotypicFeature:'#f97316',
};
function catColor(cats){
  if(!cats) return '#6b7280';
  for(const c of cats){
    const short = c.replace('biolink:','');
    if(CAT_COLORS[short]) return CAT_COLORS[short];
  }
  return '#6b7280';
}

let cyInstance = null;
function destroyCy(){ if(cyInstance){ cyInstance.destroy(); cyInstance=null; } }
function cyFit(){ if(cyInstance) cyInstance.fit(50); }
function cyRelayout(){
  if(!cyInstance) return;
  cyInstance.layout({
    name:'fcose', animate:true, animationDuration:600,
    quality:'proof',
    nodeRepulsion:()=>10000,
    idealEdgeLength:()=>150,
    nodeSeparation:80,
    gravity:0.25,
    gravityRange:3.8,
    numIter:2500,
  }).run();
}

function renderGraph(container, data){
  const kg = data.message?.knowledge_graph;
  if(!kg) return;

  const div = document.createElement('div');
  div.id = 'cy-container';
  container.appendChild(div);

  const elements = [];

  Object.entries(kg.nodes).forEach(([id, n])=>{
    const label = n.name || id;
    const cats = (n.categories||[]).map(c=>c.replace('biolink:','')).join(', ');
    elements.push({
      group:'nodes',
      data:{ id, label, cats, color: catColor(n.categories) },
    });
  });

  Object.entries(kg.edges).forEach(([eid, e])=>{
    const label = (e.predicate||'').replace('biolink:','');
    elements.push({
      group:'edges',
      data:{ id: eid, source: e.subject, target: e.object, label },
    });
  });

  cyInstance = cytoscape({
    container: div,
    elements,
    style: [
      { selector:'node', style:{
        'background-color':'data(color)',
        'label':'data(label)',
        'font-size':'11px',
        'text-valign':'bottom',
        'text-margin-y':'4px',
        'color':'#1f2937',
        'text-outline-color':'#fff',
        'text-outline-width':1.5,
        'width':20, 'height':20,
        'border-width':2, 'border-color':'#fff',
        'text-max-width':'120px',
        'text-wrap':'ellipsis',
      }},
      { selector:'node:active', style:{
        'overlay-opacity':0,
      }},
      { selector:'edge', style:{
        'width':2,
        'line-color':'#c4b5fd',
        'target-arrow-color':'#a78bfa',
        'target-arrow-shape':'triangle',
        'curve-style':'bezier',
        'label':'data(label)',
        'font-size':'9px',
        'color':'#8b5cf6',
        'text-rotation':'autorotate',
        'text-outline-color':'#fff',
        'text-outline-width':1.5,
        'text-margin-y':'-8px',
      }},
      { selector:'node:selected', style:{
        'border-color':'#2563eb', 'border-width':3,
      }},
    ],
    layout:{
      name:'fcose',
      animate:false,
      quality:'proof',
      nodeRepulsion:()=>10000,
      idealEdgeLength:()=>150,
      nodeSeparation:80,
      gravity:0.25,
      gravityRange:3.8,
      numIter:2500,
    },
    minZoom:0.1, maxZoom:10,
    wheelSensitivity:0.3,
  });

  /* show CURIE on hover */
  cyInstance.on('mouseover','node', e=>{
    const n = e.target;
    n.style('label', n.data('id')+'\\n'+n.data('label'));
    n.style('font-size','12px');
    n.style('font-weight','bold');
  });
  cyInstance.on('mouseout','node', e=>{
    const n = e.target;
    n.style('label', n.data('label'));
    n.style('font-size','11px');
    n.style('font-weight','normal');
  });
}

/* ── Keyboard shortcut ──────────────────────────────────────────────── */
document.getElementById('query-input').addEventListener('keydown', function(e){
  if((e.metaKey||e.ctrlKey) && e.key==='Enter'){ e.preventDefault(); runQuery(); }
  /* Tab key inserts two spaces instead of moving focus */
  if(e.key==='Tab'){
    e.preventDefault();
    const s=this.selectionStart, end=this.selectionEnd;
    this.value = this.value.substring(0,s)+'  '+this.value.substring(end);
    this.selectionStart = this.selectionEnd = s+2;
  }
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def test_ui() -> str:
    """Serve the test UI."""
    import json as _json
    import platform

    cmd_key = "Cmd" if platform.system() == "Darwin" else "Ctrl"
    buttons = " ".join(
        f'<button class="ex-btn" onclick="loadExample({i})">{ex["label"]}</button>'
        for i, ex in enumerate(_EXAMPLES)
    )
    default_query = _json.dumps(_EXAMPLES[0]["query"], indent=2)
    examples_json = _json.dumps([ex["query"] for ex in _EXAMPLES])

    html = _HTML_TEMPLATE.replace("%%EXAMPLE_BUTTONS%%", buttons)
    html = html.replace("%%DEFAULT_QUERY%%", default_query)
    html = html.replace("%%EXAMPLES_JSON%%", examples_json)
    html = html.replace("%%CMD%%", cmd_key)
    return html


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/query")
async def trapi_query(
    body: dict,
    summary: bool = Query(False, description="Return plain-text summary instead of JSON"),
):
    """Execute a TRAPI query.

    Accepts a TRAPI request body with ``message.query_graph``.
    Returns a TRAPI response with ``message`` containing
    ``query_graph``, ``knowledge_graph``, and ``results``.

    When ``?summary=true``, returns a plain-text summary showing the
    ASCII query graph visualization and a compact result listing.
    """
    if _graph is None:
        return JSONResponse(
            status_code=503,
            content={"error": "graph not loaded"},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "request body must be a JSON object"},
        )

    # Extract query_graph from the TRAPI envelope.
    message = body.get("message", body)
    query_graph = message.get("query_graph") if isinstance(message, dict) else None

    if not isinstance(query_graph, dict) or not query_graph:
        return JSONResponse(
            status_code=400,
            content={"error": "missing or invalid message.query_graph"},
        )
    if not isinstance(query_graph.get("nodes"), dict) or not isinstance(
        query_graph.get("edges"), dict
    ):
        return JSONResponse(
            status_code=400,
            content={"error": "query_graph must contain 'nodes' and 'edges' objects"},
        )

    # Optional limit from the request body; coerce and clamp to a safe range
    # to prevent resource-exhaustion via huge or malformed values.
    try:
        limit = int(body.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"error": "'limit' must be an integer"},
        )
    limit = max(1, min(limit, _MAX_LIMIT))

    t0 = time.time()
    try:
        # Logging is inside the try: rendering the query graph reads it, so a
        # malformed one must not escape the handler before validation runs.
        print(f"\n--- TRAPI query (limit={limit}) ---")
        print(display_query_graph(query_graph))

        # Run the synchronous, CPU/IO-heavy query off the event loop so it
        # does not block other requests (incl. /health).
        result_message = await run_in_threadpool(
            query, _graph, query_graph, limit=limit
        )
    except ValueError as exc:
        # trapi._validate_query_graph raises ValueError for a query graph that is
        # well-formed JSON but not a valid graph — an edge naming a node that
        # does not exist, or the Pathfinder 'paths' shape.  That is the client's
        # error, and the message names the offending element.
        print(f"  -> invalid query graph: {exc}")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid query graph", "detail": str(exc)},
        )
    except Exception as exc:
        # Anything else is a fault on our side.  Reporting these as 400 told
        # callers their request was bad when the server had failed, which also
        # hides real breakage from anything alerting on 5xx.
        traceback.print_exc()
        print(f"  -> query failed: {type(exc).__name__}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": "internal error", "detail": str(exc)},
        )
    elapsed_ms = (time.time() - t0) * 1000

    n_results = len(result_message["results"])
    n_kg_nodes = len(result_message["knowledge_graph"]["nodes"])
    n_kg_edges = len(result_message["knowledge_graph"]["edges"])
    print(
        f"  -> {n_results} results, {n_kg_nodes} KG nodes, "
        f"{n_kg_edges} KG edges  ({elapsed_ms:.1f}ms)"
    )

    if summary:
        text = _format_summary(query_graph, result_message, elapsed_ms)
        return PlainTextResponse(text)

    return JSONResponse(content={"message": result_message})


def _format_summary(
    query_graph: dict,
    msg: dict,
    elapsed_ms: float,
    max_results: int = 20,
) -> str:
    """Build a plain-text summary of the query and results."""
    lines: list[str] = []

    # -- Query graph visualization --
    lines.append("Query Graph")
    lines.append("-" * 40)
    lines.append(display_query_graph(query_graph))
    lines.append("")

    # -- Stats --
    results = msg["results"]
    kg = msg["knowledge_graph"]
    lines.append(
        f"Results: {len(results)}  |  "
        f"KG nodes: {len(kg['nodes'])}  |  KG edges: {len(kg['edges'])}  |  "
        f"{elapsed_ms:.1f}ms"
    )
    lines.append("")

    # -- Compact result listing --
    def _name(curie: str) -> str:
        n = kg["nodes"].get(curie, {})
        return n.get("name") or curie

    for i, r in enumerate(results[:max_results]):
        node_str = "  ".join(
            f"{k}={_name(v[0]['id'])}"
            for k, v in r["node_bindings"].items()
        )
        lines.append(f"  [{i + 1}] {node_str}")
        for a in r["analyses"]:
            for ek, ebs in a["edge_bindings"].items():
                eid = ebs[0]["id"]
                e = kg["edges"].get(eid, {})
                lines.append(
                    f"       {ek}: {e.get('subject', '')} "
                    f"--[{e.get('predicate', '')}]--> {e.get('object', '')}"
                )

    if len(results) > max_results:
        lines.append(f"  ... and {len(results) - max_results} more")

    lines.append("")
    return "\n".join(lines)


@app.get("/meta")
async def meta():
    """Return basic graph metadata."""
    if _graph is None:
        return JSONResponse(status_code=503, content={"error": "graph not loaded"})
    return {
        "num_nodes": _graph.num_nodes,
        "edge_count": _graph.edge_count,
        "num_predicates": len(_graph.relations),
        "predicates": [f"biolink:{r}" for r in _graph.relations],
    }


def _version_payload() -> dict:
    """What `/version` and `/health` both report."""
    ready = _graph is not None
    payload: dict = {
        "ready": ready,
        "store_format_version": STORE_FORMAT_VERSION,
        "versioned_release": _manifest is not None,
    }
    if _manifest is not None:
        payload.update({
            "graph_name": _manifest.get("graph_name"),
            "version": _manifest.get("version"),
            "built_at": _manifest.get("built_at"),
            "source_kgx": _manifest.get("source_kgx"),
            "node_count": _manifest.get("node_count"),
            "edge_count": _manifest.get("edge_count"),
            "variant_count": _manifest.get("variant_count"),
        })
    if ready:
        payload["loaded"] = {
            "nodes": _graph.num_nodes,
            "predicates": len(_graph.relations),
        }
    return payload


@app.get("/version")
async def version() -> JSONResponse:
    """Deployed release identity and readiness.

    The gate for a health-checked swap: an updater flips traffic only once this
    reports the version it just installed, and a Kubernetes readiness probe uses
    the same signal so a rolling update drains old pods only after new ones serve.
    Returns 503 until the graph is loaded, so "up" and "ready" stay distinct.
    """
    payload = _version_payload()
    return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)


@app.get("/health")
async def health() -> JSONResponse:
    """Health check, including the deployed release."""
    payload = _version_payload()
    payload["status"] = "ok" if payload["ready"] else "loading"
    payload["graph_loaded"] = payload["ready"]
    return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TRAPI test server")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_DATA_DIR,
        help=(
            "Directory containing graph and LMDB files "
            f"(env DATA_DIR, default: {_DEFAULT_DATA_DIR})"
        ),
    )
    parser.add_argument(
        "--graph",
        default="translator_kg_2026-07-19",
        help="Graph name stem, e.g. 'translator_kg_2026-07-19' or 'dgidb' "
             f"(default: translator_kg_2026-07-19)",
    )
    parser.add_argument(
        "--es-host",
        default=es_host_from_env(),
        help="Elasticsearch host (env CSRGRAPH_ES_HOST, "
             "default: http://localhost:9200)",
    )
    parser.add_argument(
        "--no-es",
        action="store_true",
        help="Skip Elasticsearch, use LMDB-only",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port (default: 8000)",
    )
    args = parser.parse_args()

    try:
        _load_graph(args.data_dir, args.graph, es_host=args.es_host, no_es=args.no_es)
    except StoreFormatMismatch as exc:
        # Exit non-zero with the operator-facing reason, not a traceback. A
        # supervisor that restarts on failure will keep failing, which is correct:
        # the fix is a rebuild or a code rollback, not a retry.
        sys.exit(f"\nSTORE FORMAT MISMATCH\n{exc}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
