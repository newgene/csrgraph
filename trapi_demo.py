"""Demo: TRAPI QueryGraph support for CSRGraph.

Copy-paste these blocks into a Python console to test interactively.
Each block is a self-contained TRAPI query example.

Prerequisites in DATA_DIR (default: ~/tmp/csrgraph_data/):
    translator_kg_2026-07-19.csrgraph.pkl.zst     (graph topology cache)
    translator_kg_2026-07-19.metadata.lmdb/       (LMDB metadata)
"""

# %% Setup — run this block first
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csrgraph_kgx import CSRGraph
from metadata_db import LMDBMetadataBackend
from trapi import display_query_graph, query

DATA_DIR = Path.home() / "tmp" / "csrgraph_data"

lmdb = LMDBMetadataBackend(str(DATA_DIR / "translator_kg_2026-07-19.metadata.lmdb"))
t0 = time.time()
graph = CSRGraph.load(str(DATA_DIR / "translator_kg_2026-07-19.csrgraph.pkl.zst"), db=lmdb)
print(f"Loaded in {time.time() - t0:.3f}s  —  "
      f"{graph.num_nodes:,} nodes, {graph.edge_count:,} edges, "
      f"{len(graph.relations)} predicates\n")


# %% Helpers
def show(msg, max_results=5):
    """Print a compact summary of a TRAPI response message."""
    results = msg["results"]
    kg = msg["knowledge_graph"]
    print(f"Results: {len(results)}  |  "
          f"KG nodes: {len(kg['nodes'])}  |  KG edges: {len(kg['edges'])}")

    def _name(curie):
        n = kg["nodes"].get(curie, {})
        return n.get("name") or curie

    for i, r in enumerate(results[:max_results]):
        node_str = "  ".join(
            f"{k}={_name(v[0]['id'])}" for k, v in r["node_bindings"].items()
        )
        print(f"  [{i+1}] {node_str}")
        for a in r["analyses"]:
            for ek, ebs in a["edge_bindings"].items():
                eid = ebs[0]["id"]
                e = kg["edges"].get(eid, {})
                print(f"       {ek}: {e.get('subject','')} "
                      f"--[{e.get('predicate','')}]--> {e.get('object','')}")
    if len(results) > max_results:
        print(f"  ... and {len(results) - max_results} more")
    print()


def run(label, qg, **kwargs):
    """Run a TRAPI query, display the query graph and results."""
    print("=" * 70)
    print(label)
    print("=" * 70)
    print(display_query_graph(qg))
    print()
    t0 = time.time()
    msg = query(graph, qg, **kwargs)
    print(f"({(time.time()-t0)*1000:.1f}ms)")
    show(msg)
    return msg


# =====================================================================
# 1. ONE-HOP: Known drug → Gene (with predicate)
#    "What genes does Metformin affect?"
# =====================================================================
# %% Query 1
run("1. One-hop: Metformin -[affects]-> Gene", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"], "categories": ["biolink:SmallMolecule"]},
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
    },
}, limit=10)


# =====================================================================
# 2. ONE-HOP: Known drug → any neighbor (no predicate filter)
#    "What is connected to Metformin?"
# =====================================================================
# %% Query 2
run("2. One-hop: Metformin -[any]-> ?", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {},
    },
    "edges": {
        "e0": {"subject": "n0", "object": "n1"},
    },
}, limit=10)


# =====================================================================
# 3. TWO-HOP: Drug → Gene → Disease (linear chain)
#    "What diseases are associated with genes that Metformin affects?"
# =====================================================================
# %% Query 3
run("3. Two-hop: Drug -> Gene -> Disease", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
        "n2": {"categories": ["biolink:Disease"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
        "e1": {
            "subject": "n1",
            "object": "n2",
            "predicates": ["biolink:gene_associated_with_condition"],
        },
    },
}, limit=10)


# =====================================================================
# 4. ONE-HOP: Known endpoints, verify edge
#    "Does Metformin affect PRKAB1?"
# =====================================================================
# %% Query 4
run("4. Edge verification: Metformin -[affects]-> PRKAB1", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"ids": ["NCBIGene:5564"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
    },
})


# =====================================================================
# 5. ONE-HOP with qualifier_constraints
#    "What genes does Metformin affect with decreased activity/abundance?"
# =====================================================================
# %% Query 5
run("5. One-hop + qualifier: affects with decreased activity_or_abundance", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
            "qualifier_constraints": [{
                "qualifier_set": [
                    {
                        "qualifier_type_id": "biolink:object_aspect_qualifier",
                        "qualifier_value": "activity_or_abundance",
                    },
                    {
                        "qualifier_type_id": "biolink:object_direction_qualifier",
                        "qualifier_value": "decreased",
                    },
                ],
            }],
        },
    },
}, limit=10)


