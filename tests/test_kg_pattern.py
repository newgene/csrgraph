"""Triple-pattern translation and projection — data-free.

Two things are worth testing here rather than assuming. First, that a repeated
variable becomes *one* query-graph node: that identity is the whole reason
patterns can express branching, and if it silently allocated two nodes the query
would still run and return plausible-looking, wrong answers. Second, that every
malformed pattern names the triple at fault, since the caller is a model that has
to repair its own input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kg_pattern as kp  # noqa: E402
import trapi  # noqa: E402
from tests.test_trapi import simple_graph  # noqa: E402,F401  (fixture)


class TestNodeTerms:
    def test_curie_is_pinned(self):
        qg, _ = kp.to_query_graph([["CHEBI:1", None, "?x"]])
        assert qg["nodes"]["n0"] == {"ids": ["CHEBI:1"]}

    def test_biolink_prefix_is_a_category_not_an_id(self):
        """``biolink:Disease`` as an id would match a node named like a category."""
        qg, _ = kp.to_query_graph([["CHEBI:1", None, "biolink:Disease"]])
        assert qg["nodes"]["n1"] == {"categories": ["biolink:Disease"]}

    def test_variable_category_is_normalised(self):
        qg, _ = kp.to_query_graph([["CHEBI:1", None, "?d:Disease"]])
        assert qg["nodes"]["n1"]["categories"] == ["biolink:Disease"]

    @pytest.mark.parametrize("term", [None, "*"])
    def test_wildcard_is_unconstrained(self, term):
        qg, _ = kp.to_query_graph([["CHEBI:1", None, term]])
        assert qg["nodes"]["n1"] == {}

    def test_name_needs_a_resolver(self):
        with pytest.raises(kp.PatternError, match="is a name, not a CURIE"):
            kp.to_query_graph([["DrugA", None, "?x"]])

    def test_name_is_resolved_when_a_resolver_is_given(self):
        qg, _ = kp.to_query_graph(
            [["DrugA", None, "?x"]], resolver=lambda t: "CHEBI:1"
        )
        assert qg["nodes"]["n0"] == {"ids": ["CHEBI:1"]}


class TestVariableSharing:
    def test_repeated_variable_is_one_node(self):
        qg, var_map = kp.to_query_graph([
            ["CHEBI:1", None, "?d:Disease"],
            ["?drug", "treats", "?d"],
        ])
        # Both edges must reference the same qnode for ?d, or this is two
        # independent queries wearing one pattern's clothing.
        assert qg["edges"]["e0"]["object"] == qg["edges"]["e1"]["object"]
        assert qg["edges"]["e0"]["object"] == var_map["d"]
        assert len(qg["nodes"]) == 3        # pinned + ?d + ?drug

    def test_category_can_be_added_on_a_later_mention(self):
        qg, var_map = kp.to_query_graph([
            ["CHEBI:1", None, "?d"],
            ["?drug", "treats", "?d:Disease"],
        ])
        assert qg["nodes"][var_map["d"]]["categories"] == ["biolink:Disease"]

    def test_a_category_is_not_duplicated(self):
        qg, var_map = kp.to_query_graph([
            ["CHEBI:1", None, "?d:Disease"],
            ["?drug", "treats", "?d:Disease"],
        ])
        assert qg["nodes"][var_map["d"]]["categories"] == ["biolink:Disease"]


class TestEdgeTerms:
    @pytest.mark.parametrize("term", [None, "*"])
    def test_wildcard_leaves_predicates_unset(self, term):
        qg, _ = kp.to_query_graph([["CHEBI:1", term, "?x"]])
        assert "predicates" not in qg["edges"]["e0"]

    def test_bare_predicate_gets_the_biolink_prefix(self):
        qg, _ = kp.to_query_graph([["CHEBI:1", "affects", "?x"]])
        assert qg["edges"]["e0"]["predicates"] == ["biolink:affects"]

    def test_list_becomes_a_union(self):
        qg, _ = kp.to_query_graph([["CHEBI:1", ["affects", "biolink:treats"], "?x"]])
        assert qg["edges"]["e0"]["predicates"] == ["biolink:affects", "biolink:treats"]

    def test_qualifiers_become_a_trapi_qualifier_set(self):
        qg, _ = kp.to_query_graph([[
            "CHEBI:1",
            {"predicate": "affects", "object_direction_qualifier": "increased"},
            "?x",
        ]])
        qedge = qg["edges"]["e0"]
        assert qedge["predicates"] == ["biolink:affects"]
        assert qedge["qualifier_constraints"] == [{"qualifier_set": [{
            "qualifier_type_id": "biolink:object_direction_qualifier",
            "qualifier_value": "increased",
        }]}]

    def test_fully_qualified_qualifier_id_also_accepted(self):
        qg, _ = kp.to_query_graph([[
            "CHEBI:1", {"biolink:object_aspect_qualifier": "activity"}, "?x",
        ]])
        qset = qg["edges"]["e0"]["qualifier_constraints"][0]["qualifier_set"]
        assert qset[0]["qualifier_type_id"] == "biolink:object_aspect_qualifier"

    def test_unknown_edge_field_lists_the_valid_ones(self):
        with pytest.raises(kp.PatternError, match="unknown edge field 'nope'"):
            kp.to_query_graph([["CHEBI:1", {"nope": "x"}, "?y"]])

    def test_aliases_are_derived_from_trapi(self):
        """Guards against the alias table drifting from trapi's own mapping."""
        assert set(kp._QUALIFIER_ALIASES.values()) == set(
            trapi._QUALIFIER_TYPE_TO_FIELD
        )


