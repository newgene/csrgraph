"""Demo: production usage of CSRGraph with pre-built cached data.

Copy-paste these blocks into a Python console to test interactively.
No original KGX archive needed — only pre-built cache files.

Prerequisites in DATA_DIR (default: ~/tmp/csrgraph_data/):
    translator_kg_2026-07-19.csrgraph.pkl.zst     (graph topology cache)
    translator_kg_2026-07-19.metadata.lmdb/       (LMDB metadata)
    translator_kg_2026-07-19_nodes / _edges       (ES indices, already built)
"""

# %% Setup — run this block first
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csrgraph_kgx import CSRGraph
from metadata_db import (
    LMDBMetadataBackend,
    ElasticsearchMetadataBackend,
    HybridMetadataBackend,
)

DATA_DIR = Path.home() / "tmp" / "csrgraph_data"
ES_HOST = "http://localhost:9200"
ES_PREFIX = "translator_kg_2026-07-19"

# %% 1. Load graph with metadata backend — single entry point
lmdb = LMDBMetadataBackend(str(DATA_DIR / "translator_kg_2026-07-19.metadata.lmdb"))
es = ElasticsearchMetadataBackend(host=ES_HOST, index_prefix=ES_PREFIX)
db = HybridMetadataBackend(lmdb=lmdb, es=es)

t0 = time.time()
graph = CSRGraph.load(str(DATA_DIR / "translator_kg_2026-07-19.csrgraph.pkl.zst"), db=db)
print(f"Loaded in {time.time() - t0:.3f}s  —  "
      f"{graph.num_nodes:,} nodes, {graph.edge_count:,} edges, "
      f"{len(graph.relations)} predicates")

# Alternative: attach db after loading
#   graph = CSRGraph.load(str(DATA_DIR / "translator_kg_2026-07-19.csrgraph.pkl.zst"))
#   graph.set_db(db)

# %% Helper: resolve display name from metadata
def name(nid):
    meta = graph.get_node(nid)
    return meta.get("name", nid) if meta else nid

# %% 2. Graph stats — top predicates
for pred, count in sorted(graph.predicate_counts.items(), key=lambda x: -x[1])[:10]:
    print(f"  biolink:{pred:50s} {count:>10,}")

# %% 3. Neighbor queries — Metformin
drug = "CHEBI:6801"
print(f"{drug} ({name(drug)})")

nbrs = graph.neighbors(drug)
print(f"Total neighbors: {len(nbrs)}")
for n in nbrs[:5]:
    print(f"  -> {n} ({name(n)})")

# Filter to Gene neighbors only
t0 = time.time()
gene_nbrs = graph.filter_nodes(nbrs, category="biolink:Gene")
print(f"\nGene neighbors: {len(gene_nbrs)} of {len(nbrs)} ({(time.time()-t0)*1000:.1f}ms)")
for n in gene_nbrs[:5]:
    print(f"  {n['id']:20s}  {n.get('name', '')}")

# %% 4. Shortest path — Metformin → Type 2 Diabetes
source, target = "CHEBI:6801", "MONDO:0005148"
print(f"{name(source)} → {name(target)}")

t0 = time.time()
sp = graph.shortest_path(source, target)
print(f"({(time.time()-t0)*1000:.1f}ms)")
if sp:
    for s, p, t in sp:
        print(f"  {name(s):30s} --[{p}]--> {name(t)}")

# %% 5. Filter path edges by knowledge level
if sp:
    curated = graph.filter_edges(sp, knowledge_level="knowledge_assertion")
    print(f"Curated edges: {len(curated)} of {len(sp)}")
    for e in curated:
        print(f"  {e['subject']} --[{e['predicate']}]--> {e['object']}")
        print(f"    kl={e.get('knowledge_level')}  agent_type={e.get('agent_type')}")

# %% 6. All shortest paths
t0 = time.time()
all_sp = graph.all_shortest_paths(source, target)
print(f"{len(all_sp)} shortest path(s) ({(time.time()-t0)*1000:.1f}ms)")
for i, path in enumerate(all_sp[:3]):
    print(f"\nPath {i+1}:")
    for s, p, t in path:
        print(f"  {name(s):30s} --[{p}]--> {name(t)}")

# %% 7. match_path — 2-hop: Drug → Gene → Disease (no need to pass db)
t0 = time.time()
paths = graph.match_path([
    "CHEBI:6801",                          # start: Metformin
    None,                                  # any predicate
    {"category": "biolink:Gene"},          # intermediate: any Gene
    None,                                  # any predicate
    {"category": "biolink:Disease"},       # end: any Disease
], limit=5)
print(f"{len(paths)} path(s) ({(time.time()-t0)*1000:.1f}ms)")
for i, path in enumerate(paths):
    print(f"\nPath {i+1}:")
    for s, p, t in path:
        print(f"  {name(s):30s} --[{p}]--> {name(t)}")

# %% 8. Predicate-sequence path matching
seq = ["biolink:affects", "biolink:gene_associated_with_condition"]
t0 = time.time()
seq_paths = graph.paths_by_predicate_sequence(source, target, seq)
print(f"Predicate sequence {seq}")
print(f"{len(seq_paths)} path(s) ({(time.time()-t0)*1000:.1f}ms)")
for i, path in enumerate(seq_paths[:3]):
    print(f"\nPath {i+1}:")
    for s, p, t in path:
        print(f"  {name(s):30s} --[{p}]--> {name(t)}")

# %% 9. Single node metadata lookup
meta = graph.get_node("CHEBI:6801")
for k, v in meta.items():
    print(f"  {k}: {str(v)[:80]}")

# %% 10. ES full-text search for nodes by name
resp = es._es.search(
    index=f"{ES_PREFIX}_nodes",
    query={"match": {"name": "metformin"}},
    size=5,
)
for hit in resp["hits"]["hits"]:
    src = hit["_source"]
    print(f"  {src['id']:25s}  {src.get('name',''):40s}  {src.get('category',[])}")

# %% Cleanup
graph.close()
