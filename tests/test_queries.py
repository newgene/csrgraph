"""Tests for CSRGraph query patterns, mapped from TRAPI QueryGraphDict examples.

QueryGraphDict source: BioPack retriever tier-0 dgraph tests
https://github.com/BioPack-team/retriever/tree/main/tests/data_tiers/tier_0/dgraph

Each test maps a TRAPI query pattern to the equivalent CSRGraph/MetadataBackend API call.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make csrgraph/ importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from metadata_db import SQLiteMetadataBackend

# ---------------------------------------------------------------------------
# Known data facts about dgidb.tar.zst
# ---------------------------------------------------------------------------
_ARCHIVE = Path("~/tmp/csrgraph_data").expanduser() / "dgidb.tar.zst"

# Known edges with qualifier metadata
_DRUG_1 = "CHEBI:78543"  # inhibitor
_GENE_1 = "NCBIGene:2260"  # EGFR
_DRUG_2 = "CHEBI:59750"  # agonist
_GENE_2 = "NCBIGene:2908"  # GRM1

# Sample nodes for generic tests
_DRUG_SAMPLES = ["CHEBI:100147", "CHEBI:10023", "CHEBI:100241"]
_GENE_SAMPLES = ["HGNC:1153", "HGNC:12029", "HGNC:12127"]


# ===========================================================================
# Session-scoped fixtures
# ===========================================================================


@pytest.fixture(scope="session")
def kg(tmp_path_factory):
    """Build a fresh SQLite metadata DB and CSRGraph from dgidb.tar.zst.

    Skips the entire module if the archive is not present.
    """
    if not _ARCHIVE.exists():
        pytest.skip(f"Test data not found: {_ARCHIVE}")

    from csrgraph_kgx import CSRGraph  # imported here so skip works correctly

    # Build metadata DB with qualifier fields indexed
    db_path = str(tmp_path_factory.mktemp("dgidb") / "dgidb.metadata.db")
    db = SQLiteMetadataBackend.build(
        str(_ARCHIVE),
        db_path,
        node_metadata_fields=["all"],
        edge_metadata_fields=["all"],
        indexed_extra_node_fields=[],
        indexed_extra_edge_fields=[
            "causal_mechanism_qualifier",
            "object_aspect_qualifier",
            "object_direction_qualifier",
        ],
    )

    # Build CSRGraph (topology only, no metadata in RAM)
    graph = CSRGraph.from_kgx_archive(str(_ARCHIVE))

    yield graph, db

    db.close()


# ===========================================================================
# Tests
# ===========================================================================


class TestSimpleEdgeQueries:
    """SIMPLE_QGRAPH and related two-node patterns."""

    def test_simple_two_node_known_predicate(self, kg):
        """SIMPLE_QGRAPH: given two known node IDs, verify an edge exists.

        TRAPI equivalent: n0 (CHEBI:78543) -[biolink:affects]-> n1 (NCBIGene:2260)
        """
        graph, _ = kg
        path = graph.shortest_path(_DRUG_1, _GENE_1, relation="biolink:affects")
        assert path is not None, (
            f"Expected a path from {_DRUG_1} to {_GENE_1} via affects"
        )
        assert len(path) == 1
        subj, pred, obj = path[0]
        assert subj == _DRUG_1
        assert obj == _GENE_1
        assert pred == "biolink:affects"

    def test_floating_object_neighbors_by_predicate(self, kg):
        """SIMPLE_REVERSE_QGRAPH / TRAPI_FLOATING_OBJECT_QUERY: one known node,
        find all neighbors via a specific predicate.

        TRAPI equivalent: n0 (CHEBI:78543) -[biolink:affects]-> n1 (floating)
        """
        graph, _ = kg
        nbrs = graph.neighbors(_DRUG_1, relation="biolink:affects")
        assert len(nbrs) > 0, f"{_DRUG_1} should have affects-neighbors"
        assert _GENE_1 in nbrs

    def test_multi_predicate_neighbors(self, kg):
        """PREDICATES_SINGLE_QGRAPH with multiple predicates: union of affects + interacts_with."""
        graph, _ = kg
        nbrs_affects = set(graph.neighbors(_DRUG_1, relation="biolink:affects"))
        nbrs_interacts = set(
            graph.neighbors(_DRUG_1, relation="biolink:interacts_with")
        )
        combined = nbrs_affects | nbrs_interacts
        assert len(combined) >= len(nbrs_affects), (
            "Union must be at least as large as one set"
        )
        assert len(combined) > 0


class TestCategoryFiltering:
    """CATEGORY_FILTER_QGRAPH patterns."""

    def test_category_filter_on_neighbors(self, kg):
        """CATEGORY_FILTER_QGRAPH: get neighbors, then filter to only Gene nodes."""
        graph, db = kg
        nbrs = graph.neighbors(_DRUG_1)
        assert len(nbrs) > 0
        genes = db.filter_nodes(nbrs, category="biolink:Gene")
        assert len(genes) > 0, "Should find Gene neighbors of the drug"
        for node in genes:
            cats = node.get("category", [])
            assert any("Gene" in c for c in cats), f"Expected Gene category, got {cats}"


class TestEdgeAttributeConstraints:
    """ATTRIBUTES_ONLY_QGRAPH: filter by built-in edge attributes."""

    def test_edge_attribute_constraint_knowledge_level(self, kg):
        """Filter edges by knowledge_level=knowledge_assertion."""
        graph, db = kg
        path = graph.shortest_path(_DRUG_1, _GENE_1, relation="biolink:affects")
        assert path is not None
        filtered = db.filter_edges(path, knowledge_level="knowledge_assertion")
        assert len(filtered) == len(path), (
            "All dgidb edges should have knowledge_level=knowledge_assertion"
        )

    def test_edge_attribute_constraint_agent_type(self, kg):
        """Filter edges by agent_type=automated_agent."""
        graph, db = kg
        path = graph.shortest_path(_DRUG_1, _GENE_1, relation="biolink:affects")
        assert path is not None
        filtered = db.filter_edges(path, agent_type="automated_agent")
        assert len(filtered) == len(path), (
            "All dgidb edges should have agent_type=automated_agent"
        )


class TestQualifierFilters:
    """QUALIFIER_SET_QGRAPH patterns: filter by extra qualifier fields."""

    def test_qualifier_filter_causal_mechanism(self, kg):
        """QUALIFIER_SET_QGRAPH: filter edges by causal_mechanism_qualifier=inhibition."""
        _, db = kg
        results = db.filter_edges(
            [(_DRUG_1, "biolink:affects", _GENE_1)],
            extra_filters={"causal_mechanism_qualifier": "inhibition"},
        )
        assert len(results) == 1, (
            f"Expected 1 edge with causal_mechanism_qualifier=inhibition, got {len(results)}"
        )
        assert results[0]["causal_mechanism_qualifier"] == "inhibition"

    def test_qualifier_filter_object_direction(self, kg):
        """QUALIFIER_SET_QGRAPH: filter by object_direction_qualifier=increased (agonist)."""
        _, db = kg
        results = db.filter_edges(
            [(_DRUG_2, "biolink:affects", _GENE_2)],
            extra_filters={"object_direction_qualifier": "increased"},
        )
        assert len(results) == 1, (
            f"Expected 1 edge with object_direction_qualifier=increased, got {len(results)}"
        )
        assert results[0]["object_direction_qualifier"] == "increased"

    def test_negated_qualifier_filter(self, kg):
        """Extra filter with a value that does not exist → empty result."""
        _, db = kg
        results = db.filter_edges(
            [(_DRUG_1, "biolink:affects", _GENE_1)],
            extra_filters={"causal_mechanism_qualifier": "nonexistent_qualifier_xyz"},
        )
        assert results == [], (
            "Filter on nonexistent qualifier value should return empty list"
        )


class TestPathQueries:
    """TWO_HOP_QGRAPH and path-based patterns.

    These tests use a small synthetic graph so they are not limited by the
    bipartite structure of real KGX exports (DGIdb, TTD) where leaf nodes
    have no outgoing edges.

    Synthetic graph topology:
        CHEBI:SYN001 --affects--> NCBIGene:SYN001 --affects--> MONDO:SYN001
        CHEBI:SYN001 --treats-->  MONDO:SYN001
        CHEBI:SYN002 --affects--> NCBIGene:SYN001

    TRAPI equivalent:
        TWO_HOP_QGRAPH: n0(drug) -[any]-> n1(gene) -[any]-> n2(disease)
    """

    # Synthetic node IDs
    _S_DRUG1 = "CHEBI:SYN001"
    _S_DRUG2 = "CHEBI:SYN002"
    _S_GENE1 = "NCBIGene:SYN001"
    _S_DIS1 = "MONDO:SYN001"

    @pytest.fixture(scope="class")
    def path_graph(self):
        """Tiny synthetic CSRGraph with relay nodes for multi-hop path testing."""
        from csrgraph_kgx import CSRGraph  # noqa: PLC0415

        triples = [
            (self._S_DRUG1, "biolink:affects", self._S_GENE1),
            (self._S_GENE1, "biolink:affects", self._S_DIS1),
            (self._S_DRUG1, "biolink:treats", self._S_DIS1),
            (self._S_DRUG2, "biolink:affects", self._S_GENE1),
        ]
        return CSRGraph(triples)

    def test_two_hop_path(self, path_graph):
        """TWO_HOP_QGRAPH: find a two-hop affects->affects path drug->gene->disease.

        TRAPI: n0 (CHEBI:SYN001) -[any]-> n1 (NCBIGene:SYN001) -[any]-> n2 (MONDO:SYN001)
        The shortest path via the relay gene is 2 hops; the direct treats edge is 1 hop,
        so all_shortest_paths returns only the 1-hop route — we use all_paths(max_depth=2)
        to enumerate both.
        """
        paths = path_graph.all_paths(self._S_DRUG1, self._S_DIS1, max_depth=2)
        assert len(paths) >= 2, "Should find both the direct and 2-hop paths"
        hop_lengths = {len(p) for p in paths}
        assert 2 in hop_lengths, "At least one 2-hop path must exist"

    def test_predicate_sequence_path(self, path_graph):
        """paths_by_predicate_sequence: match exact affects->affects hop sequence.

        TRAPI: n0 -[biolink:affects]-> n1 -[biolink:affects]-> n2
        """
        paths = path_graph.paths_by_predicate_sequence(
            self._S_DRUG1,
            self._S_DIS1,
            ["biolink:affects", "biolink:affects"],
        )
        assert len(paths) > 0, (
            "Should find the affects->affects path via the relay gene"
        )
        for path in paths:
            assert len(path) == 2
            assert path[0][1] == "biolink:affects"
            assert path[1][1] == "biolink:affects"
            assert path[0][0] == self._S_DRUG1
            assert path[0][2] == self._S_GENE1
            assert path[1][0] == self._S_GENE1
            assert path[1][2] == self._S_DIS1


class TestNodeSubclassing:
    """node_subclassing=True: queries expand source/target via subclass_of edges."""

    # Synthetic graph:
    #   PARENT --subclass_of edges-->  two children
    #   CHILD1 --affects-->            TARGET
    #   CHILD2 --treats-->             TARGET2
    _PARENT = "MONDO:PARENT"
    _CHILD1 = "MONDO:CHILD1"
    _CHILD2 = "MONDO:CHILD2"
    _TARGET = "NCBIGene:T1"
    _TARGET2 = "NCBIGene:T2"

    @pytest.fixture(scope="class")
    def sc_graph(self):
        from csrgraph_kgx import CSRGraph  # noqa: PLC0415

        triples = [
            # class hierarchy
            (self._CHILD1, "biolink:subclass_of", self._PARENT),
            (self._CHILD2, "biolink:subclass_of", self._PARENT),
            # relationships on children only (not on PARENT directly)
            (self._CHILD1, "biolink:affects", self._TARGET),
            (self._CHILD2, "biolink:treats", self._TARGET2),
        ]
        return CSRGraph(triples)

    def test_neighbors_without_subclassing(self, sc_graph):
        """Without subclassing, PARENT has no affects/treats neighbours."""
        nbrs = sc_graph.neighbors(self._PARENT)
        # Only subclass_of edge exists from PARENT's perspective (children point TO parent)
        assert self._TARGET not in nbrs
        assert self._TARGET2 not in nbrs

    def test_neighbors_with_subclassing(self, sc_graph):
        """With subclassing, PARENT's neighbours include children's neighbours."""
        nbrs = sc_graph.neighbors(self._PARENT, node_subclassing=True)
        assert self._TARGET in nbrs, "CHILD1's affects-target should appear"
        assert self._TARGET2 in nbrs, "CHILD2's treats-target should appear"

    def test_shortest_path_with_subclassing(self, sc_graph):
        """shortest_path finds CHILD1->TARGET when querying PARENT->TARGET."""
        path = sc_graph.shortest_path(self._PARENT, self._TARGET, node_subclassing=True)
        assert path is not None, "Should find a path via CHILD1"
        # The actual path starts at a child, not the abstract parent
        assert path[-1][2] == self._TARGET
        assert path[0][0] in (self._CHILD1, self._CHILD2, self._PARENT)

    def test_all_paths_with_subclassing(self, sc_graph):
        """all_paths with subclassing enumerates paths through subclass nodes."""
        paths = sc_graph.all_paths(self._PARENT, self._TARGET, node_subclassing=True)
        assert len(paths) > 0, "Should find at least one path via a child node"

    def test_predicate_sequence_with_subclassing(self, sc_graph):
        """paths_by_predicate_sequence expands source to subclasses."""
        paths = sc_graph.paths_by_predicate_sequence(
            self._PARENT,
            self._TARGET,
            ["biolink:affects"],
            node_subclassing=True,
        )
        assert len(paths) == 1
        assert paths[0][0][0] == self._CHILD1
        assert paths[0][0][2] == self._TARGET

    # ------------------------------------------------------------------
    # match_path tests
    #
    # match_path requires a MetadataBackend.  For the synthetic graph we
    # use a minimal stub that passes through all node/edge lists unchanged
    # (no real DB needed when NodeSpecs are string CURIEs or None).
    # ------------------------------------------------------------------

    @pytest.fixture(scope="class")
    def stub_db(self):
        """Minimal MetadataBackend stub for synthetic-graph match_path tests."""
        from metadata_db import MetadataBackend  # noqa: PLC0415

        class _StubDB(MetadataBackend):
            """Pass-through stub: filter_nodes/filter_edges return everything."""

            def get_node(self, nid):
                return {"id": nid}

            def get_edge(self, subject, predicate, obj):
                return {"subject": subject, "predicate": predicate, "object": obj}

            def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
                return [{"id": nid} for nid in node_ids]

            def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                             extra_filters=None):
                return [
                    {"subject": s, "predicate": p, "object": o}
                    for s, p, o in edges
                ]

            def close(self):
                pass

        return _StubDB()

    def test_match_path_without_subclassing(self, sc_graph, stub_db):
        """Without subclassing, querying PARENT directly finds no paths
        (edges are only on children)."""
        results = sc_graph.match_path(
            [self._PARENT, None, None],
            node_subclassing=False,
            db=stub_db,
        )
        # PARENT has only outgoing subclass_of edges; no affects/treats edges
        targets = {path[0][2] for path in results}
        assert self._TARGET  not in targets
        assert self._TARGET2 not in targets

    def test_match_path_wildcard_edge_with_subclassing(self, sc_graph, stub_db):
        """match_path with wildcard EdgeSpec returns all children's neighbors."""
        results = sc_graph.match_path(
            [self._PARENT, None, None],
            node_subclassing=True,
            db=stub_db,
        )
        targets = {path[0][2] for path in results}
        # CHILD1 --affects--> TARGET, CHILD2 --treats--> TARGET2
        # subclass_of edges also appear (CHILD1/CHILD2 --subclass_of--> PARENT)
        assert self._TARGET  in targets, "CHILD1's affects-target should appear"
        assert self._TARGET2 in targets, "CHILD2's treats-target should appear"

    def test_match_path_exact_predicate_with_subclassing(self, sc_graph, stub_db):
        """match_path with exact predicate string filters to only that predicate."""
        results = sc_graph.match_path(
            [self._PARENT, "biolink:affects", None],
            node_subclassing=True,
            db=stub_db,
        )
        assert len(results) == 1, "Only CHILD1 has an affects edge"
        assert results[0][0][0] == self._CHILD1
        assert results[0][0][2] == self._TARGET

    def test_match_path_exact_target_with_subclassing(self, sc_graph, stub_db):
        """match_path with exact target CURIE filters to only paths ending there."""
        results = sc_graph.match_path(
            [self._PARENT, None, self._TARGET],
            node_subclassing=True,
            db=stub_db,
        )
        assert len(results) == 1
        assert results[0][0][2] == self._TARGET
        assert results[0][0][0] == self._CHILD1

    def test_match_path_target_subclassing(self, sc_graph, stub_db):
        """node_subclassing also expands the target NodeSpec when it is a CURIE.

        CHILD1/CHILD2 are subclasses of PARENT; querying for paths that end at
        PARENT should also accept paths ending at CHILD1 or CHILD2.
        """
        results = sc_graph.match_path(
            [self._TARGET, None, self._PARENT],
            node_subclassing=True,
            db=stub_db,
        )
        # No direct edges from TARGET to PARENT or its subclasses in this graph
        # (graph is one-directional: children → parent via subclass_of,
        #  and children → targets via affects/treats)
        assert results == [], (
            "No edges from TARGET back toward PARENT subtree exist in this graph"
        )