class TestMalformedPatterns:
    def test_empty_pattern(self):
        with pytest.raises(kp.PatternError, match="empty"):
            kp.to_query_graph([])

    def test_wrong_arity_names_the_triple(self):
        with pytest.raises(kp.PatternError, match="triple 1: expected"):
            kp.to_query_graph([["CHEBI:1", None, "?x"], ["CHEBI:1", "affects"]])

    def test_non_string_node_term(self):
        with pytest.raises(kp.PatternError, match="triple 0"):
            kp.to_query_graph([[42, None, "?x"]])

    def test_bare_question_mark_is_rejected(self):
        with pytest.raises(kp.PatternError, match="variable needs a name"):
            kp.to_query_graph([["CHEBI:1", None, "?"]])


class TestQueryTerms:
    def test_collects_predicates_and_qualifier_values(self):
        qg, _ = kp.to_query_graph([
            ["CHEBI:1", {"predicate": "affects",
                         "object_direction_qualifier": "increased"}, "?g"],
            ["?g", "treats", "?d:Disease"],
        ])
        preds, quals = kp._query_terms(qg)
        assert preds == ("biolink:affects", "biolink:treats")
        assert quals == ("increased",)

    def test_no_terms_when_everything_is_a_wildcard(self):
        qg, _ = kp.to_query_graph([["CHEBI:1", None, "?x"]])
        assert kp._query_terms(qg) == ((), ())


class TestRun:
    def test_projects_named_variables(self, simple_graph):  # noqa: F811
        out = kp.run(simple_graph, [["CHEBI:1", "affects", "?g:Gene"]],
                     return_vars=["?g"])
        assert out["columns"] == ["?g"]
        assert sorted(out["rows"]) == [["HGNC:1"], ["HGNC:2"]]
        assert out["truncated"] is False

    def test_defaults_to_every_variable(self, simple_graph):  # noqa: F811
        out = kp.run(simple_graph, [
            ["CHEBI:1", "affects", "?g:Gene"],
            ["?g", None, "?d:Disease"],
        ])
        assert out["columns"] == ["?g", "?d"]

    def test_names_are_applied_when_a_lookup_is_given(self, simple_graph):  # noqa: F811
        out = kp.run(
            simple_graph, [["CHEBI:1", "affects", "?g:Gene"]],
            return_vars=["?g"],
            name_lookup=lambda curies: {"HGNC:1": "GeneA", "HGNC:2": "GeneB"},
        )
        assert sorted(out["rows"]) == [["GeneA (HGNC:1)"], ["GeneB (HGNC:2)"]]

    def test_limit_sets_truncated(self, simple_graph):  # noqa: F811
        out = kp.run(simple_graph, [["CHEBI:1", "affects", "?g:Gene"]],
                     return_vars=["?g"], limit=1)
        assert out["returned"] == 1
        assert out["truncated"] is True

    def test_rows_are_deduped_by_returned_columns(self, simple_graph):  # noqa: F811
        """Two paths differing only in a column we do not return are one answer."""
        out = kp.run(
            simple_graph,
            [["CHEBI:1", "affects", "?g:Gene"], ["?g", None, "?d:Disease"]],
            return_vars=["?x"] if False else ["?g"],
        )
        assert len(out["rows"]) == len(set(map(tuple, out["rows"])))

    def test_unknown_return_var_lists_available(self, simple_graph):  # noqa: F811
        with pytest.raises(kp.PatternError, match="Available: \\['g'\\]"):
            kp.run(simple_graph, [["CHEBI:1", "affects", "?g:Gene"]],
                   return_vars=["?nope"])

    def test_require_pinned_rejects_all_variable_patterns(self, simple_graph):  # noqa: F811
        with pytest.raises(kp.PatternError, match="at least one pinned node"):
            kp.run(simple_graph, [["?a", None, "?b"]])

    def test_require_pinned_counts_a_resolved_name(self, simple_graph):  # noqa: F811
        out = kp.run(simple_graph, [["DrugA", "affects", "?g:Gene"]],
                     resolver=lambda t: "CHEBI:1")
        assert out["returned"] == 2

    def test_malformed_pattern_reports_arity_not_pinning(self, simple_graph):  # noqa: F811
        """Validation must precede the pinned-node check, or the error misleads."""
        with pytest.raises(kp.PatternError, match="expected \\[subject"):
            kp.run(simple_graph, [["?a", None]])


