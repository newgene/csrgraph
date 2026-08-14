"""F1/F2 release packaging — data-free, using a tiny synthetic KGX archive.

Covers the properties a consumer depends on: the release appears atomically or
not at all, the manifest describes it accurately, and the three edge counts stay
distinct from one another.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import make_release  # noqa: E402
from metadata_db import STORE_FORMAT_VERSION  # noqa: E402
from tests.test_synthetic import _zstd_compress  # noqa: E402

# One triple asserted twice with different qualifiers, plus a second triple.
# So: 3 records, 2 distinct triples, 3 variants — the three counts the manifest
# keeps separate, all different from each other.
_NODES = [
    {"id": "CHEBI:V", "name": "DrugV", "category": ["biolink:SmallMolecule"]},
    {"id": "NCBIGene:V", "name": "GeneV", "category": ["biolink:Gene"]},
]
_EDGES = [
    {"subject": "CHEBI:V", "predicate": "biolink:affects", "object": "NCBIGene:V",
     "object_direction_qualifier": "increased"},
    {"subject": "CHEBI:V", "predicate": "biolink:affects", "object": "NCBIGene:V",
     "object_direction_qualifier": "decreased"},
    {"subject": "CHEBI:V", "predicate": "biolink:treats", "object": "NCBIGene:V"},
]


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    nb = ("\n".join(json.dumps(n) for n in _NODES)).encode()
    eb = ("\n".join(json.dumps(e) for e in _EDGES)).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in [("nodes.jsonl", nb), ("edges.jsonl", eb)]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    p = tmp_path / "kg.tar.zst"
    p.write_bytes(_zstd_compress(buf.getvalue()))
    return p


def _run(archive: Path, out_root: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(ROOT / "make_release.py"), str(archive),
         "--version", "v1", "--graph-name", "kg", "--out-root", str(out_root),
         *extra],
        capture_output=True, text=True, cwd=str(ROOT),
    )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_tree_hash_is_stable(tmp_path: Path):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(b"one")
    (d / "sub" / "b.bin").write_bytes(b"two")
    assert make_release._sha256_tree(d) == make_release._sha256_tree(d)


def test_tree_hash_covers_layout_not_just_bytes(tmp_path: Path):
    """Renaming a file must change the digest.

    Without the path in the digest, two trees holding the same file *contents*
    under different names would hash identically, and a release with a misplaced
    artifact would verify clean.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    (a / "x.bin").write_bytes(b"same")
    (b / "y.bin").write_bytes(b"same")
    assert make_release._sha256_tree(a)[1] == make_release._sha256_tree(b)[1]  # same bytes
    assert make_release._sha256_tree(a)[0] != make_release._sha256_tree(b)[0]  # different digest


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_release_is_published_with_an_accurate_manifest(archive: Path, tmp_path: Path):
    out = tmp_path / "releases"
    r = _run(archive, out)
    assert r.returncode == 0, r.stderr

    rel = out / "v1"
    manifest = json.loads((rel / "manifest.json").read_text())

    assert manifest["graph_name"] == "kg"
    assert manifest["version"] == "v1"
    assert manifest["store_format_version"] == STORE_FORMAT_VERSION

    # The three counts are genuinely different, and each means what it says.
    assert manifest["node_count"] == 2
    assert manifest["edge_count"] == 2, "distinct (subject, predicate, object)"
    assert manifest["source_record_count"] == 3, "raw records in the archive"
    assert manifest["variant_count"] == 3, "distinct (s, p, o, qualifier fingerprint)"

    # Every artifact named in the manifest exists, with the recorded size.
    for spec in manifest["artifacts"].values():
        path = rel / spec["path"]
        assert path.exists(), spec["path"]
        if path.is_file():
            assert path.stat().st_size == spec["bytes"]


def test_manifest_hashes_match_the_published_artifacts(archive: Path, tmp_path: Path):
    out = tmp_path / "releases"
    assert _run(archive, out).returncode == 0
    rel = out / "v1"
    manifest = json.loads((rel / "manifest.json").read_text())

    pkl = manifest["artifacts"]["pkl_zst"]
    assert make_release._sha256_file(rel / pkl["path"])[0] == pkl["sha256"]
    lmdb = manifest["artifacts"]["lmdb"]
    assert make_release._sha256_tree(rel / lmdb["path"])[0] == lmdb["sha256_tree"]


def test_existing_release_is_not_clobbered(archive: Path, tmp_path: Path):
    out = tmp_path / "releases"
    assert _run(archive, out).returncode == 0
    before = (out / "v1" / "manifest.json").read_text()

    second = _run(archive, out)
    assert second.returncode != 0
    assert "already exists" in second.stdout + second.stderr
    assert (out / "v1" / "manifest.json").read_text() == before

    assert _run(archive, out, "--force").returncode == 0
    assert (out / "v1" / "manifest.json").exists()


