"""MCP server exposing the csrgraph knowledge graph to agentic clients.

A thin wrapper over the :mod:`kg_query` helpers, shaped for LLM consumption
rather than for completeness: tools take names *or* CURIEs, return compact
``Name (CURIE) --[predicate]--> Name (CURIE)`` strings instead of nested JSON,
and default to small result caps.  Those defaults are the point -- an agent pays
for every token of a tool result, so a helper that happily returns 1000 paths is
the wrong default even though the library one is.

Run it. stdio is the default and the usual case for a local client -- the client
spawns its own copy, so nothing needs starting or stopping::

    CSRGRAPH_DATA_DIR=~/tmp/releases/2026-07-19 CSRGRAPH_GRAPH_NAME=translator_kg_2026-07-19 \\
        .venv/bin/python mcp_server.py

    claude mcp add csrgraph -- /abs/path/.venv/bin/python /abs/path/mcp_server.py

``--http`` serves streamable HTTP instead, so several clients share *one* loaded
graph.  That matters because the graph is ~1.4 GB resident: N stdio clients cost
N times that, N HTTP clients cost it once.  The trade is that ``_LOCK`` is then
shared too, so a slow query in one session delays the others::

    .venv/bin/python mcp_server.py --http --port 8791

    claude mcp add --transport http csrgraph http://127.0.0.1:8791/mcp

Three properties are worth knowing before changing anything here.

**The graph loads once**, in ``main()`` before serving (or on first use if this
module is imported directly). Cold start is ~1.2 s against a release directory,
almost all of it the memmap attach; per-query cost is then well under a
millisecond for topology and ~1-2 ms for metadata.  A long-lived server pays that
once, which is the whole reason to prefer this over invoking the CLI per question.

**Tool calls are serialised.** ``_LOCK`` is not defensive boilerplate: LMDB reads
under concurrent threads *collapse* to x0.03 of single-thread throughput --
25x less aggregate work than one thread -- because each cursor step is a short C
call that hands the GIL back and forth (see
``docs/concurrency-and-scalability-2026-07-19.md``).  liblmdb and py-lmdb are both
thread-safe; the contention is the GIL, not correctness.  MCP clients issue
parallel tool calls freely, so without the lock a burst of them runs slower than
if it were queued.

**Resolution needs Elasticsearch.** ``backend="hybrid"`` sends point lookups to
LMDB (400x faster) and full-text to ES.  With ES absent the server still starts
and every tool except :func:`resolve_entity` works, because callers can pass
CURIEs directly.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from functools import lru_cache, wraps
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
    raise SystemExit(
        "The MCP server needs the optional 'mcp' extra:\n"
        "    .venv/bin/pip install -e '.[mcp]'\n"
        f"(import failed: {exc})"
    ) from exc

import kg_pattern as kp
import kg_query as kq
from metadata_db import env_flag, env_var

# --------------------------------------------------------------------------- #
# Configuration. All names are CSRGRAPH_-prefixed (see metadata_db.env_var):
# CSRGRAPH_DATA_DIR, CSRGRAPH_GRAPH_NAME, CSRGRAPH_ES_HOST, CSRGRAPH_NO_ES,
# CSRGRAPH_BIOLINK_VERSION.
# --------------------------------------------------------------------------- #
DATA_DIR = Path(env_var("DATA_DIR", "~/tmp/csrgraph_data")).expanduser()
GRAPH_NAME = env_var("GRAPH_NAME", kq.DEFAULT_GRAPH)
ES_HOST = kq.DEFAULT_ES_HOST      # from CSRGRAPH_ES_HOST; see metadata_db
NO_ES = env_flag("NO_ES")

#: Ceilings applied to whatever the model asks for.  A model that requests
#: max_hops=5 on this graph is not going to like the answer, and neither is its
#: context window.
MAX_HOPS_CEILING = 3

#: Cap on rows/paths *returned*. Unlike `kg_pattern.DEFAULT_ENUMERATE_LIMIT`,
#: which bounds correctness, this one only bounds cost: measured on the
#: 2026-07-19 graph, 200 rows is ~1,700 tokens for one column and ~2,300 for
#: two, so 500 is ~4,300-5,700 and 2000 would be ~23,000 -- most of a small
#: context window spent on one tool result.
#:
#: Raised 200 -> 500 because callers chasing complete answers were hitting it
#: routinely while the honest ceiling for an *agent* is still well under the
#: full answer set (the qualified-affects pattern has 843). `truncated` marks
#: the difference either way, which is what keeps a capped result from reading
#: as exhaustive.
#:
#: Override with ``CSRGRAPH_LIMIT_CEILING``. A test suite comparing against
#: complete answer sets should raise it; it is a policy knob, not a correctness
#: one, and the two consumers genuinely disagree about the right value.
LIMIT_CEILING = int(env_var("LIMIT_CEILING", "500"))

# Serialises graph access -- see the module docstring on the LMDB/GIL collapse.
_LOCK = threading.Lock()

mcp = MCPServer(
    name="csrgraph",
    instructions=(
        "Query a Biolink/Translator biomedical knowledge graph. Entity arguments "
        "accept either a CURIE (e.g. 'NCBIGene:1017') or a free-text name/symbol "
        "(e.g. 'CDK2', 'type 2 diabetes'), which is resolved automatically. "
        "Biolink categories are prefixed, e.g. 'biolink:Disease'. For 'disease or "
        "phenotype' use 'biolink:DiseaseOrPhenotypicFeature' to cover both at once."
    ),
)


# --------------------------------------------------------------------------- #
# Graph handle
# --------------------------------------------------------------------------- #
def _load_graph():
    """Load the graph once, preferring hybrid and degrading to LMDB-only.

    ES is optional on purpose: a release directory ships an LMDB store and no ES
    index, so requiring ES would make the common deployment unusable for the
    tools that do not need it.
    """
    if not NO_ES:
        try:
            return kq.get_graph(
                name=GRAPH_NAME, data_dir=DATA_DIR, es_host=ES_HOST, backend="hybrid",
            )
        except Exception as exc:
            print(f"ES unavailable ({exc}); falling back to LMDB-only "
                  f"(resolve_entity will be unavailable)", file=sys.stderr, flush=True)
    return kq.get_graph(name=GRAPH_NAME, data_dir=DATA_DIR, backend="lmdb")


_graph = None


def _g():
    """The loaded graph, loading it on first use.

    Deliberately *not* loaded at import. Doing that made ``--help`` fail with a
    FileNotFoundError about a missing snapshot -- argparse never got to run --
    and any CLI mistake needed a valid graph before it could report itself.
    ``main()`` pre-loads before serving, so a request never pays for this.

    Stdout is diverted to stderr for the load: under the stdio transport stdout
    *is* the JSON-RPC channel, and CSRGraph.load() unconditionally prints its
    timing and memory breakdown, which the client then rejects as malformed
    ("Invalid JSON: expected value at line 1 column 1"). Only the load prints --
    every other print reachable in csrgraph_kgx and metadata_db is on a load,
    save, memmap or index-build path, and the truncation warning goes through
    logger.warning to stderr already -- so no tool call has to redirect, which is
    fortunate: mutating sys.stdout under a live transport could swallow a reply.
    """
    global _graph
    if _graph is None:
        with contextlib.redirect_stdout(sys.stderr):
            _graph = _load_graph()
    return _graph


def _entity(text: str, category: str | None = None) -> str:
    """Accept a CURIE or a name; resolve names to the single best CURIE.

    The ``:`` test is how the CLI distinguishes the two, and it is good enough:
    Biolink CURIEs always carry a prefix, and entity names in this graph do not
    contain colons.
    """
    if ":" in text:
        return text
    return kq.resolve_one(text, category=category, graph=_g())


def _fmt_paths(paths, limit: int) -> list[str]:
    return [kq.format_path(_g(), p) for p in paths[:limit]]


@lru_cache(maxsize=1)
def _manifest() -> dict:
    """The release manifest for DATA_DIR, or {} for a hand-built directory."""
    path = DATA_DIR / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}


def _biolink_version() -> str | None:
    """Biolink version to expand predicates against.

    Taken from the release manifest, so expansion uses the same model the data
    was normalised with. BIOLINK_VERSION overrides it; None means "whatever the
    toolkit fetches", which drifts as Biolink releases and silently produces
    answers from a different model than the graph was built with.
    """
    return env_var("BIOLINK_VERSION") or _manifest().get("biolink_version")


def _resolver():
    """A name->CURIE callable for patterns, or None when ES is absent.

    Returning None rather than a raising stub lets kg_pattern produce its own
    "that is a name, pass a CURIE" error naming the offending triple, which is
    more useful than a generic ES failure.
    """
    if not kq._es_backend_available(_g().db):
        return None
    return lambda text: kq.resolve_one(text, graph=_g())


@lru_cache(maxsize=1)
def _categories(top: int) -> list[str] | None:
    """Categories present in the graph, most common first.

    Needs an ES terms aggregation: categories live in node metadata, so the only
    alternative is a full LMDB scan of ~1.7M nodes, which is too slow to do per
    call and too stale to cache. Returns None without ES rather than guessing
    from the Biolink model, which would list categories this graph may not have.
    """
    if not kq._es_backend_available(_g().db):
        return None
    be = kq._es_backend(_g().db)
    resp = be._es.search(
        index=be._nodes_idx, size=0,
        aggs={"c": {"terms": {"field": "category", "size": top}}},
    )
    return [f"biolink:{b['key']}" for b in resp["aggregations"]["c"]["buckets"]]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
#: Exceptions that mean "the caller asked for something impossible", as opposed
#: to "this server is broken". All three carry a message written to be read by
#: whoever made the call:
#:
#:   * PatternError   -- a malformed graph_query pattern, naming the triple
#:   * ValueError     -- an unresolvable name, or a CURIE not in the graph
#:   * RuntimeError   -- a lookup needing Elasticsearch while it is unavailable
#:
#: Deliberately *not* a blanket ``Exception``. TypeError, AttributeError, KeyError
#: and friends indicate a defect in this file rather than in the request, and
#: those should stay crashes so the server logs a traceback instead of telling
#: the model its input was at fault.
_ANTICIPATED = (kp.PatternError, ValueError, RuntimeError)


def _tool(fn):
    """Convert anticipated failures into ``ToolError`` for the tool boundary.

    Under mcp >= 2 this is the difference between a usable error and a useless
    one. ``MCPServer`` forwards a ``ToolError`` message to the client verbatim,
    but replaces every other exception's text with a bare ``Error executing tool
    <name>`` (``tools/base.py``: ``except Exception`` -> ``UnexpectedToolError``),
    withholding the original by design. So a hand-written, actionable message --
    "triple 0: unknown edge field 'bogus'" -- reached the model as nothing at
    all, and the three documented graph_query error cases were indistinguishable.

    Applied as a decorator *under* ``@mcp.tool`` so the conversion happens before
    the SDK's handler sees the exception. ``functools.wraps`` keeps
    ``__wrapped__`` intact, which is what lets ``mcp.tool`` still derive the input
    schema from the real signature.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _ANTICIPATED as exc:
            raise ToolError(str(exc)) from exc
    return wrapper

