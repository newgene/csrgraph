"""Compact triple patterns for the full TRAPI query engine.

TRAPI QueryGraphs express everything this graph can answer -- linear chains,
branching, cycles, qualifier constraints -- but they are verbose to author and
verbose to answer, which makes them a poor fit for an LLM that pays tokens per
character both ways.  This module keeps the expressiveness and drops the format:
a pattern is a list of ``[subject, predicate, object]`` triples, translated to a
QueryGraph and handed to :func:`trapi.match`.

**Shared variables are the point.** A variable repeated across triples is the
same node, which is what makes branching and cyclic queries expressible at all::

    [["CDK2",   "affects", "?p:Protein"],
     ["?p",     None,      "?d:Disease"],
     ["?drug",  "treats",  "?d"]]

``?d`` appears twice, so this is a branch: a disease reached from CDK2 via some
protein *and* treated by some drug.  No fixed-shape helper can ask that.

Term grammar, deliberately small:

===============================  ==========================================
Node term                        Meaning
===============================  ==========================================
``"NCBIGene:1017"``              pinned node (a CURIE -- has a known prefix)
``"CDK2"``                       pinned node by name, resolved via *resolver*
``"?d"``                         variable, any category
``"?d:Disease"``                 variable constrained to a category
``"biolink:Disease"``            anonymous variable of that category
``"*"`` / ``None``               anonymous variable, any category
===============================  ==========================================

===============================  ==========================================
Edge term                        Meaning
===============================  ==========================================
``None`` / ``"*"``               any predicate
``"affects"``                    that predicate (``biolink:`` optional)
``["affects", "treats"]``        any of these predicates
``{"predicate": ..., ...}``      predicate plus qualifier constraints
===============================  ==========================================

Anything unrecognised raises :class:`PatternError` naming the offending triple by
index, because a model that mis-authors a pattern needs to know *which* term to
fix, not that "the query was invalid".
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence

from csrgraph_kgx import CSRGraph

import trapi

#: Node-term prefixes that mean "this is a Biolink category, not a CURIE".
_CATEGORY_PREFIX = "biolink:"

#: Short qualifier names accepted in an edge term, mapped to TRAPI type IDs.
#: Keyed off trapi's own table so the two cannot drift apart.
_QUALIFIER_ALIASES: dict[str, str] = {
    field: type_id for type_id, field in trapi._QUALIFIER_TYPE_TO_FIELD.items()
}


class PatternError(ValueError):
    """A pattern could not be translated. The message names the triple index."""


def _biolink(term: str) -> str:
    return term if term.startswith(_CATEGORY_PREFIX) else f"{_CATEGORY_PREFIX}{term}"


def _is_curie(term: str) -> bool:
    """Whether *term* looks like a CURIE rather than a free-text name.

    A colon is the discriminator, matching how ``kg_query``'s CLI tells the two
    apart. ``biolink:`` is excluded because the Biolink model owns that prefix
    for categories and predicates -- treating ``biolink:Disease`` as a pinned id
    would search for a node whose CURIE is a category name and quietly find
    nothing.
    """
    return ":" in term and not term.startswith(_CATEGORY_PREFIX)


def _query_terms(query_graph: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The predicates and qualifier values a query graph actually mentions."""
    predicates: set[str] = set()
    qualifiers: set[str] = set()
    for qedge in query_graph["edges"].values():
        predicates.update(qedge.get("predicates") or ())
        for constraint in qedge.get("qualifier_constraints") or ():
            for qual in constraint.get("qualifier_set") or ():
                value = qual.get("qualifier_value")
                if isinstance(value, str):
                    qualifiers.add(value)
    return tuple(sorted(predicates)), tuple(sorted(qualifiers))


@lru_cache(maxsize=32)
def _expander_for(
    predicates: tuple[str, ...], qualifier_values: tuple[str, ...]
) -> Any:
    """A BiolinkExpander covering exactly these terms.

    ``BiolinkExpander.from_bmt()`` resolves *only the terms passed to it* -- it
    iterates ``predicates or ()``, so calling it with no arguments builds an
    expander that expands nothing and silently behaves like literal matching.
    Deriving the terms from the query is therefore not an optimisation but the
    difference between expansion working and quietly doing nothing.

    Cached because constructing the toolkit parses the Biolink model (~1 s).
    """
    return trapi.BiolinkExpander.from_bmt(
        predicates=predicates or None,
        qualifier_values=qualifier_values or None,
    )