def test_failed_build_leaves_nothing_behind(archive: Path, tmp_path: Path):
    """A release exists completely or not at all.

    The staging directory matters because LMDBMetadataBackend.build() rmtree's
    its target: a build that wrote directly into the release path would destroy
    the previous release before knowing it could produce a new one.
    """
    out = tmp_path / "releases"
    out.mkdir()
    r = _run(tmp_path / "does-not-exist.tar.zst", out)
    assert r.returncode != 0
    assert list(out.iterdir()) == [], "staging or partial release left behind"


def test_no_memmap_omits_it_from_the_manifest(archive: Path, tmp_path: Path):
    out = tmp_path / "releases"
    assert _run(archive, out, "--no-memmap").returncode == 0
    manifest = json.loads((out / "v1" / "manifest.json").read_text())
    assert "memmap" not in manifest["artifacts"]
    assert not (out / "v1" / "kg.csrgraph.memmap").exists()


def test_published_release_is_loadable(archive: Path, tmp_path: Path):
    """The point of the whole exercise: the output actually serves."""
    from csrgraph_kgx import CSRGraph
    from metadata_db import LMDBMetadataBackend

    out = tmp_path / "releases"
    assert _run(archive, out).returncode == 0
    rel = out / "v1"
    manifest = json.loads((rel / "manifest.json").read_text())

    graph = CSRGraph.load(str(rel / manifest["artifacts"]["pkl_zst"]["path"]))
    assert graph.num_nodes == manifest["node_count"]

    db = LMDBMetadataBackend(str(rel / manifest["artifacts"]["lmdb"]["path"]))
    try:
        variants = db.get_edge_variants("CHEBI:V", "biolink:affects", "NCBIGene:V")
        assert len(variants) == 2, "both qualifier variants survived packaging"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# F3 — the server's view of a release
# ---------------------------------------------------------------------------

def _publish(archive: Path, tmp_path: Path) -> Path:
    out = tmp_path / "releases"
    assert _run(archive, out).returncode == 0
    return out / "v1"


