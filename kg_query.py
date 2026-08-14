"""High-level, free-text-friendly query helpers for the csrgraph knowledge graph.

This module is the convenience layer used to answer natural-language questions
like *"find paths connecting FREM1 to any disease"* against the loaded
Translator KG graph.  It wraps the lower-level :class:`CSRGraph` API with:

* a cached graph loader that defaults to the **translator_kg_2026-07-19** snapshot
  and the Elasticsearch metadata backend (override via env vars or arguments);
* name/symbol -> CURIE resolution through Elasticsearch;
* batch CURIE -> name resolution;
* common query shapes (connect two entities, associations to a category,
  neighbours) that enable **node subclassing by default** so edges attached to
  semantic subtypes (``rdfs:subClassOf`` / ``subclass_of`` descendants) are
  included.

Usage as a library::

    import kg_query as kq
    g = kq.get_graph()
    frem1 = kq.resolve("FREM1", category="biolink:Gene")[0]["id"]
    for path in kq.associations(g, frem1, "biolink:Disease", max_hops=2):
        print(kq.format_path(g, path))

Usage as a CLI (inside the project .venv)::

    python kg_query.py resolve "type 2 diabetes"
    python kg_query.py assoc  --from FREM1 --to-category biolink:Disease --max-hops 2
    python kg_query.py connect --from FREM1 --to "type 2 diabetes"
    python kg_query.py neighbors --of FREM1 --category biolink:Disease
"""
from __future__ import annotations

import argparse
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Sequence, Tuple, overload

from csrgraph_kgx import CSRGraph, MatchStats
from metadata_db import ElasticsearchMetadataBackend

# --------------------------------------------------------------------------- #
# Defaults (env-overridable, matching trapi_server.py conventions)
# --------------------------------------------------------------------------- #
DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "~/tmp/csrgraph_data")).expanduser()
DEFAULT_GRAPH = os.environ.get("GRAPH_NAME", "translator_kg_2026-07-19")
DEFAULT_ES_HOST = os.environ.get("ES_HOST", "http://localhost:9200")

PathEdge = tuple  # (subject, predicate, object)


# --------------------------------------------------------------------------- #
# Graph loading
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4)
def get_graph(
    name: str = DEFAULT_GRAPH,
    data_dir: str | os.PathLike = DEFAULT_DATA_DIR,
    es_host: str = DEFAULT_ES_HOST,
) -> CSRGraph:
    """Load (and cache) a graph by stem name with an Elasticsearch backend.

    Defaults to the ``translator_kg_2026-07-19`` snapshot in ``~/tmp/csrgraph_data``.
    The result is memoised, so repeated calls in one process reuse the same
    in-memory graph.
    """
    data_dir = Path(data_dir).expanduser()
    snapshot = data_dir / f"{name}.csrgraph.pkl.zst"
    if not snapshot.exists():
        raise FileNotFoundError(
            f"Snapshot not found: {snapshot}\n"
            f"Available graphs in {data_dir}: "
            + ", ".join(sorted(p.name.split('.')[0] for p in data_dir.glob('*.csrgraph.pkl.zst')))
        )
    db = ElasticsearchMetadataBackend(es_host, index_prefix=name)
    return CSRGraph.load(str(snapshot), db=db)


# --------------------------------------------------------------------------- #
# Name <-> CURIE resolution (via Elasticsearch)
# --------------------------------------------------------------------------- #
def resolve(
    text: str,
    *,
    category: Optional[str] = None,
    graph: Optional[CSRGraph] = None,
    top: int = 5,
) -> List[dict]:
    """Resolve a free-text name/symbol to candidate node CURIEs, best first.

    Returns a list of ``{"id", "name", "category"}`` dicts.  An exact
    (case-insensitive) name match is boosted so e.g. the gene symbol ``FREM1``
    ranks its exact node above partial matches.  Restrict to a biolink
    ``category`` (e.g. ``"biolink:Gene"``) to disambiguate symbols shared
    across entity types.
    """
    graph = graph or get_graph()
    es = graph.db._es  # ElasticsearchMetadataBackend
    index = f"{graph.db._nodes_idx}" if hasattr(graph.db, "_nodes_idx") else None
    if index is None:
        # Fall back to convention used by ElasticsearchMetadataBackend.
        index = f"{DEFAULT_GRAPH}_nodes"

    should = [
        {"term": {"name.keyword": {"value": text, "boost": 10}}},  # exact (if mapped)
        {"match_phrase": {"name": {"query": text, "boost": 5}}},
        {"match": {"name": {"query": text}}},
    ]
    query: dict = {"bool": {"should": should, "minimum_should_match": 1}}
    if category:
        cat = category.split(":", 1)[-1]  # strip biolink: prefix for stored value
        query = {"bool": {"must": [query], "filter": [{"term": {"category": cat}}]}}

    resp = es.search(
        index=index,
        query=query,
        size=top,
        _source=["id", "name", "category"],
    )
    out = []
    for hit in resp["hits"]["hits"]:
        s = hit["_source"]
        cats = s.get("category", [])
        out.append(
            {
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "category": [c if c.startswith("biolink:") else f"biolink:{c}" for c in cats],
            }
        )
    return out