@mcp.tool(
    description="Resolve a free-text name or symbol to candidate CURIEs, best "
                "first. Requires Elasticsearch. Pass a category to disambiguate "
                "a symbol shared across entity types."
)
@_tool
def resolve_entity(text: str, category: str | None = None, top: int = 5) -> list[dict]:
    with _LOCK:
        return kq.resolve(text, category=category, graph=_g(),
                          top=min(top, LIMIT_CEILING))


@mcp.tool(
    description="Find what an entity is associated with: paths from it to any "
                "node of a target Biolink category. Use for 'what is X linked "
                "to' questions. max_hops>1 is much slower."
)
@_tool
def find_associations(
    entity: str,
    target_category: str,
    max_hops: int = 1,
    limit: int = 25,
    entity_category: str | None = None,
) -> dict:
    limit = min(limit, LIMIT_CEILING)
    with _LOCK:
        curie = _entity(entity, entity_category)
        paths = kq.associations(
            _g(), curie, target_category,
            max_hops=min(max_hops, MAX_HOPS_CEILING), limit=limit,
        )
        return {
            "source": curie,
            "returned": min(len(paths), limit),
            # Flagged so the model does not read a capped list as exhaustive.
            "truncated": len(paths) >= limit,
            "paths": _fmt_paths(paths, limit),
        }