class TestRdfsSubClassOfSubclassing:
    """Subclassing must also follow ``rdfs:subClassOf`` edges, not only the
    biolink ``subclass_of`` predicate.

    Ontology-derived graphs (e.g. translator_kg) encode the class hierarchy with
    the raw ``rdfs:subClassOf`` CURIE.  ``CSRGraph.SUBCLASS_PREDICATES`` lists
    every recognised variant and they are unioned, so ``node_subclassing=True``
    works regardless of which convention the source KG uses.
    """

    _PARENT = "MONDO:PARENT"
    _CHILD = "MONDO:CHILD"
    _TARGET = "NCBIGene:T1"

    @pytest.fixture(scope="class")
    def sc_graph(self):
        from csrgraph_kgx import CSRGraph  # noqa: PLC0415

        triples = [
            (self._CHILD, "rdfs:subClassOf", self._PARENT),
            (self._CHILD, "biolink:affects", self._TARGET),
        ]
        return CSRGraph(triples)

    def test_rdfs_subclass_predicate_recognised(self, sc_graph):
        assert "rdfs:subClassOf" in sc_graph.SUBCLASS_PREDICATES
        u = sc_graph.node_to_id[self._PARENT]
        expanded = sc_graph._expand_subclasses(u)
        assert sc_graph.node_to_id[self._CHILD] in expanded

    def test_neighbors_with_rdfs_subclassing(self, sc_graph):
        nbrs = sc_graph.neighbors(self._PARENT, node_subclassing=True)
        assert self._TARGET in nbrs
        # Without subclassing the abstract parent has no affects edge.
        assert self._TARGET not in sc_graph.neighbors(self._PARENT)


