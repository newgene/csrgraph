"""TRAPI QueryGraph support for CSRGraph.

Translates a TRAPI 1.6 QueryGraph into CSRGraph operations and formats the
results as a TRAPI Response ``Message``.

Supports **linear chains** (fast path via ``match_path``), **branching**
queries, and **cyclic** queries (general subgraph matcher with backtracking).

Usage::

    from csrgraph_kgx import CSRGraph
    from trapi import query

    graph = CSRGraph.load("translator_kg.csrgraph.pkl.zst", db=db)

    # One-hop: Metformin → Gene
    message = query(graph, {
        "nodes": {
            "n0": {"ids": ["CHEBI:6801"]},
            "n1": {"categories": ["biolink:Gene"]},
        },
        "edges": {
            "e0": {"subject": "n0", "object": "n1", "predicates": ["biolink:affects"]},
        },
    })

    # Two-hop: Drug → Gene → Disease
    message = query(graph, {
        "nodes": {
            "n0": {"ids": ["CHEBI:6801"]},
            "n1": {"categories": ["biolink:Gene"]},
            "n2": {"categories": ["biolink:Disease"]},
        },
        "edges": {
            "e0": {"subject": "n0", "object": "n1"},
            "e1": {"subject": "n1", "object": "n2"},
        },
    })

    # Triangle (cyclic): Drug → Gene → Disease → Drug
    message = query(graph, {
        "nodes": {
            "n0": {"ids": ["CHEBI:6801"]},
            "n1": {"categories": ["biolink:Gene"]},
            "n2": {"categories": ["biolink:Disease"]},
        },
        "edges": {
            "e0": {"subject": "n0", "object": "n1"},
            "e1": {"subject": "n1", "object": "n2"},
            "e2": {"subject": "n2", "object": "n0"},
        },
    })
"""

from __future__ import annotations

import re
from typing import Any

from csrgraph_kgx import CSRGraph, _strip_biolink

# Type alias for a complete binding of a query graph to concrete nodes/edges.
# nodes: qnode_key → CURIE,  edges: qedge_key → (subject, predicate, object)
Binding = dict[str, Any]  # {"nodes": dict[str,str], "edges": dict[str,tuple]}

# TRAPI qualifier type IDs → edge metadata field names in our backends
_QUALIFIER_TYPE_TO_FIELD: dict[str, str] = {
    "biolink:qualified_predicate": "qualified_predicate",
    "biolink:object_aspect_qualifier": "object_aspect_qualifier",
    "biolink:object_direction_qualifier": "object_direction_qualifier",
    "biolink:subject_aspect_qualifier": "subject_aspect_qualifier",
    "biolink:subject_direction_qualifier": "subject_direction_qualifier",
    "biolink:causal_mechanism_qualifier": "causal_mechanism_qualifier",
    "biolink:species_context_qualifier": "species_context_qualifier",
    "biolink:disease_context_qualifier": "disease_context_qualifier",
    "biolink:frequency_qualifier": "frequency_qualifier",
}

RESOURCE_ID = "infores:csrgraph"