@mcp.tool(
    description="Find how two specific entities are connected: shortest "
                "path(s) between them. Both ends are subtype-expanded."
)
@_tool
def connect_entities(
    source: str,
    target: str,
    limit: int = 25,
    source_category: str | None = None,
    target_category: str | None = None,
) -> dict:
    limit = min(limit, LIMIT_CEILING)
    with _LOCK:
        a = _entity(source, source_category)
        b = _entity(target, target_category)
        paths = kq.connect(_g(), a, b)
        return {
            "source": a,
            "target": b,
            "returned": min(len(paths), limit),
            "truncated": len(paths) > limit,
            "paths": _fmt_paths(paths, limit),
        }


@mcp.tool(
    description="List the direct (1-hop) neighbours of an entity, optionally "
                "filtered by Biolink category or predicate."
)
@_tool
def list_neighbors(
    entity: str,
    category: str | None = None,
    predicate: str | None = None,
    limit: int = 50,
    entity_category: str | None = None,
) -> dict:
    limit = min(limit, LIMIT_CEILING)
    with _LOCK:
        curie = _entity(entity, entity_category)
        rows = kq.neighbors(_g(), curie, category=category, predicate=predicate)
        return {
            "entity": curie,
            "returned": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "neighbors": rows[:limit],
        }