# =====================================================================
# 6. TWO-HOP: Gene → Disease ← Drug (shared object node)
#    "What drugs treat diseases associated with TP53?"
# =====================================================================
# %% Query 6
run("6. Two-hop: TP53 -> Disease <- Drug (shared object)", {
    "nodes": {
        "n0": {"ids": ["NCBIGene:7157"]},  # TP53
        "n1": {"categories": ["biolink:Disease"]},
        "n2": {"categories": ["biolink:SmallMolecule"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:gene_associated_with_condition"],
        },
        "e1": {
            "subject": "n2",
            "object": "n1",
            "predicates": ["biolink:treats_or_applied_or_studied_to_treat"],
        },
    },
}, limit=10)


# =====================================================================
# 7. BRANCHING (fork): Drug → Gene AND Drug → Disease
#    "Find genes affected by and diseases treated by Metformin"
# =====================================================================
# %% Query 7
run("7. Branching: Drug -> Gene, Drug -> Disease", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
        "n2": {"categories": ["biolink:Disease"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
        "e1": {
            "subject": "n0",
            "object": "n2",
            "predicates": ["biolink:treats_or_applied_or_studied_to_treat"],
        },
    },
}, limit=10)


# =====================================================================
# 8. CYCLIC (triangle): Drug → Gene → Disease → Drug
#    "Find Drug-Gene-Disease triangles starting from Metformin"
# =====================================================================
# %% Query 8
run("8. Cyclic triangle: Drug -> Gene -> Disease -> Drug", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
        "n2": {"categories": ["biolink:Disease"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
        "e1": {
            "subject": "n1",
            "object": "n2",
            "predicates": ["biolink:gene_associated_with_condition"],
        },
        "e2": {
            "subject": "n0",
            "object": "n2",
            "predicates": ["biolink:treats_or_applied_or_studied_to_treat"],
        },
    },
}, limit=10)


# =====================================================================
# 9. MULTIPLE IDs (BATCH): query from multiple drugs at once
#    "What genes do Metformin OR Aspirin affect?"
# =====================================================================
# %% Query 9
run("9. Multiple IDs (BATCH): Metformin+Aspirin -[affects]-> Gene", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801", "CHEBI:15365"]},  # Metformin, Aspirin
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
    },
}, limit=20)


# =====================================================================
# 10. MULTIPLE PREDICATES (OR): affects OR treats
#    "What genes does Metformin affect or treat?"
# =====================================================================
# %% Query 10
run("10. Multiple predicates (OR): affects OR treats", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects", "biolink:interacts_with"],
        },
    },
}, limit=10)


# =====================================================================
# 11. SYMMETRIC PREDICATE: interacts_with (bidirectional)
#    "What genes interact with MTOR?" (should find edges in both directions)
# =====================================================================
# %% Query 11
run("11. Symmetric: MTOR -[interacts_with]-> Gene (bidirectional)", {
    "nodes": {
        "n0": {"ids": ["NCBIGene:2475"]},  # MTOR
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:physically_interacts_with"],
        },
    },
}, limit=10)


# =====================================================================
# 12. EDGE ATTRIBUTE CONSTRAINT: knowledge_level == knowledge_assertion
#    "Curated physical interactions for Metformin only"
# =====================================================================
# %% Query 12
run("12. Edge constraint: directly_physically_interacts_with + knowledge_assertion", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:directly_physically_interacts_with"],
            "attribute_constraints": [{
                "id": "biolink:knowledge_level",
                "name": "knowledge_level",
                "operator": "==",
                "value": "knowledge_assertion",
                "not": False,
            }],
        },
    },
}, limit=10)


# =====================================================================
# 13. THREE-HOP: Drug → Gene → Gene → Disease
#    "What diseases are linked to genes interacting with Metformin targets?"
# =====================================================================
# %% Query 13
run("13. Three-hop: Drug -> Gene -> Gene -> Disease", {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
        "n2": {"categories": ["biolink:Gene"]},
        "n3": {"categories": ["biolink:Disease"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
        "e1": {
            "subject": "n1",
            "object": "n2",
            "predicates": ["biolink:directly_physically_interacts_with"],
        },
        "e2": {
            "subject": "n2",
            "object": "n3",
            "predicates": ["biolink:gene_associated_with_condition"],
        },
    },
}, limit=10)


# =====================================================================
# 14. RAW JSON: print full TRAPI response for one query
# =====================================================================
# %% Query 14
print("=" * 70)
print("14. Full TRAPI JSON response (1-hop, limit=2)")
print("=" * 70)

qg = {
    "nodes": {
        "n0": {"ids": ["CHEBI:6801"]},
        "n1": {"categories": ["biolink:Gene"]},
    },
    "edges": {
        "e0": {
            "subject": "n0",
            "object": "n1",
            "predicates": ["biolink:affects"],
        },
    },
}
print(display_query_graph(qg))
print()
msg = query(graph, qg, limit=2)
print(json.dumps(msg, indent=2, default=str)[:3000])
print("...\n")

# %% Cleanup
graph.close()
