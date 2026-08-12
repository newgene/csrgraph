"""Self-contained tests that run anywhere — no external data, ES, or LMDB.

These build a tiny KGX archive and CSRGraph in a tmp dir and exercise the
code most likely to break across Python versions: KGX archive parsing,
pickle+zstd save/load round-trips, core path queries, and the SQLite
metadata backend (build + filtering). They complement the data-gated
integration tests in test_queries.py / test_trapi.py, which skip when the
proprietary DGIdb archive is absent.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csrgraph_kgx import CSRGraph  # noqa: E402
from metadata_db import SQLiteMetadataBackend  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NODES = [
    {"id": "CHEBI:1", "name": "drugA", "category": ["biolink:SmallMolecule"]},
    {"id": "NCBIGene:1", "name": "geneA", "category": ["biolink:Gene"]},
    {"id": "NCBIGene:2", "name": "geneB", "category": ["biolink:Gene"]},
    {"id": "MONDO:1", "name": "diseaseA", "category": ["biolink:Disease"]},
]
_EDGES = [
    {"subject": "CHEBI:1", "predicate": "biolink:affects", "object": "NCBIGene:1",
     "knowledge_level": "knowledge_assertion", "agent_type": "manual_agent"},
    {"subject": "CHEBI:1", "predicate": "biolink:affects", "object": "NCBIGene:2",
     "knowledge_level": "prediction", "agent_type": "automated_agent"},
    {"subject": "NCBIGene:1", "predicate": "biolink:gene_associated_with_condition",
     "object": "MONDO:1", "knowledge_level": "knowledge_assertion",
     "agent_type": "manual_agent"},
]


def _zstd_compress(data: bytes) -> bytes:
    """Compress with the stdlib zstd (>=3.14) or third-party zstandard."""
    try:
        import compression.zstd as _zstd  # type: ignore[import]
        return _zstd.compress(data)
    except ImportError:  # pragma: no cover - exercised only on Python < 3.14
        import zstandard
        return zstandard.ZstdCompressor().compress(data)


def _write_archive(path: Path) -> None:
    """Write a minimal KGX .tar.zst archive at *path*."""
    nb = ("\n".join(json.dumps(n) for n in _NODES)).encode()
    eb = ("\n".join(json.dumps(e) for e in _EDGES)).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in [("nodes.jsonl", nb), ("edges.jsonl", eb)]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    path.write_bytes(_zstd_compress(buf.getvalue()))


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    p = tmp_path / "kg.tar.zst"
    _write_archive(p)
    return p


# ---------------------------------------------------------------------------
# CSRGraph: construction from triples
# ---------------------------------------------------------------------------

def _triple_graph() -> CSRGraph:
    return CSRGraph([
        ("CHEBI:1", "affects", "NCBIGene:1"),
        ("CHEBI:1", "affects", "NCBIGene:2"),
        ("NCBIGene:1", "gene_associated_with_condition", "MONDO:1"),
    ])


def test_construct_topology():
    g = _triple_graph()
    assert g.num_nodes == 4
    assert g.edge_count == 3
    assert sorted(g.neighbors("CHEBI:1")) == ["NCBIGene:1", "NCBIGene:2"]


def test_shortest_path():
    g = _triple_graph()
    path = g.shortest_path("CHEBI:1", "MONDO:1")
    assert path is not None
    assert path[0][0] == "CHEBI:1"
    assert path[-1][-1] == "MONDO:1"
    assert len(path) == 2


def test_add_biolink_preserves_other_namespaces():
    # Both modules' _add_biolink must prefix only bare values, leaving
    # already-namespaced CURIEs (biolink: or rdfs:/owl:/...) unchanged.
    from csrgraph_kgx import _add_biolink as add_g
    from metadata_db import _add_biolink as add_db
    for add in (add_g, add_db):
        assert add("affects") == "biolink:affects"
        assert add("biolink:affects") == "biolink:affects"
        assert add("rdfs:subClassOf") == "rdfs:subClassOf"
        assert add("owl:sameAs") == "owl:sameAs"


def test_non_biolink_predicate_not_double_prefixed():
    # A non-biolink CURIE predicate (e.g. rdfs:subClassOf) must round-trip
    # unchanged, not become "biolink:rdfs:subClassOf".
    g = CSRGraph([
        ("CHEBI:1", "rdfs:subClassOf", "CHEBI:2"),
        ("CHEBI:1", "biolink:affects", "NCBIGene:1"),
    ])
    assert set(g.edges_between("CHEBI:1", "CHEBI:2")) == {"rdfs:subClassOf"}
    assert set(g.edges_between("CHEBI:1", "NCBIGene:1")) == {"biolink:affects"}
    # querying by the non-biolink predicate works
    sp = g.shortest_path("CHEBI:1", "CHEBI:2", relation="rdfs:subClassOf")
    assert sp == [("CHEBI:1", "rdfs:subClassOf", "CHEBI:2")]


def test_paths_by_predicate_sequence_is_simple():
    # Add a cycle; the sequence search must not loop forever / emit cycles.
    g = CSRGraph([
        ("A", "r", "B"),
        ("B", "r", "A"),
        ("B", "s", "C"),
    ])
    paths = g.paths_by_predicate_sequence("A", "C", ["r", "s"])
    assert paths == [[("A", "biolink:r", "B"), ("B", "biolink:s", "C")]]


# ---------------------------------------------------------------------------
# Serialization round-trip (pickle + zstd)
# ---------------------------------------------------------------------------

def test_save_load_roundtrip(tmp_path: Path):
    g = _triple_graph()
    fp = tmp_path / "g.pkl.zst"
    g.save(str(fp))
    assert fp.exists()

    g2 = CSRGraph.load(str(fp))
    assert g2.num_nodes == g.num_nodes
    assert g2.edge_count == g.edge_count
    assert sorted(g2.neighbors("CHEBI:1")) == sorted(g.neighbors("CHEBI:1"))
    assert g2.shortest_path("CHEBI:1", "MONDO:1") == g.shortest_path("CHEBI:1", "MONDO:1")


# ---------------------------------------------------------------------------
# KGX archive loading
# ---------------------------------------------------------------------------

def test_from_kgx_archive(archive: Path):
    g = CSRGraph.from_kgx_archive(
        str(archive),
        node_metadata_fields=["name", "category"],
    )
    assert g.num_nodes == 4
    assert g.edge_count == 3
    assert "affects" in g.csr_by_relation
    assert g.get_node_name("CHEBI:1") == "drugA"


# ---------------------------------------------------------------------------
# SQLite metadata backend: build + filtering (no external services)
# ---------------------------------------------------------------------------

def test_sqlite_backend_build_and_filter(archive: Path, tmp_path: Path):
    db = SQLiteMetadataBackend.build(
        str(archive),
        str(tmp_path / "kg.db"),
        node_metadata_fields=["name", "category"],
        edge_metadata_fields=["knowledge_level", "agent_type"],
    )
    try:
        assert db.get_node("CHEBI:1")["name"] == "drugA"

        genes = db.filter_nodes(
            ["CHEBI:1", "NCBIGene:1", "NCBIGene:2"], category="biolink:Gene"
        )
        assert {n["id"] for n in genes} == {"NCBIGene:1", "NCBIGene:2"}

        # knowledge_level filter should drop the predicted edge
        curated = db.filter_edges(
            [
                ("CHEBI:1", "biolink:affects", "NCBIGene:1"),
                ("CHEBI:1", "biolink:affects", "NCBIGene:2"),
            ],
            knowledge_level="knowledge_assertion",
        )
        assert len(curated) == 1
        assert curated[0]["object"] == "NCBIGene:1"
    finally:
        db.close()


def test_all_paths_depth_is_bounded_by_default():
    """all_paths must not default to an unbounded exponential DFS."""
    from csrgraph_kgx import DEFAULT_ALL_PATHS_MAX_DEPTH

    # A chain longer than the default bound: N:0 -> N:1 -> ... -> N:8
    chain = [(f"N:{i}", "biolink:related_to", f"N:{i+1}") for i in range(8)]
    g = CSRGraph(chain)

    # The only route is 8 hops, past the default of 5, so nothing is returned.
    assert g.all_paths("N:0", "N:8") == []
    assert DEFAULT_ALL_PATHS_MAX_DEPTH == 5

    # Within the bound it is found.
    assert len(g.all_paths("N:0", "N:5")) == 1

    # An explicit bound and explicit None both still work.
    assert len(g.all_paths("N:0", "N:8", max_depth=8)) == 1
    assert len(g.all_paths("N:0", "N:8", max_depth=None)) == 1


def test_sqlite_nodes_by_category(archive: Path, tmp_path: Path):
    """nodes_by_category answers from the index, agreeing with filter_nodes."""
    db = SQLiteMetadataBackend.build(
        str(archive),
        str(tmp_path / "cat.db"),
        node_metadata_fields=["name", "category"],
        edge_metadata_fields=["knowledge_level", "agent_type"],
    )
    try:
        genes = db.nodes_by_category("biolink:Gene")
        assert genes, "expected the synthetic archive to contain Gene nodes"

        # Must match what the candidate-list API reports for the same category.
        every_id = [n["id"] for n in db.filter_nodes(genes + ["CHEBI:1", "MONDO:1"])]
        via_filter = {
            n["id"] for n in db.filter_nodes(every_id, category="biolink:Gene")
        }
        assert set(genes) == via_filter

        # The prefix accepts a bare category too, and limit is honoured.
        assert set(db.nodes_by_category("Gene")) == set(genes)
        assert len(db.nodes_by_category("biolink:Gene", limit=1)) == 1
    finally:
        db.close()


def test_nodes_by_category_fallback_is_detectable():
    """A backend without a category index must signal, not silently full-scan."""
    from metadata_db import MetadataBackend

    class _NoIndexDB(MetadataBackend):
        def get_node(self, node_id):
            return {}

        def get_edge(self, subject, predicate, obj):
            return {}

        def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
            return []

        def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                         extra_filters=None):
            return []

        def close(self):
            pass

    with pytest.raises(NotImplementedError):
        _NoIndexDB().nodes_by_category("biolink:Gene")


def test_sqlite_backend_match_path(archive: Path, tmp_path: Path):
    g = CSRGraph.from_kgx_archive(str(archive), node_metadata_fields=["name", "category"])
    db = SQLiteMetadataBackend.build(
        str(archive),
        str(tmp_path / "kg.db"),
        node_metadata_fields=["name", "category"],
        edge_metadata_fields=["knowledge_level", "agent_type"],
    )
    try:
        g.set_db(db)
        # Drug -> Gene -> Disease
        paths = g.match_path(
            ["CHEBI:1", None, {"category": "biolink:Gene"}, None,
             {"category": "biolink:Disease"}],
            limit=10,
        )
        assert any(p[-1][-1] == "MONDO:1" for p in paths)
    finally:
        db.close()