class _NodeAllocator:
    """Assigns query-graph node keys, reusing one key per pattern variable."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self._by_var: dict[str, str] = {}
        self._n = 0

    def _fresh(self) -> str:
        key = f"n{self._n}"
        self._n += 1
        return key

    def anonymous(self, category: str | None) -> str:
        key = self._fresh()
        self.nodes[key] = {"categories": [_biolink(category)]} if category else {}
        return key

    def pinned(self, curie: str) -> str:
        key = self._fresh()
        self.nodes[key] = {"ids": [curie]}
        return key

    def variable(self, var: str, category: str | None) -> str:
        """Return the key for *var*, creating it or refining its category.

        A variable may be introduced bare and constrained later (``?d`` in one
        triple, ``?d:Disease`` in another); the constraint applies to the single
        shared node either way.
        """
        key = self._by_var.get(var)
        if key is None:
            key = self._fresh()
            self._by_var[var] = key
            self.nodes[key] = {}
        if category:
            cats = self.nodes[key].setdefault("categories", [])
            if _biolink(category) not in cats:
                cats.append(_biolink(category))
        return key


def _parse_node(
    term: Any,
    alloc: _NodeAllocator,
    resolver: Callable[[str], str] | None,
    where: str,
) -> str:
    if term is None or term == "*":
        return alloc.anonymous(None)
    if not isinstance(term, str):
        raise PatternError(f"{where}: node term must be a string, got {term!r}")
    if term.startswith("?"):
        var, _, category = term[1:].partition(":")
        if not var:
            raise PatternError(f"{where}: variable needs a name, e.g. '?d:Disease'")
        return alloc.variable(var, category or None)
    if term.startswith(_CATEGORY_PREFIX):
        return alloc.anonymous(term)
    if _is_curie(term):
        return alloc.pinned(term)
    # A bare word: a name to resolve. Without a resolver this cannot proceed --
    # and must not silently degrade into a CURIE that does not exist.
    if resolver is None:
        raise PatternError(
            f"{where}: {term!r} is a name, not a CURIE, and no resolver is "
            f"available (Elasticsearch is required to resolve names). Pass a "
            f"CURIE instead."
        )
    return alloc.pinned(resolver(term))


def _parse_edge(term: Any, where: str) -> dict:
    """Return the QEdge fields contributed by an edge term."""
    if term is None or term == "*":
        return {}
    if isinstance(term, str):
        return {"predicates": [_biolink(term)]}
    if isinstance(term, (list, tuple)):
        return {"predicates": [_biolink(str(p)) for p in term]}
    if not isinstance(term, dict):
        raise PatternError(f"{where}: edge term must be a string, list or object")

    out: dict = {}
    predicate = term.get("predicate") or term.get("predicates")
    if isinstance(predicate, str):
        out["predicates"] = [_biolink(predicate)]
    elif isinstance(predicate, (list, tuple)):
        out["predicates"] = [_biolink(str(p)) for p in predicate]

    qualifier_set = []
    for key, value in term.items():
        if key in {"predicate", "predicates"}:
            continue
        type_id = _QUALIFIER_ALIASES.get(key) or (
            key if key in trapi._QUALIFIER_TYPE_TO_FIELD else None
        )
        if type_id is None:
            raise PatternError(
                f"{where}: unknown edge field {key!r}. Expected 'predicate' or "
                f"one of: {', '.join(sorted(_QUALIFIER_ALIASES))}"
            )
        qualifier_set.append(
            {"qualifier_type_id": type_id, "qualifier_value": value}
        )
    if qualifier_set:
        out["qualifier_constraints"] = [{"qualifier_set": qualifier_set}]
    return out


def to_query_graph(
    pattern: Sequence[Sequence[Any]],
    *,
    resolver: Callable[[str], str] | None = None,
) -> tuple[dict, dict[str, str]]:
    """Translate a triple *pattern* into a TRAPI QueryGraph.

    Returns the query graph and a ``{variable: qnode_key}`` map, which the caller
    needs to project results back onto the names the pattern used.
    """
    if not pattern:
        raise PatternError("pattern is empty: expected at least one triple")
    alloc = _NodeAllocator()
    edges: dict[str, dict] = {}
    for i, triple in enumerate(pattern):
        where = f"triple {i}"
        if len(triple) != 3:
            raise PatternError(
                f"{where}: expected [subject, predicate, object], got "
                f"{len(triple)} element(s)"
            )
        subject, predicate, obj = triple
        s_key = _parse_node(subject, alloc, resolver, f"{where} subject")
        o_key = _parse_node(obj, alloc, resolver, f"{where} object")
        edges[f"e{i}"] = {
            "subject": s_key, "object": o_key, **_parse_edge(predicate, where),
        }
    return {"nodes": alloc.nodes, "edges": edges}, dict(alloc._by_var)


def run(
    graph: CSRGraph,
    pattern: Sequence[Sequence[Any]],
    *,
    return_vars: Iterable[str] | None = None,
    limit: int = 25,
    enumerate_limit: int = 1000,
    resolver: Callable[[str], str] | None = None,
    expand_predicates: bool = False,
    expander: Any | None = None,
    node_subclassing: bool = True,
    name_lookup: Callable[[Sequence[str]], dict[str, str]] | None = None,
    require_pinned: bool = True,
) -> dict:
    """Match *pattern* and project the result into compact rows.

    ``limit`` caps the rows returned; ``enumerate_limit`` is TRAPI's very
    different cap on paths *enumerated* before constraints are applied, which is
    why the two are separate. Under-setting the latter silently under-answers
    constrained queries, so it keeps the engine's own default rather than
    inheriting the much smaller row cap.

    ``require_pinned`` rejects a pattern in which every node is a variable. Such
    a pattern gives the matcher no anchor and degenerates into scanning the
    graph. The check runs *after* translation so that a malformed pattern
    reports its actual error rather than "nothing is pinned" -- and so that a
    node pinned by name counts, since resolution happens during translation.
    """
    query_graph, var_map = to_query_graph(pattern, resolver=resolver)
    if require_pinned and not any(
        "ids" in qnode for qnode in query_graph["nodes"].values()
    ):
        raise PatternError(
            "pattern needs at least one pinned node (a CURIE or a name) to "
            "anchor the search; an all-variable pattern would enumerate the "
            "whole graph"
        )
    if expander is None and expand_predicates:
        expander = _expander_for(*_query_terms(query_graph))
    result = trapi.match(
        graph, query_graph, limit=enumerate_limit, expander=expander,
        node_subclassing=node_subclassing,
    )

    # Default to every named variable, in first-appearance order.
    columns = list(return_vars) if return_vars is not None else list(var_map)
    columns = [c.lstrip("?") for c in columns]
    unknown = [c for c in columns if c not in var_map]
    if unknown:
        raise PatternError(
            f"return_vars names variables not in the pattern: {unknown}. "
            f"Available: {sorted(var_map) or '(none -- pattern has no variables)'}"
        )

    rows: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    capped = False
    for binding in result.bindings:
        curies = tuple(binding["nodes"].get(var_map[c], "") for c in columns)
        # One query graph can bind the same tuple of *returned* nodes by several
        # distinct paths; as a projection those are one answer, so dedupe rather
        # than repeat a row the caller cannot tell apart.
        if curies in seen:
            continue
        # Count distinct answers past the cap instead of stopping, so `truncated`
        # reflects "more answers exist" rather than "more bindings existed" --
        # after dedupe those differ, and the second would cry wolf on every
        # multi-path query.
        if len(rows) >= limit:
            capped = True
            break
        seen.add(curies)
        rows.append(list(curies))

    names = name_lookup([c for row in rows for c in row]) if name_lookup else {}

    def _label(curie: str) -> str:
        # Only pair a name with its CURIE when the name adds something. Nodes
        # with no stored name resolve to the CURIE itself, and "HGNC:1 (HGNC:1)"
        # is noise the caller pays tokens for.
        name = names.get(curie)
        if not curie:
            return ""
        return f"{name} ({curie})" if name and name != curie else curie

    return {
        "columns": [f"?{c}" for c in columns],
        "rows": [[_label(c) for c in row] for row in rows],
        "returned": len(rows),
        # Either the engine hit its enumeration cap or we stopped projecting.
        "truncated": result.truncated or capped,
        "matched_paths": len(result.bindings),
    }