# ---------------------------------------------------------------------------
# Symmetric predicates from Biolink Model v4.3.7
# Source: https://github.com/biolink/biolink-model/blob/master/biolink-model.yaml
# To update: fetch biolink-model.yaml from the URL above and search for all
# slots with ``symmetric: true``.  Convert slot names to biolink: CURIEs
# (replace spaces with underscores, add ``biolink:`` prefix).
# ---------------------------------------------------------------------------
SYMMETRIC_PREDICATES: frozenset[str] = frozenset({
    "biolink:associated_with",
    "biolink:binds",
    "biolink:chemically_similar_to",
    "biolink:close_match",
    "biolink:coexists_with",
    "biolink:coexpressed_with",
    "biolink:colocalizes_with",
    "biolink:correlated_with",
    "biolink:directly_physically_interacts_with",
    "biolink:exact_match",
    "biolink:gene_fusion_with",
    "biolink:genetic_association",
    "biolink:genetic_neighborhood_of",
    "biolink:genetically_associated_with",
    "biolink:genetically_interacts_with",
    "biolink:homologous_to",
    "biolink:in_cell_population_with",
    "biolink:in_complex_with",
    "biolink:in_linkage_disequilibrium_with",
    "biolink:in_pathway_with",
    "biolink:indirectly_physically_interacts_with",
    "biolink:interacts_with",
    "biolink:negatively_correlated_with",
    "biolink:occurs_together_in_literature_with",
    "biolink:opposite_of",
    "biolink:orthologous_to",
    "biolink:overlaps",
    "biolink:paralogous_to",
    "biolink:pharmacologically_interacts_with",
    "biolink:physically_interacts_with",
    "biolink:positively_correlated_with",
    "biolink:related_condition",
    "biolink:related_to",
    "biolink:related_to_at_concept_level",
    "biolink:related_to_at_instance_level",
    "biolink:same_as",
    "biolink:similar_to",
    "biolink:temporally_related_to",
    "biolink:xenologous_to",
})


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def display_query_graph(query_graph: dict) -> str:
    """Return an ASCII visualisation of a TRAPI QueryGraph.

    Example output::

        (n0:CHEBI:6801 SmallMolecule) --[e0:affects]--> (n1:Gene)
                                      --[e1:treats]-->  (n2:Disease)
                                                        (n2:Disease) --[e2:treated_by]--> (n0:CHEBI:6801 SmallMolecule)
    """
    qnodes = query_graph["nodes"]
    qedges = query_graph["edges"]

    def _node_label(nk: str) -> str:
        qn = qnodes[nk]
        parts = [nk]
        ids = qn.get("ids")
        if ids:
            parts.append(",".join(ids[:2]))
            if len(ids) > 2:
                parts.append("...")
        cats = qn.get("categories")
        if cats:
            short = [c.replace("biolink:", "") for c in cats[:2]]
            parts.append(",".join(short))
        return ":".join(parts[:2]) + (" " + parts[2] if len(parts) > 2 else "")

    def _edge_label(ek: str) -> str:
        qe = qedges[ek]
        preds = qe.get("predicates")
        if preds:
            short = [p.replace("biolink:", "") for p in preds[:2]]
            label = ",".join(short)
            if len(preds) > 2:
                label += ",..."
        else:
            label = "*"
        quals = qe.get("qualifier_constraints", [])
        if quals:
            label += "+Q"
        return f"{ek}:{label}"

    # Group edges by subject node for compact display.
    by_subject: dict[str, list[str]] = {}
    for ek, qe in qedges.items():
        by_subject.setdefault(qe["subject"], []).append(ek)

    lines: list[str] = []
    rendered_edges: set[str] = set()

    # Render nodes that have outgoing edges.
    for subj_nk in list(by_subject.keys()):
        edge_keys = by_subject[subj_nk]
        src = f"({_node_label(subj_nk)})"
        for i, ek in enumerate(edge_keys):
            qe = qedges[ek]
            obj_nk = qe["object"]
            tgt = f"({_node_label(obj_nk)})"
            elbl = f"[{_edge_label(ek)}]"
            arrow = f" --{elbl}--> "
            if i == 0:
                lines.append(f"{src}{arrow}{tgt}")
            else:
                pad = " " * len(src)
                lines.append(f"{pad}{arrow}{tgt}")
            rendered_edges.add(ek)

    # Render any edges not yet shown (shouldn't happen, but safety net).
    for ek in qedges:
        if ek not in rendered_edges:
            qe = qedges[ek]
            src = f"({_node_label(qe['subject'])})"
            tgt = f"({_node_label(qe['object'])})"
            lines.append(f"{src} --[{_edge_label(ek)}]--> {tgt}")

    return "\n".join(lines)


def query(
    graph: CSRGraph,
    query_graph: dict,
    *,
    limit: int = 100,
) -> dict:
    """Execute a TRAPI QueryGraph against a CSRGraph and return a TRAPI Message.

    Parameters
    ----------
    graph : CSRGraph
        Graph with an attached metadata backend (``graph.db`` must be set).
    query_graph : dict
        A TRAPI 1.6 ``QueryGraph`` dict with ``nodes`` and ``edges``.
    limit : int
        Maximum number of results to return (default 100).

    Returns
    -------
    dict
        A TRAPI ``Message`` dict with ``query_graph``, ``knowledge_graph``,
        and ``results``.
    """
    qnodes: dict[str, dict] = query_graph["nodes"]
    qedges: dict[str, dict] = query_graph["edges"]

    # Check if any edge uses symmetric predicates (needs bidirectional search).
    has_symmetric = any(
        any(p in SYMMETRIC_PREDICATES for p in qe.get("predicates", []))
        for qe in qedges.values()
    )

    # Fast path: try to linearise into a chain for match_path().
    # Falls back to general matcher for non-linear or symmetric queries.
    if has_symmetric:
        bindings = _general_match(graph, qnodes, qedges, limit)
    else:
        try:
            ordered_node_keys, ordered_edge_keys = _linearise(qnodes, qedges)
            bindings = _linear_query(
                graph, qnodes, qedges, ordered_node_keys, ordered_edge_keys, limit,
            )
        except ValueError:
            # Branching or cyclic — use the general subgraph matcher.
            bindings = _general_match(graph, qnodes, qedges, limit)

    # Post-filter pipeline: node constraints → edge attributes → qualifiers.
    bindings = _apply_node_constraint_filters(graph, bindings, qnodes)
    bindings = _apply_edge_attribute_constraints(graph, bindings, qedges)
    bindings = _apply_qualifier_filters(graph, bindings, qedges)

    return _build_message(graph, query_graph, bindings)