class TestSubclassPredicateMigration:
    """The hierarchy predicate is migrating from the (mistaken) ``rdfs:subClassOf``
    used in current translator_kg releases back to the canonical
    ``biolink:subclass_of``.  Subclassing must work for the current graph, the
    fixed future graph, and a mixed transition graph — all without code changes.
    """

    _PARENT = "MONDO:PARENT"
    _CHILD_RDFS = "MONDO:CHILD_RDFS"
    _CHILD_BIOLINK = "MONDO:CHILD_BIOLINK"
    _TARGET_RDFS = "NCBIGene:R"
    _TARGET_BIOLINK = "NCBIGene:B"

    def _graph(self, hierarchy_triples):
        from csrgraph_kgx import CSRGraph  # noqa: PLC0415

        return CSRGraph(hierarchy_triples + [
            (self._CHILD_RDFS, "biolink:affects", self._TARGET_RDFS),
            (self._CHILD_BIOLINK, "biolink:affects", self._TARGET_BIOLINK),
        ])

    def test_future_biolink_only(self):
        """Fixed future graph: only ``biolink:subclass_of`` edges."""
        g = self._graph([
            (self._CHILD_BIOLINK, "biolink:subclass_of", self._PARENT),
        ])
        nbrs = g.neighbors(self._PARENT, node_subclassing=True)
        assert self._TARGET_BIOLINK in nbrs

    def test_transition_mixed(self):
        """Mixed graph during migration: both predicates present and unioned."""
        g = self._graph([
            (self._CHILD_RDFS, "rdfs:subClassOf", self._PARENT),
            (self._CHILD_BIOLINK, "biolink:subclass_of", self._PARENT),
        ])
        expanded = g._expand_subclasses(g.node_to_id[self._PARENT])
        assert g.node_to_id[self._CHILD_RDFS] in expanded
        assert g.node_to_id[self._CHILD_BIOLINK] in expanded
        nbrs = g.neighbors(self._PARENT, node_subclassing=True)
        assert {self._TARGET_RDFS, self._TARGET_BIOLINK} <= set(nbrs)


