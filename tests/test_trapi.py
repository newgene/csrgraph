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
        nodes, edges = _linearise(qnodes, qedges)
        assert nodes == ["n0", "n1"]
        assert edges == ["e0"]

    def test_two_hop(self):
        from trapi import _linearise

        qnodes = {"n0": {"ids": ["CHEBI:1"]}, "n1": {}, "n2": {}}
        qedges = {
            "e0": {"subject": "n0", "object": "n1"},
            "e1": {"subject": "n1", "object": "n2"},
        }
        nodes, edges = _linearise(qnodes, qedges)
        assert nodes == ["n0", "n1", "n2"]
        assert edges == ["e0", "e1"]

    def test_pinned_node_chosen_as_start(self):
        """The node with ids should be chosen as start even if listed last."""
        from trapi import _linearise

        qnodes = {"n1": {}, "n0": {"ids": ["CHEBI:1"]}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        nodes, edges = _linearise(qnodes, qedges)
        assert nodes[0] == "n0"

    def test_disconnected_raises(self):
        from trapi import _linearise

        qnodes = {"n0": {}, "n1": {}, "n2": {}}
        qedges = {"e0": {"subject": "n0", "object": "n1"}}
        with pytest.raises(ValueError, match="Non-linear"):
            _linearise(qnodes, qedges)


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
