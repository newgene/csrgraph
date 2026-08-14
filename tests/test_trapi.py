"""Tests for TRAPI QueryGraph support (trapi.py)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from metadata_db import MetadataBackend


# ---------------------------------------------------------------------------
# Stub backend for synthetic tests
# ---------------------------------------------------------------------------

class _StubDB(MetadataBackend):
    """Pass-through stub: returns minimal metadata for any node/edge."""

    def __init__(self, node_meta=None, edge_meta=None):
        self._node_meta = node_meta or {}
        self._edge_meta = edge_meta or {}

    def get_node(self, nid):
        return self._node_meta.get(nid, {"id": nid})

    def get_edge(self, subject, predicate, obj):
        key = (subject, predicate, obj)
        return self._edge_meta.get(key, {
            "subject": subject, "predicate": predicate, "object": obj,
        })

    def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
        results = []
        for nid in node_ids:
            meta = self._node_meta.get(nid, {"id": nid})
            if category:
                cats = meta.get("category", [])
                if isinstance(cats, str):
                    cats = [cats]
                if category not in cats:
                    continue
            results.append(meta)
        return results

    def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                     extra_filters=None):
        return [
            {"subject": s, "predicate": p, "object": o}
            for s, p, o in edges
        ]

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Synthetic graph fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def simple_graph():
    """A→B→C graph with metadata backend for TRAPI tests."""
    from csrgraph_kgx import CSRGraph

    triples = [
        ("CHEBI:1", "biolink:affects", "HGNC:1"),
        ("CHEBI:1", "biolink:affects", "HGNC:2"),
        ("HGNC:1", "biolink:gene_associated_with_condition", "MONDO:1"),
        ("HGNC:2", "biolink:gene_associated_with_condition", "MONDO:2"),
    ]
    node_meta = {
        "CHEBI:1": {"id": "CHEBI:1", "name": "DrugA", "category": ["biolink:SmallMolecule"]},
        "HGNC:1": {"id": "HGNC:1", "name": "GeneA", "category": ["biolink:Gene"]},
        "HGNC:2": {"id": "HGNC:2", "name": "GeneB", "category": ["biolink:Gene"]},
        "MONDO:1": {"id": "MONDO:1", "name": "DiseaseA", "category": ["biolink:Disease"]},
        "MONDO:2": {"id": "MONDO:2", "name": "DiseaseB", "category": ["biolink:Disease"]},
    }
    db = _StubDB(node_meta=node_meta)
    graph = CSRGraph(triples)
    graph.set_db(db)
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLinearise:
    """Test QueryGraph linearisation."""

    def test_one_hop(self):
        from trapi import _linearise

        qnodes = {"n0": {"ids": ["CHEBI:1"]}, "n1": {}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        nodes, edges, dirs = _linearise(qnodes, qedges)
        assert nodes == ["n0", "n1"]
        assert edges == ["e0"]
        assert dirs == [True]        # walked subject -> object

    def test_two_hop(self):
        from trapi import _linearise

        qnodes = {"n0": {"ids": ["CHEBI:1"]}, "n1": {}, "n2": {}}
        qedges = {
            "e0": {"subject": "n0", "object": "n1"},
            "e1": {"subject": "n1", "object": "n2"},
        }
        nodes, edges, dirs = _linearise(qnodes, qedges)
        assert nodes == ["n0", "n1", "n2"]
        assert edges == ["e0", "e1"]
        assert dirs == [True, True]

    def test_pinned_node_chosen_as_start(self):
        """The node with ids should be chosen as start even if listed last."""
        from trapi import _linearise

        qnodes = {"n1": {}, "n0": {"ids": ["CHEBI:1"]}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        nodes, edges, _dirs = _linearise(qnodes, qedges)
        assert nodes[0] == "n0"

    def test_object_anchored_chain_is_marked_reverse(self):
        """Pinning the object means hop 0 is walked against the edge.

        This flag used to be computed and then dropped, which made every
        "what treats disease X?" query return nothing.
        """
        from trapi import _linearise

        qnodes = {"n0": {"categories": ["biolink:ChemicalEntity"]},
                  "n1": {"ids": ["MONDO:1"]}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        nodes, edges, dirs = _linearise(qnodes, qedges)
        assert nodes == ["n1", "n0"]   # starts from the pinned object
        assert edges == ["e0"]
        assert dirs == [False]         # traversed object -> subject

    def test_disconnected_raises(self):
        from trapi import _linearise

        qnodes = {"n0": {}, "n1": {}, "n2": {}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        with pytest.raises(ValueError, match="Non-linear"):
            _linearise(qnodes, qedges)


class TestEdgeOrientation:
    """Queries anchored at the edge's object end must still find answers.

    "What chemicals treat disease X?" pins the object and leaves the subject open.
    That shape silently returned zero results for every query until the per-hop
    traversal direction was carried through to match_path, so it is guarded here
    directly rather than only via the corpus (which needs a real graph).
    """

    def test_open_subject_pinned_object_finds_answers(self, simple_graph):
        from trapi import query

        # simple_graph has CHEBI:1 -[affects]-> HGNC:1, so ask it backwards.
        qg = {
            "nodes": {
                "n0": {"categories": ["biolink:SmallMolecule"]},   # open subject
                "n1": {"ids": ["HGNC:1"]},                          # pinned object
            },
            "edges": {"e0": {"subject": "n0", "object": "n1",
                             "predicates": ["biolink:affects"]}},
        }
        msg = query(simple_graph, qg)
        assert msg["results"], "object-anchored query must not return empty"

        for r in msg["results"]:
            assert r["node_bindings"]["n1"][0]["id"] == "HGNC:1"
            assert r["node_bindings"]["n0"][0]["id"].startswith("CHEBI:")

        # Knowledge-graph edges must keep true orientation, not the walk order.
        for edge in msg["knowledge_graph"]["edges"].values():
            assert edge["subject"].startswith("CHEBI:")
            assert edge["object"] == "HGNC:1"

    def test_both_orientations_agree_on_the_same_edge(self, simple_graph):
        """Forward and reverse anchoring of one edge must both find it."""
        from trapi import query

        fwd = query(simple_graph, {
            "nodes": {"n0": {"ids": ["CHEBI:1"]}, "n1": {"ids": ["HGNC:1"]}},
            "edges": {"e0": {"subject": "n0", "object": "n1",
                             "predicates": ["biolink:affects"]}},
        })
        rev = query(simple_graph, {
            "nodes": {"n0": {"categories": ["biolink:SmallMolecule"]},
                      "n1": {"ids": ["HGNC:1"]}},
            "edges": {"e0": {"subject": "n0", "object": "n1",
                             "predicates": ["biolink:affects"]}},
        })
        assert fwd["results"], "both-pinned should find the edge"
        pairs_fwd = {(e["subject"], e["object"])
                     for e in fwd["knowledge_graph"]["edges"].values()}
        pairs_rev = {(e["subject"], e["object"])
                     for e in rev["knowledge_graph"]["edges"].values()}
        assert pairs_fwd <= pairs_rev


class TestMultiPredicateIsDisjunction:
    """Listing more predicates must never shrink the answer set.

    Multiple predicates used to become a wildcard EdgeSpec that was post-filtered
    *after* the result cap, so the cap filled with whichever predicates came first
    and a selective one could be squeezed out entirely. The predicate set is now
    pushed into traversal.
    """

    @pytest.fixture(scope="class")
    def pred_graph(self):
        from csrgraph_kgx import CSRGraph

        # 'noise' outnumbers 'wanted', so a cap applied before filtering would
        # drop every 'wanted' edge.
        triples = [("N:S", "biolink:noise", f"N:n{i}") for i in range(30)]
        triples += [("N:S", "biolink:wanted", f"N:w{i}") for i in range(3)]
        g = CSRGraph(triples)
        g.set_db(_StubDB())
        return g

    def _count(self, g, preds, limit):
        from trapi import query

        qg = {"nodes": {"n0": {"ids": ["N:S"]}, "n1": {}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": preds}}}
        return len(query(g, qg, limit=limit)["results"])

    def test_selective_predicate_survives_a_small_cap(self, pred_graph):
        assert self._count(pred_graph, ["biolink:wanted"], 5) == 3

    def test_adding_a_predicate_never_reduces_results(self, pred_graph):
        only_wanted = self._count(pred_graph, ["biolink:wanted"], 5)
        with_noise = self._count(pred_graph, ["biolink:wanted", "biolink:noise"], 5)
        assert with_noise >= only_wanted

    def test_disjunction_is_the_union(self, pred_graph):
        big = 500
        w = self._count(pred_graph, ["biolink:wanted"], big)
        n = self._count(pred_graph, ["biolink:noise"], big)
        both = self._count(pred_graph, ["biolink:wanted", "biolink:noise"], big)
        assert (w, n, both) == (3, 30, 33)


class TestBiolinkExpander:
    """Predicate/qualifier widening, driven by an explicit map (no BMT needed)."""

    def test_predicates_widen_to_descendants(self):
        from trapi import BiolinkExpander, expand_query_graph

        exp = BiolinkExpander(predicates={
            "biolink:treats": frozenset({"biolink:treats", "biolink:applied_to_treat"})
        })
        qg = {"nodes": {}, "edges": {"e0": {"subject": "n0", "object": "n1",
                                            "predicates": ["biolink:treats"]}}}
        out = expand_query_graph(qg, exp)
        assert set(out["edges"]["e0"]["predicates"]) == {
            "biolink:treats", "biolink:applied_to_treat"
        }
        assert qg["edges"]["e0"]["predicates"] == ["biolink:treats"], "input untouched"

    def test_unknown_predicate_is_left_alone(self):
        from trapi import BiolinkExpander, expand_query_graph

        exp = BiolinkExpander(predicates={"biolink:treats": frozenset({"x"})})
        qg = {"nodes": {}, "edges": {"e0": {"subject": "n0", "object": "n1",
                                            "predicates": ["biolink:affects"]}}}
        out = expand_query_graph(qg, exp)
        assert out["edges"]["e0"]["predicates"] == ["biolink:affects"]

    def test_qualifier_sets_become_alternatives(self):
        """One constraint with a widened value becomes several sets.

        TRAPI treats multiple qualifier_constraints as alternatives, so the
        cross-product preserves the meaning without touching the match logic.
        """
        from trapi import BiolinkExpander, expand_query_graph

        exp = BiolinkExpander(qualifier_values={
            "activity": frozenset({"activity", "activity_or_abundance"})
        })
        qg = {"nodes": {}, "edges": {"e0": {
            "subject": "n0", "object": "n1",
            "qualifier_constraints": [{"qualifier_set": [
                {"qualifier_type_id": "biolink:object_aspect_qualifier",
                 "qualifier_value": "activity"},
                {"qualifier_type_id": "biolink:object_direction_qualifier",
                 "qualifier_value": "increased"},
            ]}]}}}
        out = expand_query_graph(qg, exp)
        sets = out["edges"]["e0"]["qualifier_constraints"]
        assert len(sets) == 2
        aspects = {q["qualifier_value"] for s in sets for q in s["qualifier_set"]
                   if q["qualifier_type_id"].endswith("object_aspect_qualifier")}
        assert aspects == {"activity", "activity_or_abundance"}
        directions = {q["qualifier_value"] for s in sets for q in s["qualifier_set"]
                      if q["qualifier_type_id"].endswith("object_direction_qualifier")}
        assert directions == {"increased"}, "un-widened value stays fixed"

    def test_combination_cap_leaves_constraint_untouched(self):
        from trapi import BiolinkExpander, expand_query_graph, _MAX_QUALIFIER_COMBINATIONS

        big = frozenset(f"v{i}" for i in range(_MAX_QUALIFIER_COMBINATIONS + 5))
        exp = BiolinkExpander(qualifier_values={"a": big, "b": big})
        qc = {"qualifier_set": [
            {"qualifier_type_id": "biolink:object_aspect_qualifier", "qualifier_value": "a"},
            {"qualifier_type_id": "biolink:object_direction_qualifier", "qualifier_value": "b"},
        ]}
        qg = {"nodes": {}, "edges": {"e0": {"subject": "n0", "object": "n1",
                                            "qualifier_constraints": [qc]}}}
        out = expand_query_graph(qg, exp)
        assert out["edges"]["e0"]["qualifier_constraints"] == [qc]

    def test_no_expander_means_literal_matching(self, simple_graph):
        """The default path must be unchanged."""
        from trapi import query

        qg = {"nodes": {"n0": {"ids": ["CHEBI:1"]},
                        "n1": {"categories": ["biolink:Gene"]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:affects"]}}}
        assert len(query(simple_graph, qg)["results"]) == 2


class TestQueryGraphValidation:
    """Structurally invalid query graphs must raise ValueError, not KeyError."""

    def test_edge_referencing_missing_node(self, simple_graph):
        from trapi import query

        qg = {"nodes": {"n0": {"ids": ["CHEBI:1"]}},
              "edges": {"e0": {"subject": "n0", "object": "n_missing"}}}
        with pytest.raises(ValueError, match="not a node in the query graph"):
            query(simple_graph, qg)

    def test_edge_missing_an_end(self, simple_graph):
        from trapi import query

        qg = {"nodes": {"n0": {}}, "edges": {"e0": {"subject": "n0"}}}
        with pytest.raises(ValueError, match="missing 'object'"):
            query(simple_graph, qg)

    def test_pathfinder_paths_graph_is_reported_clearly(self, simple_graph):
        from trapi import query

        qg = {"nodes": {"n0": {"ids": ["CHEBI:1"]}, "n1": {"ids": ["HGNC:1"]}},
              "paths": {"p0": {"subject": "n0", "object": "n1"}}}
        with pytest.raises(ValueError, match="Pathfinder"):
            query(simple_graph, qg)

    def test_missing_nodes_key(self, simple_graph):
        from trapi import query

        with pytest.raises(ValueError, match="nodes must be an object"):
            query(simple_graph, {"edges": {}})


class TestOneHopQuery:
    """One-hop TRAPI queries."""

    def test_one_hop_with_predicate(self, simple_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
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
        msg = query(simple_graph, qg)

        assert "results" in msg
        assert len(msg["results"]) == 2  # HGNC:1 and HGNC:2

        # Check result structure.
        for r in msg["results"]:
            assert "n0" in r["node_bindings"]
            assert "n1" in r["node_bindings"]
            assert r["node_bindings"]["n0"][0]["id"] == "CHEBI:1"
            assert r["node_bindings"]["n1"][0]["id"] in ("HGNC:1", "HGNC:2")
            assert len(r["analyses"]) == 1
            assert "e0" in r["analyses"][0]["edge_bindings"]

    def test_one_hop_no_predicate(self, simple_graph):
        """Wildcard predicate should return all neighbors."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(simple_graph, qg)
        assert len(msg["results"]) == 2

    def test_one_hop_no_results(self, simple_graph):
        """Query for a non-existent node returns empty results."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["FAKE:999"]},
                "n1": {},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(simple_graph, qg)
        assert len(msg["results"]) == 0


class TestTwoHopQuery:
    """Two-hop TRAPI queries."""

    def test_two_hop_drug_gene_disease(self, simple_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
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
        }
        msg = query(simple_graph, qg)
        assert len(msg["results"]) == 2  # Two paths: via HGNC:1 and HGNC:2

        # Verify KG nodes contain all traversed nodes.
        kg_node_ids = set(msg["knowledge_graph"]["nodes"].keys())
        assert "CHEBI:1" in kg_node_ids
        # At least one gene and one disease.
        assert kg_node_ids & {"HGNC:1", "HGNC:2"}
        assert kg_node_ids & {"MONDO:1", "MONDO:2"}

        # Verify KG edges exist.
        assert len(msg["knowledge_graph"]["edges"]) > 0


class TestKnowledgeGraph:
    """Verify KnowledgeGraph node/edge structure in response."""

    def test_kg_node_structure(self, simple_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(simple_graph, qg)
        node = msg["knowledge_graph"]["nodes"]["CHEBI:1"]
        assert "categories" in node
        assert "attributes" in node
        assert node["name"] == "DrugA"

    def test_kg_edge_structure(self, simple_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:affects"],
                },
            },
        }
        msg = query(simple_graph, qg)
        edges = msg["knowledge_graph"]["edges"]
        assert len(edges) > 0
        for edge_id, edge in edges.items():
            assert "subject" in edge
            assert "predicate" in edge
            assert "object" in edge
            assert "sources" in edge
            assert edge["sources"][0]["resource_role"] == "primary_knowledge_source"


class TestQualifierConstraints:
    """Test qualifier-based post-filtering."""

    @pytest.fixture(scope="class")
    def qual_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:1", "biolink:affects", "HGNC:2"),
        ]
        edge_meta = {
            ("CHEBI:1", "biolink:affects", "HGNC:1"): {
                "subject": "CHEBI:1",
                "predicate": "biolink:affects",
                "object": "HGNC:1",
                "object_aspect_qualifier": "expression",
                "object_direction_qualifier": "decreased",
            },
            ("CHEBI:1", "biolink:affects", "HGNC:2"): {
                "subject": "CHEBI:1",
                "predicate": "biolink:affects",
                "object": "HGNC:2",
                "object_aspect_qualifier": "activity",
            },
        }
        db = _StubDB(edge_meta=edge_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_qualifier_filters_results(self, qual_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
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
                                "qualifier_value": "expression",
                            },
                            {
                                "qualifier_type_id": "biolink:object_direction_qualifier",
                                "qualifier_value": "decreased",
                            },
                        ],
                    }],
                },
            },
        }
        msg = query(qual_graph, qg)
        # Only HGNC:1 matches both qualifiers.
        assert len(msg["results"]) == 1
        assert msg["results"][0]["node_bindings"]["n1"][0]["id"] == "HGNC:1"

    def test_qualifier_or_logic(self, qual_graph):
        """Multiple qualifier_constraints are ORed."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:affects"],
                    "qualifier_constraints": [
                        {
                            "qualifier_set": [{
                                "qualifier_type_id": "biolink:object_aspect_qualifier",
                                "qualifier_value": "expression",
                            }],
                        },
                        {
                            "qualifier_set": [{
                                "qualifier_type_id": "biolink:object_aspect_qualifier",
                                "qualifier_value": "activity",
                            }],
                        },
                    ],
                },
            },
        }
        msg = query(qual_graph, qg)
        # Both HGNC:1 (expression) and HGNC:2 (activity) match.
        assert len(msg["results"]) == 2


