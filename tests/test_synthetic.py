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