# ──────────────────────────────────────────────────────────────────────────────
# Linear-chain fast path (delegates to match_path)
# ──────────────────────────────────────────────────────────────────────────────

def _linear_query(
    graph: CSRGraph,
    qnodes: dict[str, dict],
    qedges: dict[str, dict],
    ordered_node_keys: list[str],
    ordered_edge_keys: list[str],
    limit: int,
) -> list[Binding]:
    """Execute a linear-chain query via match_path and convert to bindings.

    Handles multiple IDs on the start node (BATCH expansion) and multiple
    predicates per edge (wildcard + post-filter).
    """
    # Determine which start IDs to iterate over (BATCH expansion).
    start_nk = ordered_node_keys[0]
    start_qn = qnodes[start_nk]
    start_ids = start_qn.get("ids") or [None]

    # Collect which edges have multiple predicates (need post-filtering).
    multi_pred_edges: dict[int, set[str]] = {}
    for hop_idx, ek in enumerate(ordered_edge_keys):
        preds = qedges[ek].get("predicates")
        if preds and len(preds) > 1:
            multi_pred_edges[hop_idx] = set(preds)

    # Collect which edges use symmetric predicates (search both directions).
    symmetric_edges: dict[int, bool] = {}
    for hop_idx, ek in enumerate(ordered_edge_keys):
        preds = qedges[ek].get("predicates") or []
        if any(p in SYMMETRIC_PREDICATES for p in preds):
            symmetric_edges[hop_idx] = True

    bindings: list[Binding] = []

    for start_id in start_ids:
        if len(bindings) >= limit:
            break

        # Build path_spec, substituting the start ID for this iteration.
        path_spec = _build_path_spec(
            ordered_node_keys, ordered_edge_keys, qnodes, qedges,
            start_id_override=start_id,
        )
        remaining = limit - len(bindings)
        raw_paths = graph.match_path(path_spec, limit=remaining)

        for path in raw_paths:
            nodes: dict[str, str] = {}
            edges: dict[str, tuple] = {}

            if path:
                nodes[ordered_node_keys[0]] = path[0][0]
                for hop_idx, (_, _, obj) in enumerate(path):
                    nodes[ordered_node_keys[hop_idx + 1]] = obj

            # Post-filter: check multi-predicate edges.
            skip = False
            for hop_idx, ek in enumerate(ordered_edge_keys):
                if hop_idx < len(path):
                    subj, pred, obj = path[hop_idx]
                    if hop_idx in multi_pred_edges:
                        if pred not in multi_pred_edges[hop_idx]:
                            skip = True
                            break
                    edges[ek] = path[hop_idx]
            if skip:
                continue

            bindings.append({"nodes": nodes, "edges": edges})
            if len(bindings) >= limit:
                break

    return bindings