@mcp.tool(
    description=(
        "Run an arbitrary graph pattern -- the general tool, covering shapes the "
        "others cannot: branching, cycles, predicate and qualifier constraints. "
        "A pattern is a list of [subject, predicate, object] triples. Reusing a "
        "?variable across triples means the same node, which is how you express "
        "branches. Nodes: 'CURIE', 'free-text name', '?var', '?var:Category', "
        "'biolink:Category', or '*'. Predicates: null for any, 'affects', "
        "['affects','treats'], or {'predicate':'affects',"
        "'object_direction_qualifier':'increased'}. Call describe_schema for the "
        "predicate vocabulary. Example -- a disease reached from CDK2 via a "
        "protein and also treated by some drug: "
        "[['CDK2','affects','?p:Protein'],['?p',null,'?d:Disease'],"
        "['?drug','treats','?d']] with return_vars ['?d','?drug']."
    )
)
@_tool
def graph_query(
    pattern: list[list],
    return_vars: list[str] | None = None,
    limit: int = 25,
    expand_predicates: bool = False,
) -> dict:
    limit = min(limit, LIMIT_CEILING)
    with _LOCK:
        # PatternError reaches the client through @_tool; it needs no handling
        # here, and catching it to re-raise a ValueError actively broke it.
        return kp.run(
            _g(), pattern,
            return_vars=return_vars,
            limit=limit,
            resolver=_resolver(),
            expand_predicates=expand_predicates,
            biolink_version=_biolink_version(),
            name_lookup=lambda curies: kq.names(_g(), curies),
        )


@mcp.tool(
    description="List the predicates and node categories actually present in "
                "this graph. Call before authoring a graph_query pattern rather "
                "than guessing predicate names."
)
@_tool
def describe_schema(top_categories: int = 40) -> dict:
    with _LOCK:
        counts = getattr(_g(), "predicate_counts", {}) or {}
        predicates = sorted(
            _g().relations, key=lambda p: (-counts.get(p, 0), p)
        )
        return {
            "predicates": [
                {"predicate": f"biolink:{p}", "edges": counts.get(p, 0)}
                for p in predicates
            ],
            "categories": _categories(top_categories),
            "qualifiers": sorted(kp._QUALIFIER_ALIASES),
            "note": "Predicates are listed most-frequent first. The 'biolink:' "
                    "prefix is optional in patterns.",
        }


@mcp.tool(
    description="Report which graph release is loaded and which capabilities "
                "are available. Call this first if a query fails unexpectedly."
)
@_tool
def graph_info() -> dict:
    manifest = _manifest()
    return {
        "graph_name": GRAPH_NAME,
        "data_dir": str(DATA_DIR),
        "nodes": _g().num_nodes,
        "edges": _g().edge_count,
        "predicates": len(_g().relations),
        "backend": type(_g().db).__name__,
        # False means resolve_entity is unavailable and callers must pass CURIEs.
        "resolve_available": kq._es_backend_available(_g().db),
        "biolink_version": _biolink_version(),
        "release": None if not manifest else {
            "version": manifest.get("version"),
            "store_format_version": manifest.get("store_format_version"),
        },
    }


def main(argv: list[str] | None = None) -> None:
    """Serve over stdio (default) or streamable HTTP.

    stdio is what a local client wants: it spawns its own copy, so nothing needs
    starting or stopping. HTTP is for sharing *one* loaded graph across several
    clients -- the graph costs ~1.1 GB resident, so N stdio clients cost N times
    that, while N HTTP clients cost it once.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--http", action="store_true",
        help="serve streamable HTTP instead of stdio (shares one loaded graph)",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (default: loopback only)")
    # Not 8000: that is trapi_server.py's default, and the collision is silent
    # until one of them fails to bind.
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument(
        "--stateful", action="store_true",
        help="keep per-session server state (default is stateless, which lets "
             "independent agent sessions share the server with no affinity)",
    )
    args = parser.parse_args(argv)

    # Load before serving: argparse has had its chance, so --help and bad flags
    # no longer need a valid graph, and the first request does not pay for it.
    _g()

    if not args.http:
        mcp.run()
        return

    # Startup banner on stderr: under HTTP stdout is free, but keeping the stream
    # discipline identical to stdio mode means one less thing to get wrong.
    print(f"csrgraph MCP on http://{args.host}:{args.port}/mcp "
          f"(graph {GRAPH_NAME}, {_g().num_nodes:,} nodes)",
          file=sys.stderr, flush=True)
    mcp.run(
        transport="streamable-http",
        host=args.host, port=args.port,
        stateless_http=not args.stateful,
    )


if __name__ == "__main__":
    main()