def test_store_format_mismatch_refuses_to_serve(archive: Path, tmp_path: Path):
    """The whole reason the manifest carries a format version.

    A version-1 store read by version-2 code matches nothing, so the server would
    otherwise start healthy and answer every qualifier-constrained query with an
    empty result. Refusing to start is the desired behaviour.
    """
    import trapi_server

    rel = _publish(archive, tmp_path)
    manifest = json.loads((rel / "manifest.json").read_text())
    manifest["store_format_version"] = STORE_FORMAT_VERSION + 1
    (rel / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(trapi_server.StoreFormatMismatch) as exc:
        trapi_server._read_manifest(rel)
    # The message must name both versions; an operator needs to know which side
    # to move.
    assert str(STORE_FORMAT_VERSION) in str(exc.value)
    assert str(STORE_FORMAT_VERSION + 1) in str(exc.value)


def test_matching_release_is_accepted(archive: Path, tmp_path: Path):
    import trapi_server

    rel = _publish(archive, tmp_path)
    manifest = trapi_server._read_manifest(rel)
    assert manifest is not None
    assert manifest["store_format_version"] == STORE_FORMAT_VERSION


def test_unversioned_directory_is_allowed(tmp_path: Path):
    """Hand-built directories predate releases and must keep working."""
    import trapi_server

    assert trapi_server._read_manifest(tmp_path) is None


def test_version_endpoint_reports_the_release(archive: Path, tmp_path: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import trapi_server

    rel = _publish(archive, tmp_path)
    # Point the app at the release the way a deployment would.
    import os
    os.environ.update(DATA_DIR=str(rel), GRAPH_NAME="kg", NO_ES="1")
    trapi_server._graph = None
    trapi_server._manifest = None
    trapi_server._DEFAULT_DATA_DIR = rel  # type: ignore[attr-defined]
    try:
        with TestClient(trapi_server.app) as c:
            for route in ("/version", "/health"):
                r = c.get(route)
                assert r.status_code == 200, route
                d = r.json()
                assert d["ready"] is True
                assert d["versioned_release"] is True
                assert d["graph_name"] == "kg"
                assert d["version"] == "v1"
                assert d["store_format_version"] == STORE_FORMAT_VERSION
                assert d["node_count"] == 2
                assert d["variant_count"] == 3
                assert d["loaded"]["nodes"] == 2
    finally:
        if trapi_server._db is not None:
            trapi_server._db.close()
        trapi_server._graph = None
        trapi_server._db = None
        trapi_server._manifest = None


# ---------------------------------------------------------------------------
# F4 — read-only store access
# ---------------------------------------------------------------------------

def test_readonly_open_writes_nothing(archive: Path, tmp_path: Path):
    """A serving process must not mutate the release directory it was handed.

    Opening read-write creates ``lock.mdb`` inside the store, which also makes a
    read-only mount unusable — the shared-volume deployment this exists for.
    """
    from metadata_db import LMDBMetadataBackend

    rel = _publish(archive, tmp_path)
    store = rel / "kg.metadata.lmdb"
    (store / "lock.mdb").unlink(missing_ok=True)
    before = sorted(p.name for p in store.iterdir())

    db = LMDBMetadataBackend(str(store), readonly=True)
    try:
        assert db.readonly is True
        # Every read path must work through handles opened with create=False.
        assert len(db.get_edge_variants("CHEBI:V", "biolink:affects", "NCBIGene:V")) == 2
        assert db.get_node("CHEBI:V")["name"] == "DrugV"
        assert db.filter_nodes(["CHEBI:V"], category="biolink:SmallMolecule")
        assert db.nodes_by_category("biolink:Gene", limit=5)
    finally:
        db.close()

    assert sorted(p.name for p in store.iterdir()) == before, "read-only open wrote a file"


def test_readonly_open_works_on_a_read_only_directory(archive: Path, tmp_path: Path):
    """The deployment case: a ReadOnlyMany volume shared across pods."""
    import stat

    from metadata_db import LMDBMetadataBackend

    rel = _publish(archive, tmp_path)
    store = rel / "kg.metadata.lmdb"
    (store / "lock.mdb").unlink(missing_ok=True)
    mode = store.stat().st_mode
    store.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    try:
        db = LMDBMetadataBackend(str(store), readonly=True)
        try:
            assert len(db.get_edge_variants("CHEBI:V", "biolink:affects", "NCBIGene:V")) == 2
        finally:
            db.close()
    finally:
        store.chmod(mode)


# ---------------------------------------------------------------------------
# F5 — release gates
# ---------------------------------------------------------------------------

def test_completeness_gate_accepts_correct_counts(archive: Path):
    """The archive is recounted independently and must agree with the stores."""
    make_release._gate_completeness(archive, distinct=2, variants=3)


def test_completeness_gate_catches_a_dropped_variant(archive: Path):
    """The regression this gate exists for.

    A store keyed without the qualifier fingerprint collapses the two `affects`
    assertions into one, so it holds 2 variants where the source has 3.
    """
    with pytest.raises(SystemExit) as exc:
        make_release._gate_completeness(archive, distinct=2, variants=2)
    assert "dropping assertions" in str(exc.value)


def test_completeness_gate_catches_a_short_graph(archive: Path):
    with pytest.raises(SystemExit) as exc:
        make_release._gate_completeness(archive, distinct=1, variants=3)
    assert "not a faithful copy" in str(exc.value)


def test_gate_hash_is_reproducible():
    """A gate whose numbers change between runs is a gate nobody can check.

    The builtin hash() is salted per process, so it cannot be used here even
    though cardinality would be unaffected.
    """
    import subprocess

    def digest(seed: str) -> str:
        return subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import make_release; "
             "print(make_release._h64('CHEBI:1|biolink:affects|NCBIGene:1'))" % str(ROOT)],
            capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()

    assert digest("1") == digest("2") == digest("12345")


# ---------------------------------------------------------------------------
# F6 — Elasticsearch version coupling
# ---------------------------------------------------------------------------

def test_es_client_major_mismatch_is_explained():
    """A newer client is refused during content negotiation.

    The raw error talks about media types and says nothing about the real
    problem, so it is translated into an actionable one.
    """
    pytest.importorskip("elasticsearch")
    from elastic_transport import ApiResponseMeta
    from elasticsearch import BadRequestError

    from metadata_db import ElasticsearchMetadataBackend as ES

    db = ES.__new__(ES)                       # no connection needed
    meta = ApiResponseMeta(status=400, http_version="1.1", headers={},
                           duration=0.0, node=None)
    err = BadRequestError("media_type_header_exception", meta=meta, body={})

    class _FakeES:
        def info(self):
            raise err

    db._es = _FakeES()  # type: ignore[assignment]
    with pytest.raises(RuntimeError) as exc:
        db.check_compatibility()
    msg = str(exc.value)
    assert "rejected by the server during content negotiation" in msg
    assert "pip install" in msg, "the message must say how to fix it"
