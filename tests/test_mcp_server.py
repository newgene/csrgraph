"""MCP tool boundary — data-free, driven through a real ``call_tool()``.

These go through ``MCPServer.call_tool()`` rather than calling the tool functions
directly, and that is the whole point. Under mcp >= 2 the SDK forwards a
``ToolError`` message to the client but replaces every other exception's text
with a bare ``Error executing tool <name>`` (``tools/base.py``: ``except
Exception`` -> ``UnexpectedToolError``). So ``graph_query`` raising ``ValueError``
with a carefully worded, triple-level message delivered *nothing* -- and a unit
test on ``kg_pattern.run()`` still passed, because the library was never at
fault. Only a round trip through the boundary shows it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("mcp", reason="the MCP server needs the optional 'mcp' extra")

import kg_pattern as kp  # noqa: E402
import mcp_server as ms  # noqa: E402
from mcp.server.mcpserver.exceptions import (  # noqa: E402
    ToolError,
    UnexpectedToolError,
)
from tests.test_trapi import simple_graph  # noqa: E402,F401  (fixture)


@pytest.fixture
def server(simple_graph, monkeypatch):  # noqa: F811
    """The module wired to the synthetic graph, so no real data is needed.

    ``_g()`` returns ``_graph`` when already set, so setting it here skips the
    loader entirely.
    """
    monkeypatch.setattr(ms, "_graph", simple_graph)
    return ms


def call(tool: str, args: dict):
    """Invoke a tool the way a client does, returning whatever it raises."""
    return asyncio.run(ms.mcp.call_tool(tool, args))


class TestAnticipatedFailuresReachTheClient:
    """The three documented ``graph_query`` errors, verbatim from the skill file."""

    @pytest.mark.parametrize("pattern, expected", [
        ([["?a", None, "?b"]],
         "pattern needs at least one pinned node"),
        ([["CHEBI:1", "affects"]],
         "triple 0: expected [subject, predicate, object], got 2 element(s)"),
        ([["CHEBI:1", {"bogus": "x"}, "?d"]],
         "triple 0: unknown edge field 'bogus'"),
    ])
    def test_pattern_errors_carry_their_message(self, server, pattern, expected):
        with pytest.raises(ToolError) as err:
            call("graph_query", {"pattern": pattern})
        assert expected in str(err.value)

    def test_unresolvable_name_says_so(self, server):
        """Anticipated: the pattern names an entity and no resolver is available."""
        with pytest.raises(ToolError) as err:
            call("graph_query", {"pattern": [["DrugA", "affects", "?g"]]})
        assert "is a name, not a CURIE" in str(err.value)

    def test_resolve_entity_without_es_explains_itself(self, server):
        """The docstring promises graceful degradation; the message is how."""
        with pytest.raises(ToolError) as err:
            call("resolve_entity", {"text": "DrugA"})
        assert "needs Elasticsearch" in str(err.value)

    @pytest.mark.parametrize("tool, args", [
        ("list_neighbors", {"entity": "FAKE:12345"}),
        ("connect_entities", {"source": "FAKE:1", "target": "FAKE:2"}),
    ])
    def test_unknown_curie_names_the_curie(self, server, tool, args):
        with pytest.raises(ToolError) as err:
            call(tool, args)
        assert "Unknown" in str(err.value) and "FAKE:" in str(err.value)

    def test_errors_are_tool_errors_not_crashes(self, server):
        """A crash is logged with a traceback and withholds its text; these must not be."""
        with pytest.raises(ToolError) as err:
            call("graph_query", {"pattern": [["?a", None, "?b"]]})
        assert not isinstance(err.value, UnexpectedToolError)


class TestGenuineBugsStayCrashes:
    """``_ANTICIPATED`` is narrow on purpose.

    Converting every exception would tell the model its *input* was wrong when
    the real fault is in this file, and would hide the traceback the server
    should be logging.
    """

    def test_unexpected_type_is_not_converted(self):
        @ms._tool
        def boom():
            raise TypeError("a defect, not a bad request")

        with pytest.raises(TypeError):
            boom()

    @pytest.mark.parametrize("exc", [
        kp.PatternError("bad pattern"),
        ValueError("Unknown node: X:1"),
        RuntimeError("needs Elasticsearch"),
    ])
    def test_anticipated_types_are_converted(self, exc):
        @ms._tool
        def boom():
            raise exc

        with pytest.raises(ToolError, match=str(exc)):
            boom()

    def test_anticipated_tuple_excludes_blanket_exception(self):
        assert Exception not in ms._ANTICIPATED
        assert KeyError not in ms._ANTICIPATED
        assert TypeError not in ms._ANTICIPATED


class TestSuccessPathIsUnaffected:
    def test_a_working_query_still_returns_rows(self, server, monkeypatch):
        # test_trapi's stub backend is neither LMDB nor ES, and names() requires
        # one of the two; stub it so this exercises the tool boundary rather than
        # the metadata backend.
        monkeypatch.setattr(ms.kq, "names", lambda g, curies: {})
        out = asyncio.run(ms.mcp.call_tool(
            "graph_query",
            {"pattern": [["CHEBI:1", "affects", "?g:Gene"]], "return_vars": ["?g"]},
        ))
        assert out is not None

    def test_wrapping_preserves_the_input_schemas(self, server):
        """@_tool must not flatten signatures into (*args, **kwargs).

        ``mcp.tool`` derives each schema from the wrapped function, so a
        decorator without ``functools.wraps`` would silently reduce every tool to
        no declared arguments -- and the model would stop passing any.
        """
        tools = {t.name: t for t in asyncio.run(ms.mcp.list_tools())}
        props = tools["find_associations"].input_schema["properties"]
        assert {"entity", "target_category", "max_hops", "limit"} <= set(props)
        assert tools["graph_info"].input_schema.get("properties", {}) == {}
