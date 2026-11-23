"""Client <-> server tests over the real stdio transport (the server runs as a child process)."""

import json
import os

import pytest

from src.client.agent_loop import LLMReply, ToolCall, ask, openai_llm, run_agent
from src.client.mcp_client import MCPToolClient, to_openai_tools

EXPECTED_TOOLS = {"query_database", "get_current_weather", "calculate_expression"}


# ---------------------------------------------------------------- Test 4: dynamic discovery
async def test_dynamic_tool_discovery_over_stdio():
    async with MCPToolClient() as client:
        tools = client.tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        schema = t.inputSchema
        assert schema["type"] == "object"
        assert len(schema["required"]) == 1
        (param,) = schema["required"]
        assert schema["properties"][param]["type"] == "string"
        assert len(t.description) > 40  # the LLM needs a real description
    sql = next(t for t in tools if t.name == "query_database")
    assert "inventory(" in sql.description  # schema is advertised to the model


async def test_mcp_schema_translates_to_openai_format():
    async with MCPToolClient() as client:
        openai_tools = to_openai_tools(client.tools)
    assert len(openai_tools) == 3
    for t in openai_tools:
        assert t["type"] == "function"
        assert set(t["function"]) == {"name", "description", "parameters"}
        assert t["function"]["parameters"]["type"] == "object"
        json.dumps(t)  # must be serializable for the API


# ---------------------------------------------------------------- Test 5: chained multi-step loop
def scripted_llm(script):
    """Fake LLM that replays a fixed list of replies and records what it was sent."""
    seen = []

    async def llm(messages, tools):
        seen.append([dict(m) for m in messages])
        return script[len(seen) - 1](messages)

    llm.seen = seen
    return llm


async def test_multi_step_chained_loop_sql_then_math():
    def turn1(_):
        return LLMReply(None, [ToolCall("call_sql", "query_database",
                                        json.dumps({"sql_query": "SELECT order_value FROM orders WHERE order_id = 4492"}))])

    def turn2(messages):
        value = json.loads(messages[-1]["content"])["rows"][0][0]  # read the SQL result
        return LLMReply(None, [ToolCall("call_math", "calculate_expression",
                                        json.dumps({"expression": f"{value} * 0.035"}))])

    def turn3(messages):
        insurance = json.loads(messages[-1]["content"])["result"]
        return LLMReply(f"Shipping insurance for order 4492 is ${insurance:,.2f}.")

    llm = scripted_llm([turn1, turn2, turn3])
    async with MCPToolClient() as client:
        result = await run_agent("Insurance at 3.5% for order 4492?", client, to_openai_tools(client.tools), llm)

    assert result.stopped_reason == "final_answer" and result.turns <= 3
    assert result.messages[-1]["role"] == "assistant"
    assert "$795.90" in result.answer
    assert [s.name for s in result.steps] == ["query_database", "calculate_expression"]
    # every tool message answers the matching tool_call_id
    tool_msgs = [m for m in result.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_sql", "call_math"]


async def test_parallel_tool_calls_in_one_turn_each_get_a_reply():
    def turn1(_):
        return LLMReply(None, [
            ToolCall("a", "calculate_expression", '{"expression": "1+1"}'),
            ToolCall("b", "calculate_expression", '{"expression": "2*3"}'),
        ])

    llm = scripted_llm([turn1, lambda m: LLMReply("2 and 6")])
    async with MCPToolClient() as client:
        result = await run_agent("two sums", client, to_openai_tools(client.tools), llm)
    assert [m["tool_call_id"] for m in result.messages if m["role"] == "tool"] == ["a", "b"]
    assert result.turns == 2


async def test_max_turns_stops_an_endless_tool_loop():
    always_tool = lambda m: LLMReply(None, [ToolCall(f"c{len(m)}", "calculate_expression", '{"expression": "1"}')])
    llm = scripted_llm([always_tool] * 10)
    async with MCPToolClient() as client:
        result = await run_agent("loop forever", client, to_openai_tools(client.tools), llm, max_turns=3)
    assert result.stopped_reason == "max_turns"
    assert len(llm.seen) == 3 and len(result.steps) == 3
    assert result.messages[-1]["role"] == "assistant"


async def test_hallucinated_tool_and_bad_json_are_fed_back_not_crashed():
    def turn1(_):
        return LLMReply(None, [ToolCall("x1", "delete_all_orders", "{}"),
                               ToolCall("x2", "calculate_expression", "{not json")])

    def turn2(messages):
        errors = [json.loads(m["content"])["error"] for m in messages if m["role"] == "tool"]
        assert "Unknown tool" in errors[0] and "Invalid JSON" in errors[1]
        return LLMReply("I could not run those tools.")

    llm = scripted_llm([turn1, turn2])
    async with MCPToolClient() as client:
        result = await run_agent("break things", client, to_openai_tools(client.tools), llm)
        assert await client.call_tool("calculate_expression", {"expression": "1+1"})  # server still alive
    assert all(s.is_error for s in result.steps)
    assert result.stopped_reason == "final_answer"


# ---------------------------------------------------------------- live (real OpenAI model)
@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY", "").strip(), reason="needs OPENAI_API_KEY")
async def test_live_llm_chains_sql_and_math():
    result, _ = await ask("Look up the order value of order #4492 and calculate 3.5% shipping insurance on it.",
                          llm=openai_llm())
    names = [s.name for s in result.steps]
    assert "query_database" in names and "calculate_expression" in names
    assert "795.9" in result.answer.replace(",", "")


async def test_sql_tool_teaches_exact_value_formats_and_hints_on_zero_rows():
    """Regression: gpt-4o-mini once queried sku = '90210' (no prefix), got 0 rows and
    told the operator there was no stock. The description now shows real value formats,
    and an empty result carries a hint to re-check the filter."""
    async with MCPToolClient() as client:
        sql = next(t for t in client.tools if t.name == "query_database")
        assert "'SKU-90210'" in sql.description and "'Seattle'" in sql.description
        miss = json.loads((await client.call_tool(
            "query_database", {"sql_query": "SELECT quantity FROM inventory WHERE sku = '90210'"})).text)
        hit = json.loads((await client.call_tool(
            "query_database", {"sql_query": "SELECT quantity FROM inventory WHERE sku = 'SKU-90210' AND warehouse = 'Seattle'"})).text)
    assert miss["row_count"] == 0 and "SKU-90210" in miss["hint"]
    assert hit["rows"] == [[140]] and "hint" not in hit