class TestMultipleIDs:
    """Multiple IDs per node (BATCH expansion)."""

    @pytest.fixture(scope="class")
    def multi_id_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:2", "biolink:affects", "HGNC:2"),
            ("CHEBI:3", "biolink:affects", "HGNC:3"),
        ]
        db = _StubDB()
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_batch_multiple_start_ids(self, multi_id_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1", "CHEBI:2"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:affects"],
                },
            },
        }
        msg = query(multi_id_graph, qg)
        result_sources = {r["node_bindings"]["n0"][0]["id"] for r in msg["results"]}
        assert "CHEBI:1" in result_sources
        assert "CHEBI:2" in result_sources
        assert len(msg["results"]) == 2


class TestMultiplePredicates:
    """Multiple predicates per edge (OR logic)."""

    @pytest.fixture(scope="class")
    def multi_pred_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:1", "biolink:treats", "HGNC:2"),
            ("CHEBI:1", "biolink:causes", "HGNC:3"),
        ]
        db = _StubDB()
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_or_across_predicates(self, multi_pred_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:affects", "biolink:treats"],
                },
            },
        }
        msg = query(multi_pred_graph, qg)
        targets = {r["node_bindings"]["n1"][0]["id"] for r in msg["results"]}
        assert "HGNC:1" in targets  # affects
        assert "HGNC:2" in targets  # treats
        assert "HGNC:3" not in targets  # causes — not in predicate list
        assert len(msg["results"]) == 2