class TestMatchMatchesQuery:
    def test_match_and_query_agree_on_bindings(self, simple_graph):  # noqa: F811
        """trapi.query must stay a projection of trapi.match, not a second path."""
        qg = {
            "nodes": {"n0": {"ids": ["CHEBI:1"]}, "n1": {"categories": ["biolink:Gene"]}},
            "edges": {"e0": {"subject": "n0", "object": "n1"}},
        }
        result = trapi.match(simple_graph, qg)
        message = trapi.query(simple_graph, qg)
        assert len(result.bindings) == len(message["results"])
        assert result.query_graph == qg

    def test_match_returns_the_expanded_query_graph(self, simple_graph):  # noqa: F811
        """With an expander the effective graph differs from the caller's input."""
        qg = {
            "nodes": {"n0": {"ids": ["CHEBI:1"]}, "n1": {}},
            "edges": {"e0": {"subject": "n0", "object": "n1",
                             "predicates": ["biolink:affects"]}},
        }
        expander = trapi.BiolinkExpander(
            predicates={"biolink:affects": frozenset(
                {"biolink:affects", "biolink:affects_sensitivity_to"}
            )}
        )
        result = trapi.match(simple_graph, qg, expander=expander)
        assert set(result.query_graph["edges"]["e0"]["predicates"]) == {
            "biolink:affects", "biolink:affects_sensitivity_to",
        }


class TestBiolinkVersionPinning:
    """A bare version must become a schema URL.

    Passing it straight to ``bmt.Toolkit(schema=...)`` made it look for a local
    *file* named "4.4.2" — so pinning to the graph's version, the one thing this
    parameter exists for, failed with FileNotFoundError while unpinned expansion
    kept working. The failure only appears when someone actually pins.
    """

    @pytest.mark.parametrize("version", ["4.4.2", "v4.4.2"])
    def test_bare_version_becomes_the_tagged_model_url(self, version):
        assert trapi._biolink_schema(version) == (
            "https://raw.githubusercontent.com/biolink/biolink-model/"
            "v4.4.2/biolink-model.yaml"
        )

    def test_single_component_version_is_still_a_version(self):
        assert trapi._biolink_schema("5").endswith("/v5/biolink-model.yaml")

    @pytest.mark.parametrize("schema", [
        "https://example.org/custom-model.yaml",
        "/local/path/biolink-model.yaml",
        "biolink-model.yaml",
    ])
    def test_urls_and_paths_pass_through(self, schema):
        """Pinning to a fork or a local checkout must keep working."""
        assert trapi._biolink_schema(schema) == schema

    def test_none_means_toolkit_default(self):
        assert trapi._biolink_schema(None) is None
        assert trapi._biolink_schema("") is None

    def test_expander_cache_keys_on_the_version(self):
        """Two versions must not share one cached expander."""
        kp._expander_for.cache_clear()
        info = kp._expander_for.cache_info()
        assert info.currsize == 0


class TestCapsAreSeparate:
    """``limit`` bounds what is returned; ``enumerate_limit`` bounds what is found.

    Conflating them is the bug worth guarding against: a small row cap must not
    shrink the *pool* the answers are filtered out of, or a constrained query
    silently loses answers that exist. That is precisely what a too-low
    enumerate_limit did — the qualified-affects pattern reported 357 of its 843
    answers, with ``truncated`` true but no indication of how much was missing.
    """

    def test_row_limit_does_not_shrink_the_match_pool(self, simple_graph):  # noqa: F811
        """matched_paths must reflect the enumeration, not the returned slice."""
        pattern = [["CHEBI:1", "affects", "?g:Gene"]]
        wide = kp.run(simple_graph, pattern, return_vars=["?g"], limit=10**6)
        narrow = kp.run(simple_graph, pattern, return_vars=["?g"], limit=1)
        assert narrow["returned"] == 1
        assert narrow["truncated"] is True
        # Same pool either way — only the slice differs.
        assert narrow["matched_paths"] == wide["matched_paths"]

    def test_enumerate_limit_bounds_the_pool(self, simple_graph):  # noqa: F811
        pattern = [["CHEBI:1", "affects", "?g:Gene"]]
        capped = kp.run(simple_graph, pattern, return_vars=["?g"],
                        limit=10**6, enumerate_limit=1)
        assert capped["matched_paths"] == 1
        assert capped["truncated"] is True

    def test_default_enumerate_limit_is_generous_enough_to_matter(self):
        """A floor, not the exact value — the point is that it is not row-sized.

        Measured plateaus on the 2026-07-19 graph reach 2436; a default in the
        low hundreds would under-answer by more than half.
        """
        assert kp.DEFAULT_ENUMERATE_LIMIT >= 2500

    def test_default_is_env_overridable(self, monkeypatch):
        monkeypatch.setenv("CSRGRAPH_ENUMERATE_LIMIT", "12345")
        import importlib
        import metadata_db
        metadata_db._warned_env.clear()
        reloaded = importlib.reload(kp)
        try:
            assert reloaded.DEFAULT_ENUMERATE_LIMIT == 12345
        finally:
            monkeypatch.delenv("CSRGRAPH_ENUMERATE_LIMIT", raising=False)
            importlib.reload(reloaded)
