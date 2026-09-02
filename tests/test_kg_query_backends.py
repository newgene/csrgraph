"""Backend routing in ``kg_query`` — data-free, using stub backends.

Covers the two places that reach into a backend's private attributes to find
Elasticsearch. Both were silently wrong for a hybrid backend, and both failed in
ways a test of the happy path would miss: ``_es`` names *different things* on the
two backend classes (the raw client on the ES backend, a nested backend on the
hybrid one), so the bug surfaced as an ``AttributeError`` deep inside a search
call, or worse, as a query against the wrong index that merely returned nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kg_query as kq  # noqa: E402
import metadata_db  # noqa: E402
from metadata_db import (  # noqa: E402
    ElasticsearchMetadataBackend,
    HybridMetadataBackend,
    LMDBMetadataBackend,
)


def _es_backend_stub(index_prefix: str = "g") -> ElasticsearchMetadataBackend:
    """An ES backend without a live cluster: only the attributes we route on."""
    be = object.__new__(ElasticsearchMetadataBackend)
    be._es = object()                        # stands in for the raw client
    be._nodes_idx = f"{index_prefix}_nodes"
    be._edges_idx = f"{index_prefix}_edges"
    return be


def _lmdb_backend_stub() -> LMDBMetadataBackend:
    return object.__new__(LMDBMetadataBackend)


def _hybrid(lmdb=None, es=None) -> HybridMetadataBackend:
    be = object.__new__(HybridMetadataBackend)
    be._lmdb, be._es, be._mode = lmdb, es, "auto"
    be._node_threshold, be._edge_threshold = 2000, None
    return be


class TestEsBackendUnwrapping:
    def test_plain_es_backend_is_returned_as_is(self):
        es = _es_backend_stub()
        assert kq._es_backend(es) is es

    def test_hybrid_unwraps_to_the_nested_es_backend(self):
        es = _es_backend_stub()
        # The bug: HybridMetadataBackend._es is an ElasticsearchMetadataBackend,
        # not a client, so callers reading ._es got a backend where a client was
        # expected.
        assert kq._es_backend(_hybrid(lmdb=_lmdb_backend_stub(), es=es)) is es

    def test_unwrapped_backend_carries_the_real_index_name(self):
        """The failure mode worth pinning: a wrong index returns 0 hits, not an error."""
        es = _es_backend_stub(index_prefix="translator_kg_2026-07-19")
        assert kq._es_backend(_hybrid(es=es))._nodes_idx == (
            "translator_kg_2026-07-19_nodes"
        )

    def test_lmdb_only_raises_with_actionable_message(self):
        with pytest.raises(RuntimeError, match="backend='es' or backend='hybrid'"):
            kq._es_backend(_lmdb_backend_stub())

    def test_hybrid_without_es_raises(self):
        with pytest.raises(RuntimeError):
            kq._es_backend(_hybrid(lmdb=_lmdb_backend_stub(), es=None))


class TestCapabilityProbes:
    @pytest.mark.parametrize("db, expected", [
        (_es_backend_stub(), True),
        (_hybrid(lmdb=_lmdb_backend_stub(), es=_es_backend_stub()), True),
        (_lmdb_backend_stub(), False),
        (_hybrid(lmdb=_lmdb_backend_stub(), es=None), False),
    ])
    def test_es_availability(self, db, expected):
        assert kq._es_backend_available(db) is expected

    @pytest.mark.parametrize("db, expected", [
        (_lmdb_backend_stub(), True),
        (_hybrid(lmdb=_lmdb_backend_stub(), es=_es_backend_stub()), True),
        (_es_backend_stub(), False),
        (_hybrid(lmdb=None, es=_es_backend_stub()), False),
    ])
    def test_lmdb_availability(self, db, expected):
        """Drives whether names() can work at all: an ES-free release must not
        fall into the _mget path, which is why this is a separate probe."""
        assert kq._has_lmdb(db) is expected


class TestGetGraphValidation:
    def test_unknown_backend_names_the_valid_choices(self):
        with pytest.raises(ValueError, match="hybrid"):
            kq.get_graph(name="nonexistent-graph", backend="bogus")

    def test_hybrid_requires_the_lmdb_store(self, tmp_path):
        """Hybrid must fail loudly on a missing store rather than degrade to ES."""
        (tmp_path / "g.csrgraph.pkl.zst").write_bytes(b"")   # snapshot exists
        with pytest.raises(FileNotFoundError, match="LMDB store not found"):
            kq.get_graph(name="g", data_dir=tmp_path, backend="hybrid")


class TestEnvVar:
    """``CSRGRAPH_<NAME>`` is read; the unprefixed ``<NAME>`` is not.

    The prefix exists because ``DATA_DIR``, ``GRAPH_NAME`` and ``ES_HOST`` are
    names other tools on the same machine set for their own purposes. Honouring
    one as a fallback would reintroduce the leak, so it must be ignored — but
    ignored *loudly*, since reading the wrong data or cluster returns empty
    results rather than an error.
    """

    @pytest.fixture(autouse=True)
    def _clear_warned(self):
        """The warn-once set is module state; reset it around each test."""
        metadata_db._warned_env.clear()
        yield
        metadata_db._warned_env.clear()

    @pytest.mark.parametrize("name", [
        "DATA_DIR", "GRAPH_NAME", "ES_HOST", "NO_ES", "BIOLINK_VERSION",
    ])
    def test_every_name_prefers_the_prefix_and_ignores_the_bare(
        self, name, monkeypatch, capsys
    ):
        monkeypatch.setenv(name, "bare")
        monkeypatch.delenv(metadata_db.ENV_PREFIX + name, raising=False)
        assert metadata_db.env_var(name, "fallback") == "fallback"
        assert name in capsys.readouterr().err

        monkeypatch.setenv(metadata_db.ENV_PREFIX + name, "prefixed")
        assert metadata_db.env_var(name, "fallback") == "prefixed"

    def test_warning_is_emitted_once_per_name(self, monkeypatch, capsys):
        """Several modules read DATA_DIR; one stale value is one warning."""
        monkeypatch.delenv(metadata_db.ENV_PREFIX + "DATA_DIR", raising=False)
        monkeypatch.setenv("DATA_DIR", "/somewhere/else")
        for _ in range(3):
            metadata_db.env_var("DATA_DIR", "d")
        assert capsys.readouterr().err.count("warning:") == 1

    @pytest.mark.parametrize("value, expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True),
        ("0", False), ("false", False), ("", False), ("maybe", False),
    ])
    def test_env_flag_truthiness(self, value, expected, monkeypatch):
        monkeypatch.setenv(metadata_db.ENV_PREFIX + "NO_ES", value)
        assert metadata_db.env_flag("NO_ES") is expected

    def test_env_flag_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(metadata_db.ENV_PREFIX + "NO_ES", raising=False)
        monkeypatch.delenv("NO_ES", raising=False)
        assert metadata_db.env_flag("NO_ES") is False

    def test_prefixed_var_is_used(self, monkeypatch):
        monkeypatch.setenv("CSRGRAPH_ES_HOST", "http://a:9200")
        monkeypatch.delenv("ES_HOST", raising=False)
        assert metadata_db.es_host_from_env() == "http://a:9200"

    def test_default_when_nothing_is_set(self, monkeypatch):
        monkeypatch.delenv("CSRGRAPH_ES_HOST", raising=False)
        monkeypatch.delenv("ES_HOST", raising=False)
        assert metadata_db.es_host_from_env() == metadata_db.DEFAULT_ES_HOST

    def test_legacy_var_is_ignored_not_inherited(self, monkeypatch, capsys):
        monkeypatch.delenv("CSRGRAPH_ES_HOST", raising=False)
        monkeypatch.setenv("ES_HOST", "http://someone-elses-cluster:9999")
        assert metadata_db.es_host_from_env() == metadata_db.DEFAULT_ES_HOST
        # Loudly, or a stale setting becomes an empty-results mystery.
        err = capsys.readouterr().err
        assert "ES_HOST" in err and "CSRGRAPH_ES_HOST" in err

    def test_prefixed_wins_and_stays_quiet(self, monkeypatch, capsys):
        monkeypatch.setenv("CSRGRAPH_ES_HOST", "http://a:9200")
        monkeypatch.setenv("ES_HOST", "http://b:9200")
        assert metadata_db.es_host_from_env() == "http://a:9200"
        assert capsys.readouterr().err == ""

    def test_explicit_default_is_respected(self, monkeypatch):
        monkeypatch.delenv("CSRGRAPH_ES_HOST", raising=False)
        monkeypatch.delenv("ES_HOST", raising=False)
        assert metadata_db.es_host_from_env("http://z:1") == "http://z:1"