class TestNodeConstraints:
    """Node constraints (matches, ==, >, not)."""

    @pytest.fixture(scope="class")
    def constrained_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:1", "biolink:affects", "HGNC:2"),
        ]
        node_meta = {
            "CHEBI:1": {"id": "CHEBI:1", "name": "DrugA", "category": ["biolink:SmallMolecule"]},
            "HGNC:1": {"id": "HGNC:1", "name": "diphenylmethane kinase", "category": ["biolink:Gene"]},
            "HGNC:2": {"id": "HGNC:2", "name": "laxative receptor", "category": ["biolink:Gene"]},
        }
        db = _StubDB(node_meta=node_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_regex_constraint(self, constrained_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {
                    "categories": ["biolink:Gene"],
                    "constraints": [{
                        "id": "name", "name": "name",
                        "operator": "matches", "value": "/.*diphenylmethane.*/i",
                        "not": False,
                    }],
                },
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(constrained_graph, qg)
        assert len(msg["results"]) == 1
        assert msg["results"][0]["node_bindings"]["n1"][0]["id"] == "HGNC:1"

    def test_negated_constraint(self, constrained_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {
                    "constraints": [{
                        "id": "name", "name": "name",
                        "operator": "matches", "value": "/.*diphenylmethane.*/i",
                        "not": True,
                    }],
                },
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(constrained_graph, qg)
        assert len(msg["results"]) == 1
        assert msg["results"][0]["node_bindings"]["n1"][0]["id"] == "HGNC:2"


class TestEdgeAttributeConstraints:
    """Edge attribute_constraints beyond == (>, not)."""

    @pytest.fixture(scope="class")
    def attr_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:1", "biolink:affects", "HGNC:2"),
        ]
        edge_meta = {
            ("CHEBI:1", "biolink:affects", "HGNC:1"): {
                "subject": "CHEBI:1", "predicate": "biolink:affects", "object": "HGNC:1",
                "knowledge_level": "knowledge_assertion", "score": "150",
            },
            ("CHEBI:1", "biolink:affects", "HGNC:2"): {
                "subject": "CHEBI:1", "predicate": "biolink:affects", "object": "HGNC:2",
                "knowledge_level": "prediction", "score": "50",
            },
        }
        db = _StubDB(edge_meta=edge_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_gt_constraint(self, attr_graph):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0", "object": "n1",
                    "predicates": ["biolink:affects"],
                    "attribute_constraints": [{
                        "id": "score", "name": "score",
                        "operator": ">", "value": "100", "not": False,
                    }],
                },
            },
        }
        msg = query(attr_graph, qg)
        assert len(msg["results"]) == 1
        assert msg["results"][0]["node_bindings"]["n1"][0]["id"] == "HGNC:1"

    def test_negated_gt_constraint(self, attr_graph):
        """not > 100 means <= 100."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0", "object": "n1",
                    "predicates": ["biolink:affects"],
                    "attribute_constraints": [{
                        "id": "score", "name": "score",
                        "operator": ">", "value": "100", "not": True,
                    }],
                },
            },
        }
        msg = query(attr_graph, qg)
        assert len(msg["results"]) == 1
        assert msg["results"][0]["node_bindings"]["n1"][0]["id"] == "HGNC:2"


class TestSymmetricPredicates:
    """Symmetric predicate bidirectional search."""

    @pytest.fixture(scope="class")
    def sym_graph(self):
        from csrgraph_kgx import CSRGraph

        triples = [
            # Only A→B edge exists in data; B→A should also match for symmetric predicates.
            ("HGNC:1", "biolink:interacts_with", "HGNC:2"),
            # Non-symmetric edge for comparison.
            ("HGNC:1", "biolink:causes", "MONDO:1"),
        ]
        node_meta = {
            "HGNC:1": {"id": "HGNC:1", "name": "GeneA", "category": ["biolink:Gene"]},
            "HGNC:2": {"id": "HGNC:2", "name": "GeneB", "category": ["biolink:Gene"]},
            "MONDO:1": {"id": "MONDO:1", "name": "DiseaseA", "category": ["biolink:Disease"]},
        }
        db = _StubDB(node_meta=node_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_symmetric_finds_reverse(self, sym_graph):
        """Querying B→A with symmetric predicate should find the A→B edge."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["HGNC:2"]},  # B
                "n1": {"categories": ["biolink:Gene"]},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:interacts_with"],
                },
            },
        }
        msg = query(sym_graph, qg)
        targets = {r["node_bindings"]["n1"][0]["id"] for r in msg["results"]}
        assert "HGNC:1" in targets

    def test_non_symmetric_no_reverse(self, sym_graph):
        """Non-symmetric predicate should NOT find reverse."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["MONDO:1"]},  # Disease
                "n1": {},
            },
            "edges": {
                "e0": {
                    "subject": "n0",
                    "object": "n1",
                    "predicates": ["biolink:causes"],
                },
            },
        }
        msg = query(sym_graph, qg)
        # MONDO:1 has no outgoing causes edges; HGNC:1→MONDO:1 is causes
        # but causes is not symmetric, so reverse shouldn't be found.
        assert len(msg["results"]) == 0


class TestBranchingQuery:
    """Branching (fork) query graph — general matcher."""

    @pytest.fixture(scope="class")
    def fork_graph(self):
        """A→B, A→C (fork from A to two different targets)."""
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("CHEBI:1", "biolink:treats", "MONDO:1"),
        ]
        node_meta = {
            "CHEBI:1": {"id": "CHEBI:1", "name": "DrugA", "category": ["biolink:SmallMolecule"]},
            "HGNC:1": {"id": "HGNC:1", "name": "GeneA", "category": ["biolink:Gene"]},
            "MONDO:1": {"id": "MONDO:1", "name": "DiseaseA", "category": ["biolink:Disease"]},
        }
        db = _StubDB(node_meta=node_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_fork_query(self, fork_graph):
        """Query: n0(Drug) → n1(Gene), n0(Drug) → n2(Disease)."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
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
                    "predicates": ["biolink:treats"],
                },
            },
        }
        msg = query(fork_graph, qg)
        assert len(msg["results"]) == 1
        r = msg["results"][0]
        assert r["node_bindings"]["n0"][0]["id"] == "CHEBI:1"
        assert r["node_bindings"]["n1"][0]["id"] == "HGNC:1"
        assert r["node_bindings"]["n2"][0]["id"] == "MONDO:1"
        assert "e0" in r["analyses"][0]["edge_bindings"]
        assert "e1" in r["analyses"][0]["edge_bindings"]