def resolve_one(text: str, **kwargs) -> str:
    """Resolve *text* to a single best CURIE, raising if nothing matches."""
    hits = resolve(text, **kwargs)
    if not hits:
        raise ValueError(f"No node found matching {text!r}")
    return hits[0]["id"]


def names(graph: CSRGraph, curies: Sequence[str]) -> dict[str, str]:
    """Batch-resolve CURIEs to human-readable names via Elasticsearch ``_mget``.

    Falls back to the CURIE itself when a node has no stored name.
    """
    curies = list(dict.fromkeys(curies))
    if not curies:
        return {}
    es = graph.db._es
    index = getattr(graph.db, "_nodes_idx", f"{DEFAULT_GRAPH}_nodes")
    resp = es.mget(index=index, ids=curies, _source=["name"])
    out: dict[str, str] = {}
    for doc in resp["docs"]:
        cid = doc["_id"]
        src = doc.get("_source") or {}
        out[cid] = src.get("name") or cid
    return out


def name(graph: CSRGraph, curie: str) -> str:
    """Resolve a single CURIE to its name (CURIE itself if unknown)."""
    return names(graph, [curie]).get(curie, curie)


# --------------------------------------------------------------------------- #
# Query shapes  (node subclassing ON by default — semantic subtype edges)
# --------------------------------------------------------------------------- #
def neighbors(
    graph: CSRGraph,
    entity: str,
    *,
    category: Optional[str] = None,
    predicate: Optional[str] = None,
    node_subclassing: bool = True,
) -> List[dict]:
    """Direct neighbours of *entity*, optionally filtered to a target category.

    With ``node_subclassing=True`` (default), neighbours of *entity*'s semantic
    subtypes are included too.
    """
    nbrs = graph.neighbors(entity, relation=predicate, node_subclassing=node_subclassing)
    if category:
        return graph.filter_nodes(nbrs, category=category)
    return [{"id": n} for n in nbrs]


def connect(
    graph: CSRGraph,
    source: str,
    target: str,
    *,
    node_subclassing: bool = True,
    all_paths: bool = True,
) -> List[List[PathEdge]]:
    """Shortest path(s) connecting two specific entities.

    With ``node_subclassing=True`` (default), the source and target are each
    expanded to include their semantic subtypes, so a path that runs through a
    more specific subtype (e.g. *type 2 diabetes* under *diabetes mellitus*) is
    found.
    """
    if all_paths:
        return graph.all_shortest_paths(source, target, node_subclassing=node_subclassing)
    p = graph.shortest_path(source, target, node_subclassing=node_subclassing)
    return [p] if p else []


@overload
def associations(
    graph: CSRGraph, source: str, target_category: str, *,
    max_hops: int = ..., limit: int = ..., node_subclassing: bool = ...,
    return_stats: Literal[False] = ...,
) -> List[List[PathEdge]]: ...


@overload
def associations(
    graph: CSRGraph, source: str, target_category: str, *,
    max_hops: int = ..., limit: int = ..., node_subclassing: bool = ...,
    return_stats: Literal[True],
) -> Tuple[List[List[PathEdge]], MatchStats]: ...


