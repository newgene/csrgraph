"""The TRAPI server must distinguish a bad request from its own failure.

Every exception used to come back as ``400``, so an internal fault told the
caller their request was wrong and hid real breakage from anything alerting on
5xx.  Worse, the query graph was rendered to the log *before* the handler's
``try``, so a malformed graph raised ``KeyError`` and escaped as an unhandled
500 before validation ran at all — the HelmsDeep ``malformed_query`` case.

No data needed: the graph is a three-triple CSRGraph with a stub backend.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fastapi = pytest.importorskip("fastapi", reason="server tests need fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from csrgraph_kgx import CSRGraph  # noqa: E402
from metadata_db import MetadataBackend  # noqa: E402


class _StubDB(MetadataBackend):
    def get_node(self, nid):
        return {"id": nid, "category": ["biolink:Gene"]}

    def get_edge(self, subject, predicate, obj):
        return {"subject": subject, "predicate": predicate, "object": obj}

    def filter_nodes(self, node_ids, *, category=None, extra_filters=None):
        return [self.get_node(n) for n in node_ids]

    def filter_edges(self, edges, *, knowledge_level=None, agent_type=None,
                     extra_filters=None):
        return [{"subject": s, "predicate": p, "object": o} for s, p, o in edges]

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    import trapi_server

    g = CSRGraph([
        ("C:1", "biolink:treats", "D:1"),
        ("C:1", "biolink:affects", "G:1"),
        ("G:1", "biolink:affects", "D:1"),
    ])
    g.set_db(_StubDB())
    monkeypatch.setattr(trapi_server, "_graph", g)
    # raise_server_exceptions=False so an unhandled error surfaces as the 500 a
    # real client would see, rather than propagating into the test.
    with TestClient(trapi_server.app, raise_server_exceptions=False) as c:
        yield c


def _post(client, query_graph):
    return client.post("/query", json={"message": {"query_graph": query_graph}})


def test_valid_query_succeeds(client):
    r = _post(client, {
        "nodes": {"n0": {"ids": ["C:1"]}, "n1": {"categories": ["biolink:Gene"]}},
        "edges": {"e0": {"subject": "n0", "object": "n1",
                         "predicates": ["biolink:affects"]}},
    })
    assert r.status_code == 200


def test_edge_naming_unknown_node_is_400_not_a_crash(client):
    """The malformed_query corpus case: this used to escape as an unhandled 500."""
    r = _post(client, {
        "nodes": {"n0": {"ids": ["C:1"]}},
        "edges": {"e0": {"subject": "n0", "object": "n_missing"}},
    })
    assert r.status_code == 400
    assert "n_missing" in r.json()["detail"]


def test_pathfinder_shape_is_400(client):
    r = _post(client, {"nodes": {"n0": {"ids": ["C:1"]}}, "paths": {"p0": {}}})
    assert r.status_code == 400


def test_internal_failure_is_500(client, monkeypatch):
    """A fault on our side must not be reported as a client error."""
    import trapi_server

    def _boom(*a, **kw):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(trapi_server, "query", _boom)
    r = _post(client, {
        "nodes": {"n0": {"ids": ["C:1"]}, "n1": {"categories": ["biolink:Gene"]}},
        "edges": {"e0": {"subject": "n0", "object": "n1",
                         "predicates": ["biolink:affects"]}},
    })
    assert r.status_code == 500
    assert r.json()["error"] == "internal error"
