"""HelmsDeep TRAPI corpus, gated on the full graph being present.

Skipped everywhere the data is absent — CI included — so this costs nothing
there, but it is the only test that exercises real Translator query shapes
against the real graph. Every accuracy regression in this project's history was
caught here and by nothing else: the discarded traversal direction, the
multi-predicate disjunction, collapsed qualifier variants, phenotype nodes
answering a Disease-constrained query, and hash-seed-dependent truncation.

The assertions are *invariants*, deliberately not answer counts: counts move
whenever the graph is rebuilt, which would make this a tripwire for data changes
rather than for code changes.

Enable by pointing at a built graph::

    DATA_DIR=~/tmp/csrgraph_data GRAPH_NAME=translator_kg_2026-07-19 \
        .venv/bin/python -m pytest tests/test_corpus.py -q

``trapi_corpus.py`` comes from https://github.com/TranslatorSRI/HelmsDeep and
must be importable (put it on PYTHONPATH).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path(os.environ.get("DATA_DIR", "~/tmp/csrgraph_data")).expanduser()
STEM = os.environ.get("GRAPH_NAME", "translator_kg")
LIMIT = int(os.environ.get("CORPUS_LIMIT", "2000"))

_GRAPH = DATA_DIR / f"{STEM}.csrgraph.pkl.zst"
_LMDB = DATA_DIR / f"{STEM}.metadata.lmdb"

pytestmark = pytest.mark.skipif(
    not (_GRAPH.exists() and _LMDB.exists()),
    reason=f"corpus needs {_GRAPH} and {_LMDB}; set DATA_DIR/GRAPH_NAME",
)

# Shapes neither engine implements; both are expected to be rejected, not to
# crash, which is itself the assertion.
_UNSUPPORTED = {"malformed_query", "pathfinder_drug_disease"}


@pytest.fixture(scope="module")
def graph():
    from csrgraph_kgx import CSRGraph
    from metadata_db import LMDBMetadataBackend

    db = LMDBMetadataBackend(str(_LMDB))
    g = CSRGraph.load(str(_GRAPH))
    g.set_db(db)
    yield g
    db.close()


@pytest.fixture(scope="module")
def cases():
    import random

    tc = pytest.importorskip(
        "trapi_corpus", reason="HelmsDeep trapi_corpus.py not importable"
    )
    random.seed(0)  # the inferred builders sample entities at random
    out = []
    for segment in ("retriever", "shepherd", "pathfinder"):
        for qtype, builder, _weight in tc.corpus_for(segment):
            try:
                out.append((qtype, builder()["message"]["query_graph"]))
            except Exception as exc:  # pragma: no cover - corpus-side failure
                pytest.fail(f"corpus builder {qtype} failed: {exc!r}")
    return out


def _run(graph, qg):
    import trapi

    return trapi.query(graph, qg, limit=LIMIT)


def test_every_supported_query_answers(graph, cases):
    """No supported shape may return zero results.

    Zero was the signature of the direction defect, which silently emptied 7 of
    11 queries while looking like a healthy run.
    """
    empty = []
    for qtype, qg in cases:
        if qtype in _UNSUPPORTED:
            continue
        if not _run(graph, qg).get("results"):
            empty.append(qtype)
    assert not empty, f"queries returned no results: {empty}"


def test_unsupported_shapes_are_rejected_cleanly(graph, cases):
    """Rejection must be a ValueError naming the problem, not an arbitrary crash."""
    for qtype, qg in cases:
        if qtype not in _UNSUPPORTED:
            continue
        with pytest.raises(ValueError):
            _run(graph, qg)


def test_bindings_respect_queried_categories(graph, cases):
    """A bound node must satisfy its QNode's categories.

    Subclass-expanded nodes used to bypass this, so a query constrained to
    biolink:Disease returned HP: phenotype nodes.
    """
    db = graph.db
    violations = []
    for qtype, qg in cases:
        if qtype in _UNSUPPORTED:
            continue
        msg = _run(graph, qg)
        for nk, qn in (qg.get("nodes") or {}).items():
            wanted = set(qn.get("categories") or [])
            if not wanted:
                continue
            bound = {b["id"] for r in msg["results"]
                     for b in r["node_bindings"].get(nk, [])}
            for curie in bound:
                cats = set(db.get_node(curie).get("category") or [])
                if not (cats & wanted):
                    violations.append((qtype, nk, curie, sorted(wanted)))
    assert not violations, f"bindings violate queried categories: {violations[:5]}"


def test_query_id_marks_exactly_the_expanded_nodes(graph, cases):
    """query_id is present iff the bound CURIE is not one that was asked for."""
    wrong = []
    for qtype, qg in cases:
        if qtype in _UNSUPPORTED:
            continue
        msg = _run(graph, qg)
        for nk, qn in (qg.get("nodes") or {}).items():
            ids = set(qn.get("ids") or [])
            if not ids:
                continue
            for r in msg["results"]:
                for b in r["node_bindings"].get(nk, []):
                    expanded = b["id"] not in ids
                    if expanded != ("query_id" in b):
                        wrong.append((qtype, nk, b))
    assert not wrong, f"query_id does not match expansion: {wrong[:5]}"


def test_truncation_is_declared(graph, cases):
    """A result set that filled the cap must say it is incomplete.

    Only the forward implication is asserted. The converse does not hold, and
    the reason is worth recording: constraints are applied *after* the cap, so a
    query can enumerate its full quota of paths — genuinely truncating — and
    then post-filter down to fewer results than the limit.
    ``mvp2_chem_affects_gene`` at ``limit=5`` enumerates 5 paths and returns 2
    after qualifier filtering, and reporting truncation there is correct.
    """
    import trapi

    missing = []
    for qtype, qg in cases:
        if qtype in _UNSUPPORTED:
            continue
        msg = trapi.query(graph, qg, limit=5)
        logs = msg.get("logs") or []
        if len(msg["results"]) >= 5 and not any(
            e["code"] == "ResultsTruncated" for e in logs
        ):
            missing.append(qtype)
    assert not missing, f"hit the cap without reporting it: {missing}"
