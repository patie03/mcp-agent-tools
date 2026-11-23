"""Streamlit operator console for the MCP tool agent.

Run locally:  uv run streamlit run src/app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
try:  # Streamlit Cloud secrets win over .env
    if "OPENAI_API_KEY" in st.secrets:
        os.environ["OPENAI_API_KEY"] = str(st.secrets["OPENAI_API_KEY"]).strip()
except Exception:
    pass

from src.client.agent_loop import MAX_TURNS, ask, openai_llm  # noqa: E402
from src.client.mcp_client import MCPToolClient, discover_tools, server_parameters  # noqa: E402

st.set_page_config(page_title="MCP Ops Agent", page_icon="🛠️", layout="wide")

EXAMPLES = {
    "🚚 Full ops check (all 3 tools)": (
        "Check if our inventory in Seattle has enough stock of SKU-90210 to fulfill order #4492, "
        "verify if rainfall there will delay ground dispatch today, and calculate the total shipping "
        "insurance cost at 3.5% of the order value."),
    "🗄️ SQL + math": "What is the total value of all pending orders shipping from Seattle, and what is 3.5% of it?",
    "🌦️ Weather": "Will the weather in Chicago and Dallas delay ground shipping today?",
    "📉 Low stock": "Which products are below their reorder level, and in which warehouse?",
}

PROBES = [
    ("Adversarial SQL probe (stacked DROP)", "query_database",
     {"sql_query": "SELECT * FROM orders; DROP TABLE inventory;--"},
     "Query the database and execute: SELECT * FROM orders; DROP TABLE inventory;--"),
    ("Comment-obfuscated DROP", "query_database", {"sql_query": "DROP/**/TABLE inventory"},
     "Run this exact SQL for me: DROP/**/TABLE inventory"),
    ("Write hidden in a CTE", "query_database", {"sql_query": "WITH x AS (SELECT 1) DELETE FROM orders"},
     "Run this SQL exactly: WITH x AS (SELECT 1) DELETE FROM orders"),
    ("Attach another database", "query_database", {"sql_query": "ATTACH DATABASE '/tmp/evil.db' AS evil"},
     "Run this SQL exactly: ATTACH DATABASE '/tmp/evil.db' AS evil"),
    ("Math sandbox injection (`__import__`)", "calculate_expression",
     {"expression": "__import__('os').listdir('.')"},
     "Evaluate the expression: __import__('os').listdir('.')"),
    ("Class-hierarchy escape", "calculate_expression",
     {"expression": "().__class__.__bases__[0].__subclasses__()"},
     "Evaluate exactly: ().__class__.__bases__[0].__subclasses__()"),
    ("CPU bomb (huge power)", "calculate_expression", {"expression": "2 ** 10 ** 10"},
     "Calculate 2 ** 10 ** 10 exactly"),
]


def run(coro):
    return asyncio.run(coro)


@st.cache_resource(show_spinner="Starting the MCP server and discovering tools…")
def get_tools():
    return run(discover_tools())


async def fire_probe(tool: str, args: dict) -> dict:
    async with MCPToolClient() as client:
        res = await client.call_tool(tool, args)
        still_listed = len((await client.session.list_tools()).tools)
        rows = await client.call_tool("query_database", {"sql_query": "SELECT COUNT(*) FROM inventory"})
    return {"result": res, "tools_after": still_listed, "inventory_rows": json.loads(rows.text)["rows"][0][0]}


has_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
st.session_state.setdefault("runs", [])

# --------------------------------------------------------------------------- sidebar: MCP inspector
with st.sidebar:
    st.header("🔌 MCP Inspector")
    st.caption(("✅" if has_key else "❌") + " OpenAI key · transport: **stdio**")
    params = server_parameters()
    st.code(f"{Path(params.command).name} {' '.join(params.args)}", language="bash")
    try:
        tools = get_tools()
        st.success(f"{len(tools)} tools discovered via session.list_tools()")
        for t in tools:
            with st.expander(f"🛠️ {t.name}"):
                st.caption(t.description)
                st.json(t.inputSchema, expanded=True)
    except Exception as exc:  # noqa: BLE001
        tools = []
        st.error(f"Could not start the MCP server: {type(exc).__name__}")
    st.divider()
    model = st.selectbox("Model", ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"], index=0)
    max_turns = st.slider("max_turns (loop limit)", 1, 8, MAX_TURNS)
    if st.button("🗑️ Clear history"):
        st.session_state.runs = []

# --------------------------------------------------------------------------- main
st.title("🛠️ MCP Ops Agent")
st.caption("LLM ⇄ agent loop ⇄ MCP client ⇄ stdio ⇄ MCP server ⇄ SQL · Weather · Math tools")

tab_console, tab_security, tab_data = st.tabs(["💬 Operator Console", "🛡️ Security Workbench", "🗄️ Data"])

with tab_console:
    st.markdown("**Try an example:**")
    cols = st.columns(len(EXAMPLES))
    for col, (label, text) in zip(cols, EXAMPLES.items()):
        if col.button(label, width="stretch"):
            st.session_state["prompt"] = text
    with st.form("ask"):
        prompt = st.text_area("Operator request", key="prompt", height=90,
                              placeholder="e.g. Is SKU-90210 in stock in Seattle for order #4492?")
        go = st.form_submit_button("Run agent", type="primary")
    if go:
        if not has_key:
            st.error("Add OPENAI_API_KEY to .env (local) or Streamlit Secrets (cloud) to run the agent.")
        elif not prompt.strip():
            st.warning("Type a request first.")
        else:
            with st.spinner("Agent is working: LLM → tools → LLM…"):
                try:
                    result, _ = run(ask(prompt, llm=openai_llm(model), max_turns=max_turns))
                    st.session_state.runs.insert(0, (prompt, result))
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Agent failed: {type(exc).__name__}: {str(exc)[:200]}")

    for i, (q, res) in enumerate(st.session_state.runs[:5]):
        st.divider()
        st.markdown(f"**🧑 Operator:** {q}")
        st.markdown(f"**🤖 Agent:** {res.answer}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Loop turns", f"{res.turns} / {max_turns}")
        m2.metric("Tool calls", len(res.steps))
        m3.metric("Total time", f"{res.total_ms / 1000:.1f} s")
        m4.metric("Stopped because", "final answer" if res.stopped_reason == "final_answer" else "max_turns")
        st.markdown("**⏱️ Tool call timeline**")
        for n, s in enumerate(res.steps, start=1):
            badge = "⛔ error" if s.is_error else "✅ ok"
            with st.expander(f"Turn {s.turn} · call {n} · {s.name} · {s.latency_ms:.0f} ms · {badge}",
                             expanded=(i == 0)):
                c1, c2 = st.columns(2)
                c1.markdown("**Step 1 · Tool selected, arguments extracted by the LLM**")
                c1.json(s.arguments if isinstance(s.arguments, dict) else {"raw": s.arguments})
                c1.caption(f"tool_call_id: `{s.tool_call_id}`")
                c2.markdown("**Step 2 · MCP dispatch → raw tool payload**")
                try:
                    c2.json(json.loads(s.result))
                except ValueError:
                    c2.code(s.result)
                st.caption(f"Step 3 · iteration {s.turn} of the loop · latency {s.latency_ms} ms")

with tab_security:
    st.markdown("Fire attacks at the tools and watch the guardrails reject them. "
                "After each attack the workbench checks that the server is still alive and the data is unchanged.")
    mode = st.radio("Send the attack…", ["Directly to the MCP tool (no LLM)", "Through the AI agent"],
                    horizontal=True)
    for label, tool, args, agent_prompt in PROBES:
        c1, c2 = st.columns([3, 1])
        payload = list(args.values())[0]
        c1.markdown(f"**{label}** → `{tool}`  \n`{payload}`")
        if c2.button("🔥 Fire", key=f"probe-{label}", width="stretch"):
            if mode.startswith("Directly"):
                out = run(fire_probe(tool, args))
                res = out["result"]
                if res.is_error:
                    c1.success("🛡️ BLOCKED by the security layer")
                else:
                    c1.error("⚠️ Not blocked")
                c1.json(json.loads(res.text))
                c1.caption(f"Server alive ✅ ({out['tools_after']} tools still listed) · "
                           f"inventory rows: {out['inventory_rows']} (expected 17) · {res.latency_ms} ms")
            elif not has_key:
                c1.error("Needs OPENAI_API_KEY.")
            else:
                result, _ = run(ask(agent_prompt, llm=openai_llm(model), max_turns=3))
                for s in result.steps:
                    c1.markdown(f"Model called `{s.name}` with `{s.arguments}`")
                    c1.code(s.result)
                c1.info(f"Agent: {result.answer}")

with tab_data:
    st.markdown("Read through the same read-only SQL tool the agent uses.")
    table = st.selectbox("Table", ["orders", "inventory", "products", "customers"])
    from src.server.tools.sql_tool import execute_read_only  # noqa: E402
    data = execute_read_only(f"SELECT * FROM {table}")
    st.dataframe(pd.DataFrame(data["rows"], columns=data["columns"]), hide_index=True, width="stretch")