def _linearise(
    qnodes: dict[str, dict],
    qedges: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Convert a QueryGraph into an ordered linear chain of node/edge keys.

    Raises ``ValueError`` for disconnected, branching, or cyclic query graphs.
    """
    adj: dict[str, list[tuple[str, str, bool]]] = {nk: [] for nk in qnodes}
    for ek, qe in qedges.items():
        subj, obj = qe["subject"], qe["object"]
        adj[subj].append((ek, obj, True))
        adj[obj].append((ek, subj, False))

    def _start_score(nk: str) -> tuple[int, int]:
        has_ids = 0 if qnodes[nk].get("ids") else 1
        return (has_ids, len(adj[nk]))

    start = min(qnodes, key=_start_score)

    ordered_nodes: list[str] = [start]
    ordered_edges: list[str] = []
    visited_edges: set[str] = set()
    visited_nodes: set[str] = {start}

    current = start
    while True:
        next_hop = None
        for ek, nbr, is_fwd in adj[current]:
            if ek not in visited_edges and nbr not in visited_nodes:
                next_hop = (ek, nbr, is_fwd)
                break
        if next_hop is None:
            break
        ek, nbr, is_fwd = next_hop
        visited_edges.add(ek)
        visited_nodes.add(nbr)
        ordered_edges.append(ek)
        ordered_nodes.append(nbr)
        current = nbr

    if len(ordered_nodes) != len(qnodes) or len(ordered_edges) != len(qedges):
        raise ValueError("Non-linear query graph")

    return ordered_nodes, ordered_edges


# ──────────────────────────────────────────────────────────────────────────────
# General subgraph matcher (handles branching and cyclic queries)
# ──────────────────────────────────────────────────────────────────────────────

def _general_match(
    graph: CSRGraph,
    qnodes: dict[str, dict],
    qedges: dict[str, dict],
    limit: int,
) -> list[Binding]:
    """Find all subgraph bindings matching an arbitrary query graph pattern.

    Uses backtracking search:
    1. Pick the most constrained unbound QNode adjacent to a bound QNode.
    2. Enumerate candidate CURIEs by traversing edges from bound neighbors.
    3. Verify all edges between the candidate and already-bound QNodes.
    4. Recurse until all QNodes are bound, or backtrack.

    For cycles, step 3 catches edges between two already-bound nodes and
    verifies they exist in the data graph.
    """
    # Build query-graph adjacency: qnode_key → [(qedge_key, neighbor_qnode_key)]
    adj: dict[str, list[tuple[str, str]]] = {nk: [] for nk in qnodes}
    for ek, qe in qedges.items():
        adj[qe["subject"]].append((ek, qe["object"]))
        adj[qe["object"]].append((ek, qe["subject"]))

    db = graph._require_db()

    # Pick start node: prefer pinned ids, then most edges (most constrained).
    def _start_score(nk: str) -> tuple[int, int]:
        has_ids = 0 if qnodes[nk].get("ids") else 1
        return (has_ids, -len(adj[nk]))

    start_key = min(qnodes, key=_start_score)
    start_candidates = _get_candidates(graph, db, qnodes[start_key])
    if not start_candidates:
        return []

    results: list[Binding] = []

    def _backtrack(
        node_bindings: dict[str, str],
        edge_bindings: dict[str, tuple],
    ) -> None:
        if len(results) >= limit:
            return

        # All nodes bound — record result.
        if len(node_bindings) == len(qnodes):
            # Verify any remaining unverified edges (cycles).
            for ek, qe in qedges.items():
                if ek not in edge_bindings:
                    s_curie = node_bindings[qe["subject"]]
                    o_curie = node_bindings[qe["object"]]
                    preds = _matching_predicates(graph, qe, s_curie, o_curie)
                    if not preds:
                        return
                    edge_bindings[ek] = (s_curie, preds[0], o_curie)
            results.append({
                "nodes": dict(node_bindings),
                "edges": dict(edge_bindings),
            })
            return

        # Pick next unbound QNode: must be adjacent to at least one bound QNode.
        # Prefer nodes with pinned ids, then most constrained (most bound neighbors).
        next_key = None
        best_score = (2, 0)  # (no_ids, -bound_neighbors)
        for nk in qnodes:
            if nk in node_bindings:
                continue
            bound_nbrs = sum(
                1 for _, nbr in adj[nk] if nbr in node_bindings
            )
            if bound_nbrs == 0:
                continue
            has_ids = 0 if qnodes[nk].get("ids") else 1
            score = (has_ids, -bound_nbrs)
            if score < best_score:
                best_score = score
                next_key = nk

        if next_key is None:
            # Disconnected component — shouldn't happen if the query is connected.
            return

        # Gather candidates: intersect neighbors from all bound adjacent QNodes.
        candidate_sets: list[set[str]] = []
        connecting_edges: list[tuple[str, str, bool]] = []  # (ek, bound_nk, is_forward)

        for ek, nbr in adj[next_key]:
            if nbr not in node_bindings:
                continue
            qe = qedges[ek]
            is_forward = (qe["subject"] == nbr)
            connecting_edges.append((ek, nbr, is_forward))

            bound_curie = node_bindings[nbr]
            if is_forward:
                # nbr is subject, next_key is object: get outgoing neighbors of nbr
                nbrs = _get_edge_neighbors(graph, qe, bound_curie, forward=True)
            else:
                # nbr is object, next_key is subject: get nodes that point TO nbr
                # Since our graph is directed (subject→object), next_key→nbr
                nbrs = _get_edge_neighbors(graph, qe, bound_curie, forward=False)
            candidate_sets.append(set(nbrs))

        if not candidate_sets:
            return

        # Intersect all candidate sets.
        candidates = candidate_sets[0]
        for cs in candidate_sets[1:]:
            candidates &= cs
            if not candidates:
                return

        # Filter candidates by QNode constraints.
        candidates = _filter_by_qnode(graph, db, qnodes[next_key], list(candidates))

        for curie in candidates:
            if len(results) >= limit:
                return
            # Bind this node and all connecting edges.
            node_bindings[next_key] = curie
            new_edges: list[str] = []
            valid = True
            for ek, bound_nk, is_forward in connecting_edges:
                qe = qedges[ek]
                if is_forward:
                    s, o = node_bindings[bound_nk], curie
                else:
                    s, o = curie, node_bindings[bound_nk]
                preds = _matching_predicates(graph, qe, s, o)
                if not preds:
                    valid = False
                    break
                edge_bindings[ek] = (s, preds[0], o)
                new_edges.append(ek)

            if valid:
                _backtrack(node_bindings, edge_bindings)

            # Undo bindings (backtrack).
            del node_bindings[next_key]
            for ek in new_edges:
                edge_bindings.pop(ek, None)

    # Launch search from each start candidate.
    for curie in start_candidates:
        if len(results) >= limit:
            break
        _backtrack({start_key: curie}, {})

    return results


def _get_candidates(
    graph: CSRGraph,
    db: Any,
    qnode: dict,
) -> list[str]:
    """Return candidate CURIEs for a QNode."""
    ids = qnode.get("ids")
    if ids:
        return [c for c in ids if c in graph.node_to_id]

    categories = qnode.get("categories")
    if categories:
        matched = db.filter_nodes(
            list(graph.node_to_id.keys()),
            category=categories[0],
        )
        return [m["id"] for m in matched if m["id"] in graph.node_to_id]

    # Unconstrained — cannot enumerate all nodes. Return empty.
    return []


def _filter_by_qnode(
    graph: CSRGraph,
    db: Any,
    qnode: dict,
    candidates: list[str],
) -> list[str]:
    """Filter candidate CURIEs by QNode ids, categories, and constraints."""
    if not candidates:
        return []

    ids = qnode.get("ids")
    if ids:
        id_set = set(ids)
        candidates = [c for c in candidates if c in id_set]

    categories = qnode.get("categories")
    if categories and candidates:
        # OR across multiple categories.
        matched_ids: set[str] = set()
        for cat in categories:
            matched = db.filter_nodes(candidates, category=cat)
            matched_ids.update(m["id"] for m in matched)
        candidates = [c for c in candidates if c in matched_ids]

    # Apply node constraints.
    constraints = qnode.get("constraints")
    if constraints and candidates:
        candidates = _apply_node_constraints(db, candidates, constraints)

    return candidates


def _get_edge_neighbors(
    graph: CSRGraph,
    qedge: dict,
    source_curie: str,
    forward: bool,
) -> list[str]:
    """Get neighbor CURIEs reachable from source_curie along a QEdge.

    When forward=True, source is the subject; we return objects.
    When forward=False, source is the object; we look for nodes that have
    edges pointing TO source (i.e., source is the object, we need subjects).

    For symmetric predicates, both directions are searched automatically.
    """
    predicates = qedge.get("predicates")
    has_symmetric = predicates and any(p in SYMMETRIC_PREDICATES for p in predicates)

    if forward:
        result: set[str] = set()
        if predicates:
            for pred in predicates:
                result.update(graph.neighbors(source_curie, relation=pred))
        else:
            result.update(graph.neighbors(source_curie))
        # Symmetric: also get reverse neighbors (nodes pointing TO source).
        if has_symmetric:
            result.update(_reverse_neighbors(graph, source_curie, predicates))
        return list(result)

    # Reverse direction.
    result_set = set(_reverse_neighbors(graph, source_curie, predicates))
    # Symmetric: also get forward neighbors.
    if has_symmetric:
        if predicates:
            for pred in predicates:
                result_set.update(graph.neighbors(source_curie, relation=pred))
        else:
            result_set.update(graph.neighbors(source_curie))
    return list(result_set)


def _reverse_neighbors(
    graph: CSRGraph,
    target_curie: str,
    predicates: list[str] | None,
) -> list[str]:
    """Find nodes with edges pointing TO target_curie (reverse lookup)."""
    if target_curie not in graph.node_to_id:
        return []
    v = graph.node_to_id[target_curie]

    result: set[str] = set()
    relations_to_check = (
        [_strip_biolink(p) for p in predicates]
        if predicates else list(graph.csr_by_relation.keys())
    )
    for rel in relations_to_check:
        csr = graph.csr_by_relation.get(rel)
        if csr is None:
            continue
        col = csr.getcol(v)
        for u in col.nonzero()[0]:
            result.add(graph.nodes[int(u)])
    return list(result)


def _matching_predicates(
    graph: CSRGraph,
    qedge: dict,
    source: str,
    target: str,
) -> list[str]:
    """Return predicates of actual edges from source→target that match QEdge.

    For symmetric predicates, also checks the reverse direction (target→source).
    """
    actual_preds = graph.edges_between(source, target)

    allowed = qedge.get("predicates")
    has_symmetric = allowed and any(p in SYMMETRIC_PREDICATES for p in allowed)

    # Also check reverse for symmetric predicates.
    if has_symmetric:
        reverse_preds = graph.edges_between(target, source)
        # Combine, marking reverse preds (they still match for symmetric).
        actual_preds = list(set(actual_preds) | set(reverse_preds))

    if not actual_preds:
        return []

    if allowed:
        allowed_set = set(allowed)
        return [p for p in actual_preds if p in allowed_set]

    return actual_preds


# ──────────────────────────────────────────────────────────────────────────────
# Path-spec construction (for linear fast path)
# ──────────────────────────────────────────────────────────────────────────────

def _qnode_to_spec(qnode: dict) -> str | dict | None:
    """Convert a QNode to a match_path NodeSpec."""
    ids = qnode.get("ids")
    if ids and len(ids) == 1:
        return ids[0]

    categories = qnode.get("categories")
    if categories:
        return {"category": categories[0]}

    if ids and len(ids) > 1:
        return ids[0]

    return None


def _qedge_to_spec(qedge: dict, is_forward: bool) -> str | dict | None:
    """Convert a QEdge to a match_path EdgeSpec.

    Multiple predicates → wildcard (None); post-filtered in _linear_query.
    attribute_constraints are handled by _apply_edge_attribute_constraints
    post-filter, NOT embedded in the EdgeSpec.
    """
    predicates = qedge.get("predicates")

    if predicates and len(predicates) == 1:
        return predicates[0]

    # Multiple predicates → use wildcard; post-filter handles the OR logic.
    # No predicates → wildcard.
    return None


def _build_path_spec(
    ordered_node_keys: list[str],
    ordered_edge_keys: list[str],
    qnodes: dict[str, dict],
    qedges: dict[str, dict],
    start_id_override: str | None = None,
) -> list:
    """Build a match_path path_spec from the linearised query chain."""
    spec: list = []
    for i, nk in enumerate(ordered_node_keys):
        if i == 0 and start_id_override is not None:
            spec.append(start_id_override)
        else:
            spec.append(_qnode_to_spec(qnodes[nk]))
        if i < len(ordered_edge_keys):
            ek = ordered_edge_keys[i]
            qe = qedges[ek]
            is_forward = (qe["subject"] == nk)
            spec.append(_qedge_to_spec(qe, is_forward))
    return spec


# ──────────────────────────────────────────────────────────────────────────────
# Node constraint post-filtering (applied to bindings from both paths)
# ──────────────────────────────────────────────────────────────────────────────

def _apply_node_constraint_filters(
    graph: CSRGraph,
    bindings: list[Binding],
    qnodes: dict[str, dict],
) -> list[Binding]:
    """Post-filter bindings by QNode constraints."""
    # Collect nodes with constraints.
    constrained: dict[str, list[dict]] = {}
    for nk, qn in qnodes.items():
        cs = qn.get("constraints")
        if cs:
            constrained[nk] = cs

    if not constrained:
        return bindings

    db = graph._require_db()
    filtered: list[Binding] = []

    for binding in bindings:
        keep = True
        for nk, constraints in constrained.items():
            curie = binding["nodes"].get(nk)
            if curie is None:
                keep = False
                break
            meta = db.get_node(curie)
            for c in constraints:
                if not _eval_constraint(meta, c):
                    keep = False
                    break
            if not keep:
                break
        if keep:
            filtered.append(binding)

    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# Constraint evaluation (node constraints + edge attribute_constraints)
# ──────────────────────────────────────────────────────────────────────────────

def _apply_node_constraints(
    db: Any,
    candidates: list[str],
    constraints: list[dict],
) -> list[str]:
    """Filter candidates by TRAPI AttributeConstraint list (ANDed)."""
    result = candidates
    for constraint in constraints:
        if not result:
            break
        result = [c for c in result if _eval_constraint(db.get_node(c), constraint)]
    return result


def _eval_constraint(meta: dict, constraint: dict) -> bool:
    """Evaluate a single TRAPI AttributeConstraint against metadata.

    Supports operators: ``==``, ``>``, ``<``, ``matches`` (regex), ``in``.
    The ``not`` flag negates the result.
    """
    field = constraint.get("id", "")
    # Strip biolink: prefix for metadata field lookup.
    field_name = field.replace("biolink:", "") if field.startswith("biolink:") else field
    operator = constraint.get("operator", "==")
    expected = constraint.get("value")
    negate = constraint.get("not", False)

    actual = meta.get(field_name)

    result = _eval_operator(actual, operator, expected)
    return (not result) if negate else result


def _eval_operator(actual: Any, operator: str, expected: Any) -> bool:
    """Evaluate a comparison operator."""
    if actual is None:
        return False

    if operator == "==":
        # For lists, acts like SQL IN (any element matches).
        if isinstance(actual, list):
            return expected in actual
        return str(actual) == str(expected)

    if operator == "===":
        return actual == expected

    if operator == ">":
        try:
            return float(actual) > float(expected)
        except (TypeError, ValueError):
            return False

    if operator == "<":
        try:
            return float(actual) < float(expected)
        except (TypeError, ValueError):
            return False

    if operator == "matches":
        # Value is a regex pattern, possibly with /pattern/flags syntax.
        pattern = str(expected)
        flags = 0
        if pattern.startswith("/"):
            # Parse /pattern/flags format.
            last_slash = pattern.rfind("/")
            if last_slash > 0:
                flag_str = pattern[last_slash + 1:]
                pattern = pattern[1:last_slash]
                if "i" in flag_str:
                    flags = re.IGNORECASE
        target = str(actual) if not isinstance(actual, list) else " ".join(str(v) for v in actual)
        try:
            return bool(re.search(pattern, target, flags))
        except re.error:
            return False

    if operator == "in":
        # Check if expected value is contained in actual (list or string).
        if isinstance(actual, list):
            return expected in actual
        return str(expected) in str(actual)

    return False


def _apply_edge_attribute_constraints(
    graph: CSRGraph,
    bindings: list[Binding],
    qedges: dict[str, dict],
) -> list[Binding]:
    """Post-filter bindings by QEdge attribute_constraints."""
    constrained: dict[str, list[dict]] = {}
    for ek, qe in qedges.items():
        acs = qe.get("attribute_constraints", [])
        if acs:
            constrained[ek] = acs

    if not constrained:
        return bindings

    db = graph._require_db()
    filtered: list[Binding] = []

    for binding in bindings:
        keep = True
        for ek, constraints in constrained.items():
            edge = binding["edges"].get(ek)
            if edge is None:
                keep = False
                break
            subj, pred, obj = edge
            edge_meta = db.get_edge(subj, pred, obj)
            for ac in constraints:
                if not _eval_constraint(edge_meta, ac):
                    keep = False
                    break
            if not keep:
                break
        if keep:
            filtered.append(binding)

    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# Qualifier post-filtering
# ──────────────────────────────────────────────────────────────────────────────

def _apply_qualifier_filters(
    graph: CSRGraph,
    bindings: list[Binding],
    qedges: dict[str, dict],
) -> list[Binding]:
    """Post-filter bindings by QEdge qualifier_constraints."""
    # Collect edges with qualifier constraints.
    constrained_edges: dict[str, list[list[dict]]] = {}
    for ek, qe in qedges.items():
        qcs = qe.get("qualifier_constraints", [])
        if qcs:
            constrained_edges[ek] = [qc["qualifier_set"] for qc in qcs]

    if not constrained_edges:
        return bindings

    db = graph._require_db()
    filtered: list[Binding] = []

    for binding in bindings:
        keep = True
        for ek, qualifier_sets in constrained_edges.items():
            edge = binding["edges"].get(ek)
            if edge is None:
                keep = False
                break
            subj, pred, obj = edge
            edge_meta = db.get_edge(subj, pred, obj)
            if not _matches_any_qualifier_set(edge_meta, qualifier_sets):
                keep = False
                break
        if keep:
            filtered.append(binding)

    return filtered


def _matches_any_qualifier_set(
    edge_meta: dict,
    qualifier_sets: list[list[dict]],
) -> bool:
    """OR across qualifier sets."""
    return any(_matches_qualifier_set(edge_meta, qs) for qs in qualifier_sets)


def _matches_qualifier_set(edge_meta: dict, qualifiers: list[dict]) -> bool:
    """AND within a qualifier set."""
    for q in qualifiers:
        qtype = q.get("qualifier_type_id", "")
        qval = q.get("qualifier_value", "")
        field = _QUALIFIER_TYPE_TO_FIELD.get(qtype)
        if field is None:
            return False
        actual = edge_meta.get(field)
        if actual is None or str(actual) != str(qval):
            return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# TRAPI response assembly (works with bindings from both paths)
# ──────────────────────────────────────────────────────────────────────────────

def _build_message(
    graph: CSRGraph,
    query_graph: dict,
    bindings: list[Binding],
) -> dict:
    """Assemble a TRAPI Message from bindings."""
    db = graph.db
    kg_nodes: dict[str, dict] = {}
    kg_edges: dict[str, dict] = {}
    # Map each distinct (subject, predicate, object) to a stable, unique edge
    # id.  A delimiter-joined string ("subj-pred-obj") is unsafe because CURIEs
    # and predicates contain hyphens, so distinct edges could collide and
    # overwrite each other; keying on the tuple avoids that entirely.
    edge_id_by_tuple: dict[tuple[str, str, str], str] = {}
    results: list[dict] = []

    for binding in bindings:
        node_bindings: dict[str, list[dict]] = {}
        edge_bindings: dict[str, list[dict]] = {}

        for nk, curie in binding["nodes"].items():
            node_bindings[nk] = [{"id": curie, "attributes": []}]
            if curie not in kg_nodes:
                kg_nodes[curie] = _make_kg_node(db, curie)

        for ek, edge_tuple in binding["edges"].items():
            subj, pred, obj = edge_tuple
            key = (subj, pred, obj)
            edge_id = edge_id_by_tuple.get(key)
            if edge_id is None:
                edge_id = f"e{len(edge_id_by_tuple)}"
                edge_id_by_tuple[key] = edge_id
                kg_edges[edge_id] = _make_kg_edge(db, subj, pred, obj)
            edge_bindings[ek] = [{"id": edge_id, "attributes": []}]

        results.append({
            "node_bindings": node_bindings,
            "analyses": [{
                "resource_id": RESOURCE_ID,
                "edge_bindings": edge_bindings,
            }],
        })

    return {
        "query_graph": query_graph,
        "knowledge_graph": {
            "nodes": kg_nodes,
            "edges": kg_edges,
        },
        "results": results,
    }


def _make_kg_node(db: Any, curie: str) -> dict:
    """Create a TRAPI KnowledgeGraph Node from metadata."""
    node: dict[str, Any] = {"categories": [], "attributes": []}
    if db is not None:
        meta = db.get_node(curie)
        if meta:
            node["name"] = meta.get("name")
            cats = meta.get("category", [])
            if isinstance(cats, str):
                cats = [cats]
            node["categories"] = cats
            for k, v in meta.items():
                if k not in ("id", "name", "category"):
                    node["attributes"].append({
                        "attribute_type_id": f"biolink:{k}" if ":" not in k else k,
                        "value": v,
                    })
    return node


def _make_kg_edge(db: Any, subj: str, pred: str, obj: str) -> dict:
    """Create a TRAPI KnowledgeGraph Edge from metadata."""
    edge: dict[str, Any] = {
        "subject": subj,
        "predicate": pred or "biolink:related_to",
        "object": obj,
        "sources": [{
            "resource_id": RESOURCE_ID,
            "resource_role": "primary_knowledge_source",
        }],
        "attributes": [],
    }
    if db is not None:
        meta = db.get_edge(subj, pred, obj)
        if meta:
            if meta.get("knowledge_level"):
                edge["attributes"].append({
                    "attribute_type_id": "biolink:knowledge_level",
                    "value": meta["knowledge_level"],
                })
            if meta.get("agent_type"):
                edge["attributes"].append({
                    "attribute_type_id": "biolink:agent_type",
                    "value": meta["agent_type"],
                })
            qualifiers = []
            for qfield, qtype_id in [
                ("qualified_predicate", "biolink:qualified_predicate"),
                ("object_aspect_qualifier", "biolink:object_aspect_qualifier"),
                ("object_direction_qualifier", "biolink:object_direction_qualifier"),
                ("subject_aspect_qualifier", "biolink:subject_aspect_qualifier"),
                ("subject_direction_qualifier", "biolink:subject_direction_qualifier"),
                ("causal_mechanism_qualifier", "biolink:causal_mechanism_qualifier"),
            ]:
                val = meta.get(qfield)
                if val:
                    qualifiers.append({
                        "qualifier_type_id": qtype_id,
                        "qualifier_value": str(val),
                    })
            if qualifiers:
                edge["qualifiers"] = qualifiers
            skip = {
                "subject", "predicate", "object",
                "knowledge_level", "agent_type",
                "qualified_predicate",
                "object_aspect_qualifier", "object_direction_qualifier",
                "subject_aspect_qualifier", "subject_direction_qualifier",
                "causal_mechanism_qualifier",
            }
            for k, v in meta.items():
                if k not in skip:
                    edge["attributes"].append({
                        "attribute_type_id": f"biolink:{k}" if ":" not in k else k,
                        "value": v,
                    })
    return edge