class TestMatchPath:
    """match_path() with NodeSpec / EdgeSpec patterns."""

    def test_match_path_category_pattern(self, kg):
        """match_path with category NodeSpec: SmallMolecule -[affects]-> Gene.

        TRAPI: n0 {category: SmallMolecule} -[biolink:affects]-> n1 {category: Gene}
        """
        graph, db = kg
        results = graph.match_path(
            [
                {"category": "biolink:SmallMolecule"},
                "biolink:affects",
                {"category": "biolink:Gene"},
            ],
            limit=10,
            db=db,
        )
        assert len(results) > 0, (
            "match_path should find SmallMolecule-affects-Gene paths"
        )
        for path in results:
            assert len(path) == 1, "Each result should be a 1-hop path"
            _, pred, _ = path[0]
            assert pred == "biolink:affects"


class TestMatchPathTruncation:
    """match_path must report when a hop cap makes the result a subset."""

    _SRC = "CHEBI:SRC"
    _TARGETS = [f"NCBIGene:T{i}" for i in range(6)]

    @pytest.fixture(scope="class")
    def graph(self):
        from csrgraph_kgx import CSRGraph  # noqa: PLC0415

        triples = [(self._SRC, "biolink:affects", t) for t in self._TARGETS]
        return CSRGraph(triples)

    @pytest.fixture(scope="class")
    def stub_db(self):
        from metadata_db import MetadataBackend  # noqa: PLC0415

        class _StubDB(MetadataBackend):
            def get_node(self, node_id):
                return {"id": node_id}

            def get_edge(self, subject, predicate, obj):
                return {"subject": subject, "predicate": predicate, "object": obj}

            def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
                return [{"id": nid} for nid in node_ids]

            def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                             extra_filters=None):
                return [
                    {"subject": s, "predicate": p, "object": o} for s, p, o in edges
                ]

            def close(self):
                pass

        return _StubDB()

    _SPEC = [_SRC, "biolink:affects", None]

    def test_complete_result_not_flagged(self, graph, stub_db):
        paths, stats = graph.match_path(
            self._SPEC, limit=100, db=stub_db, return_stats=True
        )
        assert len(paths) == len(self._TARGETS)
        assert stats.truncated is False
        assert stats.truncated_hops == []
        assert stats.frontier_sizes == [len(self._TARGETS)]

    def test_capped_result_is_flagged(self, graph, stub_db):
        paths, stats = graph.match_path(
            self._SPEC, limit=2, db=stub_db, return_stats=True
        )
        assert len(paths) == 2
        assert stats.truncated is True
        assert stats.truncated_hops == [0]
        assert stats.hop_caps == [2]

    def test_truncation_is_logged_without_return_stats(self, graph, stub_db, caplog):
        """Existing callers get the signal without opting in to the new return shape."""
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING, logger="csrgraph_kgx"):
            paths = graph.match_path(self._SPEC, limit=2, db=stub_db)
        assert isinstance(paths, list) and len(paths) == 2
        assert "truncated" in caplog.text

    def test_no_warning_when_complete(self, graph, stub_db, caplog):
        import logging  # noqa: PLC0415

        with caplog.at_level(logging.WARNING, logger="csrgraph_kgx"):
            graph.match_path(self._SPEC, limit=100, db=stub_db)
        assert "truncated" not in caplog.text


class TestMetadataLookups:
    """Direct metadata retrieval tests."""

    def test_node_get_metadata(self, kg):
        """Verify get_node returns expected fields for a known drug."""
        _, db = kg
        node = db.get_node(_DRUG_1)
        assert node, f"Node {_DRUG_1} should be in the DB"
        assert node.get("id") == _DRUG_1
        # DGIdb drugs have categories
        assert "category" in node, "Node should have a category field"

    def test_edge_get_metadata(self, kg):
        """Verify get_edge returns qualifier fields for a known edge."""
        _, db = kg
        edge = db.get_edge(_DRUG_1, "biolink:affects", _GENE_1)
        assert edge, f"Edge {_DRUG_1} -> {_GENE_1} should be in the DB"
        assert edge.get("subject") == _DRUG_1
        assert edge.get("object") == _GENE_1
        assert edge.get("predicate") == "biolink:affects"
        # This edge should have qualifier metadata
        assert "causal_mechanism_qualifier" in edge, (
            f"Expected causal_mechanism_qualifier in edge metadata, got: {list(edge.keys())}"
        )
        assert edge["causal_mechanism_qualifier"] == "inhibition"
        assert "object_aspect_qualifier" in edge
        assert "object_direction_qualifier" in edge