def associations(
    graph: CSRGraph,
    source: str,
    target_category: str,
    *,
    max_hops: int = 1,
    limit: int = 1000,
    node_subclassing: bool = True,
    return_stats: bool = False,
):
    """All paths of length ``max_hops`` from *source* to any node of a category.

    Builds an alternating ``match_path`` spec ``[source, *, *, ..., {category}]``
    with wildcard intermediates.  ``node_subclassing=True`` (default) expands any
    fixed CURIE node spec to its subtypes; the category endpoint already matches
    every subtype of that category.

    Pass ``return_stats=True`` to get ``(paths, MatchStats)`` and check
    ``stats.truncated`` — a hop cap can make the result a subset of the matches
    rather than the complete set.  Truncation is logged as a warning regardless.
    """
    if max_hops < 1:
        raise ValueError("max_hops must be >= 1")
    spec: list = [source]
    for _ in range(max_hops - 1):
        spec += [None, None]  # wildcard edge, wildcard intermediate node
    spec += [None, {"category": target_category}]  # final wildcard edge + typed endpoint
    return graph.match_path(
        spec, limit=limit, node_subclassing=node_subclassing,
        return_stats=return_stats,
    )


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def format_path(graph: CSRGraph, path: Sequence[PathEdge]) -> str:
    """Render a path as ``Name (CURIE) --[predicate]--> Name (CURIE) ...``."""
    if not path:
        return "(empty path)"
    curies = []
    for s, _p, o in path:
        curies += [s, o]
    nm = names(graph, curies)

    def lbl(c: str) -> str:
        n = nm.get(c, c)
        return f"{n} ({c})" if n != c else c

    parts = [lbl(path[0][0])]
    for s, p, o in path:
        parts.append(f"--[{p}]--> {lbl(o)}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Free-text-friendly csrgraph queries (default graph: %s)" % DEFAULT_GRAPH
    )
    ap.add_argument("--graph", default=DEFAULT_GRAPH, help="graph stem name")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--es-host", default=DEFAULT_ES_HOST)
    ap.add_argument(
        "--no-subclassing",
        action="store_true",
        help="disable semantic subtype expansion (on by default)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve", help="resolve free text to candidate CURIEs")
    p_res.add_argument("text")
    p_res.add_argument("--category", default=None)

    p_assoc = sub.add_parser("assoc", help="paths from an entity to any node of a category")
    p_assoc.add_argument("--from", dest="src", required=True, help="name or CURIE")
    p_assoc.add_argument("--to-category", required=True, help="e.g. biolink:Disease")
    p_assoc.add_argument("--from-category", default=None, help="disambiguate --from")
    p_assoc.add_argument("--max-hops", type=int, default=1)
    p_assoc.add_argument("--limit", type=int, default=50)

    p_conn = sub.add_parser("connect", help="shortest path(s) between two entities")
    p_conn.add_argument("--from", dest="src", required=True)
    p_conn.add_argument("--to", dest="dst", required=True)
    p_conn.add_argument("--from-category", default=None)
    p_conn.add_argument("--to-category", default=None)

    p_nbr = sub.add_parser("neighbors", help="direct neighbours of an entity")
    p_nbr.add_argument("--of", dest="src", required=True)
    p_nbr.add_argument("--category", default=None)
    p_nbr.add_argument("--predicate", default=None)
    p_nbr.add_argument("--of-category", default=None)

    args = ap.parse_args(argv)
    subclassing = not args.no_subclassing
    g = get_graph(args.graph, args.data_dir, args.es_host)

    def to_curie(text: str, category: Optional[str]) -> str:
        if ":" in text and " " not in text and text.split(":", 1)[0].isalnum():
            return text  # looks like a CURIE already
        hit = resolve_one(text, category=category, graph=g)
        print(f"# resolved {text!r} -> {hit} ({name(g, hit)})", file=sys.stderr)
        return hit

    if args.cmd == "resolve":
        for h in resolve(args.text, category=args.category, graph=g):
            print(f"{h['id']:24s} {h['name']:45s} {h['category']}")
        return 0

    if args.cmd == "assoc":
        src = to_curie(args.src, args.from_category)
        paths = associations(
            g, src, args.to_category,
            max_hops=args.max_hops, limit=args.limit, node_subclassing=subclassing,
        )
        endpoints = {p[-1][2] for p in paths}
        print(f"{len(paths)} paths, {len(endpoints)} distinct {args.to_category} endpoints:\n")
        for p in paths:
            print("  " + format_path(g, p))
        return 0

    if args.cmd == "connect":
        src = to_curie(args.src, args.from_category)
        dst = to_curie(args.dst, args.to_category)
        paths = connect(g, src, dst, node_subclassing=subclassing)
        if not paths:
            print("No path found.")
            return 0
        print(f"{len(paths)} shortest path(s):\n")
        for p in paths:
            print("  " + format_path(g, p))
        return 0

    if args.cmd == "neighbors":
        src = to_curie(args.src, args.of_category)
        res = neighbors(
            g, src, category=args.category, predicate=args.predicate,
            node_subclassing=subclassing,
        )
        nm = names(g, [r["id"] for r in res])
        print(f"{len(res)} neighbours:\n")
        for r in res:
            cid = r["id"]
            print(f"  {nm.get(cid, cid)} ({cid})")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