class TestCyclicQuery:
    """Cyclic (triangle) query graph — general matcher."""

    @pytest.fixture(scope="class")
    def triangle_graph(self):
        """A→B, B→C, C→A (triangle)."""
        from csrgraph_kgx import CSRGraph

        triples = [
            ("CHEBI:1", "biolink:affects", "HGNC:1"),
            ("HGNC:1", "biolink:gene_associated_with_condition", "MONDO:1"),
            ("MONDO:1", "biolink:treated_by", "CHEBI:1"),
        ]
        node_meta = {
            "CHEBI:1": {"id": "CHEBI:1", "name": "DrugA", "category": ["biolink:SmallMolecule"]},
            "HGNC:1": {"id": "HGNC:1", "name": "GeneA", "category": ["biolink:Gene"]},
            "MONDO:1": {"id": "MONDO:1", "name": "DiseaseA", "category": ["biolink:Disease"]},
        }
        db = _StubDB(node_meta=node_meta)
        graph = CSRGraph(triples)
        graph.set_db(db)
        return graph

    def test_triangle_query(self, triangle_graph):
        """Query: n0(Drug) → n1(Gene) → n2(Disease) → n0(Drug)."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
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
                    "subject": "n2",
                    "object": "n0",
                    "predicates": ["biolink:treated_by"],
                },
            },
        }
        msg = query(triangle_graph, qg)
        assert len(msg["results"]) == 1
        r = msg["results"][0]
        assert r["node_bindings"]["n0"][0]["id"] == "CHEBI:1"
        assert r["node_bindings"]["n1"][0]["id"] == "HGNC:1"
        assert r["node_bindings"]["n2"][0]["id"] == "MONDO:1"
        # All three edges should be bound.
        ebs = r["analyses"][0]["edge_bindings"]
        assert "e0" in ebs
        assert "e1" in ebs
        assert "e2" in ebs

    def test_triangle_no_closing_edge(self, triangle_graph):
        """Triangle query with wrong closing predicate returns no results."""
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:1"]},
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
                    "subject": "n2",
                    "object": "n0",
                    "predicates": ["biolink:causes"],  # doesn't exist
                },
            },
        }
        msg = query(triangle_graph, qg)
        assert len(msg["results"]) == 0


class TestDGIdb:
    """Integration tests against the DGIdb KGX archive."""

    _ARCHIVE = Path("~/tmp/csrgraph_data").expanduser() / "dgidb.tar.zst"

    @pytest.fixture(scope="class")
    def dgidb(self, tmp_path_factory):
        if not self._ARCHIVE.exists():
            pytest.skip(f"Test data not found: {self._ARCHIVE}")

        from csrgraph_kgx import CSRGraph
        from metadata_db import SQLiteMetadataBackend

        db_path = str(tmp_path_factory.mktemp("dgidb_trapi") / "dgidb.metadata.db")
        db = SQLiteMetadataBackend.build(
            str(self._ARCHIVE),
            db_path,
            node_metadata_fields=["all"],
            edge_metadata_fields=["all"],
        )
        graph = CSRGraph.from_kgx_archive(str(self._ARCHIVE))
        graph.set_db(db)
        yield graph
        db.close()

    def test_one_hop_drug_to_gene(self, dgidb):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:78543"]},
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
        msg = query(dgidb, qg)
        assert len(msg["results"]) > 0
        # NCBIGene:2260 (EGFR) should be among results.
        result_genes = {
            r["node_bindings"]["n1"][0]["id"] for r in msg["results"]
        }
        assert "NCBIGene:2260" in result_genes

    def test_no_predicate_returns_all_neighbors(self, dgidb):
        from trapi import query

        qg = {
            "nodes": {
                "n0": {"ids": ["CHEBI:78543"]},
                "n1": {},
            },
            "edges": {
                "e0": {"subject": "n0", "object": "n1"},
            },
        }
        msg = query(dgidb, qg)
        assert len(msg["results"]) > 0


class TestSubclassDepth:
    """Subclass expansion is on by default at depth 1, with a transitive opt-in."""

    @pytest.fixture(scope="class")
    def chain_graph(self):
        from csrgraph_kgx import CSRGraph

        # D:leaf -> D:mid -> D:root, plus a chemical treating each level.
        triples = [
            ("D:mid", "biolink:subclass_of", "D:root"),
            ("D:leaf", "biolink:subclass_of", "D:mid"),
            ("C:root", "biolink:treats", "D:root"),
            ("C:mid", "biolink:treats", "D:mid"),
            ("C:leaf", "biolink:treats", "D:leaf"),
        ]
        g = CSRGraph(triples)
        g.set_db(_StubDB())
        return g

    def _answers(self, g, **kw):
        from trapi import query

        qg = {"nodes": {"n0": {}, "n1": {"ids": ["D:root"]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:treats"]}}}
        msg = query(g, qg, limit=100, **kw)
        return {b["id"] for r in msg["results"]
                for b in r["node_bindings"].get("n0", [])}

    def test_default_is_depth_one(self, chain_graph):
        """Direct children only — matches gandalf's subclass_depth=1."""
        assert self._answers(chain_graph) == {"C:root", "C:mid"}

    def test_transitive_opt_in(self, chain_graph):
        assert self._answers(chain_graph, subclass_depth=None) == {
            "C:root", "C:mid", "C:leaf"
        }

    def test_disabled(self, chain_graph):
        assert self._answers(chain_graph, node_subclassing=False) == {"C:root"}

    def test_explicit_depth_two(self, chain_graph):
        assert self._answers(chain_graph, subclass_depth=2) == {
            "C:root", "C:mid", "C:leaf"
        }


class TestSubclassBindingConformance:
    """Subclass-expanded bindings must honour categories and declare query_id.

    ``match_path`` expands a pinned node to its ``subclass_of`` descendants and
    binds the descendant. Real Translator data asserts ``HP:x subclass_of
    MONDO:y``, so a disease-constrained query could bind a phenotype — an answer
    contradicting the query — and nothing said the bound CURIE was a stand-in for
    the queried one.
    """

    @pytest.fixture(scope="class")
    def graph(self):
        from csrgraph_kgx import CSRGraph

        # D:sub is a Disease subtype; P:sub is a PhenotypicFeature asserted as a
        # subclass of the same disease, mirroring the MONDO/HP shape in the KG.
        triples = [
            ("D:sub", "biolink:subclass_of", "D:root"),
            ("P:sub", "biolink:subclass_of", "D:root"),
            ("C:root", "biolink:treats", "D:root"),
            ("C:sub", "biolink:treats", "D:sub"),
            ("C:phen", "biolink:treats", "P:sub"),
        ]
        g = CSRGraph(triples)
        g.set_db(_StubDB(node_meta={
            "D:root": {"id": "D:root", "category": ["biolink:Disease"]},
            "D:sub": {"id": "D:sub", "category": ["biolink:Disease"]},
            "P:sub": {"id": "P:sub", "category": ["biolink:PhenotypicFeature"]},
            "C:root": {"id": "C:root", "category": ["biolink:ChemicalEntity"]},
            "C:sub": {"id": "C:sub", "category": ["biolink:ChemicalEntity"]},
            "C:phen": {"id": "C:phen", "category": ["biolink:ChemicalEntity"]},
        }))
        return g

    def _run(self, g, categories):
        from trapi import query

        n1 = {"ids": ["D:root"]}
        if categories:
            n1["categories"] = categories
        qg = {"nodes": {"n0": {}, "n1": n1},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:treats"]}}}
        msg = query(g, qg, limit=100)
        return {b["id"]: b.get("query_id")
                for r in msg["results"] for b in r["node_bindings"]["n1"]}

    def test_expanded_node_must_satisfy_queried_category(self, graph):
        """The phenotype subclass is dropped when the query asks for Disease."""
        bound = self._run(graph, ["biolink:Disease"])
        assert "P:sub" not in bound, "a PhenotypicFeature answers a Disease query"
        assert set(bound) == {"D:root", "D:sub"}

    def test_query_id_marks_expanded_nodes(self, graph):
        bound = self._run(graph, ["biolink:Disease"])
        # The descendant declares the CURIE it stands in for...
        assert bound["D:sub"] == "D:root"
        # ...and a direct hit does not, so clients can tell them apart.
        assert bound["D:root"] is None

    def test_no_category_constraint_keeps_every_subclass(self, graph):
        """Without a categories constraint there is nothing to violate."""
        bound = self._run(graph, None)
        assert set(bound) == {"D:root", "D:sub", "P:sub"}
        assert bound["P:sub"] == "D:root"

    def test_disabling_subclassing_needs_no_query_id(self, graph):
        from trapi import query

        qg = {"nodes": {"n0": {}, "n1": {"ids": ["D:root"],
                                        "categories": ["biolink:Disease"]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:treats"]}}}
        msg = query(g_ := graph, qg, limit=100, node_subclassing=False)
        bindings = [b for r in msg["results"] for b in r["node_bindings"]["n1"]]
        assert {b["id"] for b in bindings} == {"D:root"}
        assert all("query_id" not in b for b in bindings)


# ---------------------------------------------------------------------------
# Determinism under truncation
# ---------------------------------------------------------------------------

_DETERMINISM_SCRIPT = """
import sys
sys.path.insert(0, %r)
from csrgraph_kgx import CSRGraph
from metadata_db import MetadataBackend
import trapi


class _DB(MetadataBackend):
    def get_node(self, nid):
        return {"id": nid, "category": ["biolink:ChemicalEntity"]}

    def get_edge(self, s, p, o):
        return {"subject": s, "predicate": p, "object": o}

    def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
        return [self.get_node(n) for n in node_ids]

    def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                     extra_filters=None):
        return [{"subject": s, "predicate": p, "object": o} for s, p, o in edges]

    def close(self):
        pass


# 60 candidates behind a *symmetric* predicate, which routes the query to
# _general_match rather than match_path, with limit=5 so truncation bites.
triples = [("C:%%02d" %% i, "biolink:associated_with", "D:1") for i in range(60)]
g = CSRGraph(triples)
g.set_db(_DB())
qg = {"nodes": {"n0": {"categories": ["biolink:ChemicalEntity"]},
                "n1": {"ids": ["D:1"]}},
      "edges": {"e0": {"subject": "n0", "object": "n1",
                       "predicates": ["biolink:associated_with"]}}}
msg = trapi.query(g, qg, limit=5)
kept = [b["id"] for r in msg["results"] for b in r["node_bindings"]["n0"]]
print(",".join(kept))
print("TRUNCATED" if msg.get("logs") else "COMPLETE")
"""


def _run_with_hashseed(seed: str) -> tuple[str, str]:
    """Run the query in a subprocess with a fixed PYTHONHASHSEED."""
    import os
    import subprocess

    root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run(
        [sys.executable, "-c", _DETERMINISM_SCRIPT % root],
        capture_output=True, text=True, env=env, check=True,
    ).stdout.splitlines()
    return out[0], out[1]


class TestTruncationDeterminism:
    """A truncated result must not depend on the process's hash seed.

    ``_general_match`` collected candidate CURIEs into a ``set[str]`` and
    iterated it. Python randomises ``str.__hash__`` per process, so the
    exploration order — and therefore *which* candidates survived the ``limit``
    — changed on every run. Measured on the real graph, one query returned three
    different genes across three runs. ``match_path`` was unaffected because it
    works on ``int`` node indices, whose hash is identity.
    """

    def test_same_answers_across_hash_seeds(self):
        first, _ = _run_with_hashseed("1")
        second, _ = _run_with_hashseed("2")
        third, _ = _run_with_hashseed("12345")
        assert first == second == third, (
            f"truncated result varies with PYTHONHASHSEED: "
            f"{first} / {second} / {third}"
        )

    def test_truncation_is_reported_in_logs(self):
        _, status = _run_with_hashseed("1")
        assert status == "TRUNCATED", (
            "a capped result set must say so in message.logs, or a client "
            "cannot tell a partial answer from a complete one"
        )


class TestTruncationLogs:
    """message.logs appears only when the answer really is a subset."""

    @pytest.fixture(scope="class")
    def graph(self):
        from csrgraph_kgx import CSRGraph

        g = CSRGraph([("C:1", "biolink:treats", "D:1"),
                      ("C:2", "biolink:treats", "D:1"),
                      ("C:3", "biolink:treats", "D:1")])
        g.set_db(_StubDB())
        return g

    def _msg(self, g, limit):
        from trapi import query

        qg = {"nodes": {"n0": {}, "n1": {"ids": ["D:1"]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:treats"]}}}
        return query(g, qg, limit=limit)

    def test_no_logs_when_complete(self, graph):
        msg = self._msg(graph, 100)
        assert len(msg["results"]) == 3
        assert "logs" not in msg

    def test_logs_when_capped(self, graph):
        msg = self._msg(graph, 2)
        assert len(msg["results"]) == 2
        assert msg["logs"][0]["code"] == "ResultsTruncated"
        assert msg["logs"][0]["level"] == "WARNING"


class TestSymmetricHops:
    """Symmetric predicates are matched on the vectorized path, both ways.

    Biolink declares 39 predicates symmetric, and for those an assertion stored
    one way answers a query posed the other way. These queries used to divert to
    ``_general_match``, which costs an order of magnitude (26.7 s against 0.33 s
    on the HelmsDeep two_hop_lookup shape). ``match_path`` now takes a hop
    direction of ``None`` and walks both ways.
    """

    @pytest.fixture(scope="class")
    def graph(self):
        from csrgraph_kgx import CSRGraph

        # Only ONE stored orientation for each pair, so a query posed the other
        # way can only succeed if both directions are walked.
        return CSRGraph([
            ("C:1", "biolink:interacts_with", "C:2"),
            ("C:3", "biolink:interacts_with", "C:1"),
            ("C:1", "biolink:treats", "D:1"),
        ])

    def _ask(self, g, subj, obj, predicate="biolink:interacts_with"):
        from trapi import query

        g.set_db(_StubDB())
        qg = {"nodes": {"n0": {"ids": [subj]}, "n1": {"ids": [obj]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": [predicate]}}}
        msg = query(g, qg, limit=10, node_subclassing=False)
        edges = msg["knowledge_graph"]["edges"]
        return len(msg["results"]), [
            (e["subject"], e["object"]) for e in edges.values()
        ]

    def test_matches_in_stored_direction(self, graph):
        n, orient = self._ask(graph, "C:1", "C:2")
        assert n == 1 and orient == [("C:1", "C:2")]

    def test_matches_against_stored_direction(self, graph):
        """The whole point: C:3 -> C:1 answers a query for C:1 -> C:3."""
        n, orient = self._ask(graph, "C:1", "C:3")
        assert n == 1, "symmetric predicate not matched against its stored direction"
        # The knowledge graph must still report the edge as it is stored.
        assert orient == [("C:3", "C:1")]

    def test_asymmetric_predicate_still_one_way(self, graph):
        """Only symmetric predicates get this treatment."""
        assert self._ask(graph, "C:1", "D:1", "biolink:treats")[0] == 1
        assert self._ask(graph, "D:1", "C:1", "biolink:treats")[0] == 0

    def test_extra_spec_unions_both_directions(self, graph):
        """An extra spec adds the opposite-direction walk to the primary one."""
        graph.set_db(_StubDB())
        spec = ["C:1", "biolink:interacts_with", None]
        fwd = graph.match_path(spec, limit=10, hop_directions=[True])
        rev = graph.match_path(spec, limit=10, hop_directions=[False])
        both = graph.match_path(
            spec, limit=10, hop_directions=[True],
            hop_extra_specs=[("biolink:interacts_with",)],
        )
        assert {p[0] for p in fwd} == {("C:1", "biolink:interacts_with", "C:2")}
        assert {p[0] for p in rev} == {("C:3", "biolink:interacts_with", "C:1")}
        assert {p[0] for p in both} == {p[0] for p in fwd} | {p[0] for p in rev}

    def test_extra_spec_covers_only_the_predicates_it_names(self, graph):
        """A one-way predicate is not dragged along by a symmetric sibling.

        The hop lists both, but only interacts_with may be walked backwards;
        matching treats in reverse would assert that a disease treats a drug.
        """
        from trapi import query

        graph.set_db(_StubDB())
        qg = {"nodes": {"n0": {"ids": ["D:1"]}, "n1": {"ids": ["C:1"]}},
              "edges": {"e0": {"subject": "n0", "object": "n1",
                               "predicates": ["biolink:treats",
                                              "biolink:interacts_with"]}}}
        msg = query(graph, qg, limit=10, node_subclassing=False)
        assert len(msg["results"]) == 0, (
            "a one-way treats edge matched backwards because a symmetric "
            "predicate shared the hop"
        )

    def test_both_orientations_stored_yields_one_path(self):
        """A pair asserted both ways under one symmetric predicate is one answer."""
        from csrgraph_kgx import CSRGraph

        g = CSRGraph([
            ("C:1", "biolink:interacts_with", "C:2"),
            ("C:2", "biolink:interacts_with", "C:1"),
        ])
        g.set_db(_StubDB())
        paths = g.match_path(["C:1", "biolink:interacts_with", None],
                             limit=10, hop_directions=[True],
                             hop_extra_specs=[("biolink:interacts_with",)])
        assert len(paths) == 1, f"duplicate assertion emitted twice: {paths}"
        # Forward is walked first, so its orientation is the one kept.
        assert paths[0][0] == ("C:1", "biolink:interacts_with", "C:2")


class TestGeneralMatchSymmetryIsPerPredicate:
    """Candidate gathering must not treat a whole hop as symmetric.

    ``_get_edge_neighbors`` searched the opposite direction for *every* queried
    predicate whenever one of them was symmetric, so a hop listing
    ``[treats, interacts_with]`` proposed nodes reachable only by walking a
    one-way ``treats`` backwards. ``_matching_predicates`` rejected them at
    verification, so this cost work rather than answers — but it is the same
    per-hop-versus-per-predicate confusion that *did* produce wrong answers in
    ``match_path``.
    """

    @pytest.fixture(scope="class")
    def graph(self):
        from csrgraph_kgx import CSRGraph

        g = CSRGraph([
            ("C:1", "biolink:treats", "D:1"),          # one-way
            ("C:1", "biolink:interacts_with", "C:2"),  # symmetric
        ])
        g.set_db(_StubDB())
        return g

    @staticmethod
    def _qedge(*predicates):
        return {"subject": "n0", "object": "n1", "predicates": list(predicates)}

    def test_asymmetric_predicate_is_not_searched_backwards(self, graph):
        from trapi import _get_edge_neighbors

        # D:1 is the *object* of the only treats edge, so searching forward from
        # it must find nothing however the hop's other predicates behave.
        assert _get_edge_neighbors(
            graph, self._qedge("biolink:treats"), "D:1", True) == []
        assert _get_edge_neighbors(
            graph, self._qedge("biolink:treats", "biolink:interacts_with"),
            "D:1", True) == [], "one-way predicate searched backwards"

    def test_symmetric_predicate_is_still_searched_backwards(self, graph):
        from trapi import _get_edge_neighbors

        # C:2 is the object of the interacts_with edge; symmetry must still
        # reach C:1, on its own and alongside an asymmetric sibling.
        assert _get_edge_neighbors(
            graph, self._qedge("biolink:interacts_with"), "C:2", True) == ["C:1"]
        assert _get_edge_neighbors(
            graph, self._qedge("biolink:treats", "biolink:interacts_with"),
            "C:2", True) == ["C:1"]

    def test_general_match_agrees(self, graph):
        """End to end, the verifier and the gatherer reach the same verdict."""
        from trapi import _general_match

        qnodes = {"n0": {"ids": ["D:1"]}, "n1": {"ids": ["C:1"]}}
        bindings, _ = _general_match(
            graph, qnodes,
            {"e0": self._qedge("biolink:treats", "biolink:interacts_with")}, 10,
        )
        assert bindings == []
