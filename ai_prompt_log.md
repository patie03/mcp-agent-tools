# AI Prompt Log & Code Audit

**Student:** Patience Kalanga
**AI assistant used:** Claude (Anthropic), in a chat-based coding session
**Date:** November 2026

## 1. Prompt used

Adapted from the course's sample prompt:

```text
You are a Principal AI Agent Architect specializing in tool integration and the Model Context Protocol (MCP).
Build an end-to-end Python 3.11+ system featuring an MCP server with three distinct tools and a client
application executing an autonomous function-calling loop.

MCP Server (src/server/mcp_server.py) using the official MCP Python SDK (FastMCP):
 - query_database(sql_query: str) -> str: SQLite (data/enterprise_ops.db), STRICT read-only. Block DDL/DML
   (DROP, DELETE, UPDATE, INSERT, ALTER, TRUNCATE, ATTACH). Open SQLite with mode=ro.
 - get_current_weather(city: str) -> str: Open-Meteo geocoding + forecast via httpx.AsyncClient.
 - calculate_expression(expression: str) -> str: NO eval()/exec(). ast.parse with an allow-list visitor.

MCP Client & Agent Loop (src/client/agent_loop.py):
 - stdio transport with mcp.ClientSession, session.list_tools() -> OpenAI tool definitions.
 - Multi-turn loop: send prompt + tools to gpt-4o-mini, run tool_calls with session.call_tool(),
   append role="tool" messages with the matching tool_call_id, stop at a text answer or max_turns=5.
 - Tool failures return formatted error messages instead of crashing the server.

My additions:
 - The data is a fictional logistics company (orders, inventory per warehouse city, products, customers).
 - Tests must run without an API key or internet (mock the weather API, script the LLM),
   but the client tests must use the REAL MCP server over stdio.
 - A Streamlit UI with an MCP inspector, a tool-call timeline and a security workbench.
```

## 2. Audit checklist from the assignment

| Risk | What I checked | Result |
|---|---|---|
| **SQL injection & read-only bypass** | Is it only `if "drop" in query.lower()`? Can comments (`DROP/**/TABLE`), stacked queries (`; DROP TABLE`), CTE-wrapped writes (`WITH … DELETE`) or `ATTACH`/`load_extension` get through? | ✅ Four layers in `sql_tool.py`: (1) a validator that allows one statement starting with `SELECT`/`WITH`, no comments, no write keywords, with string literals blanked first so `'update pending'` is not a false positive; (2) a **SQLite authorizer** that denies every action except reads; (3) a `mode=ro` URI connection; (4) a 2 s timeout and a 100-row cap. A test switches layer 1 **off** and proves that layers 2 and 3 still block `DELETE`, `DROP`, `CREATE`, `PRAGMA` and `load_extension`. Another test checks that the data is unchanged after all 15 attacks. |
| **Arbitrary code execution via math** | Any `eval`, `exec` or `numexpr`? Does `__import__('os').system('ls')` work? | ✅ `math_tool.py` walks the `ast` tree with an allow-list (numbers, arithmetic operators, `pi`/`e`, 13 named math functions). Names, attributes, strings, lists, lambdas, comprehensions and keyword arguments are rejected **before** anything runs. Exponent and result size limits stop `2 ** 10 ** 10`. A test scans the source to make sure `eval(` and `exec(` never appear. |
| **MCP → OpenAI schema mismatch** | Does the client wrap each MCP `inputSchema` as `{"type": "function", "function": {"name", "description", "parameters"}}`? | ✅ `to_openai_tools()` in `mcp_client.py`. Tested against the real server's schemas, and the full agent run in the UI shows the model choosing tools with correctly named arguments. |
| **Infinite loops & state growth** | Is there a hard `max_turns`? Are tool exceptions returned to the model? | ✅ `max_turns=5` (test: an LLM that always calls tools stops after exactly 3 when the limit is 3). Bad JSON, unknown (hallucinated) tool names, timeouts and tool errors all become `role="tool"` messages. Tool output is capped at 4,000 characters. Every `tool_call_id` gets exactly one reply, including parallel calls. |

## 3. Bugs found in the AI draft (before → after)

### Bug 1: The server crashed on startup because the SDK wasn't pinned
The assignment's install command is `uv add "mcp[cli]"`. In November 2026 that installs **MCP 2.3**, where `FastMCP` was renamed. The draft's first line of real code failed:

```text
ModuleNotFoundError: No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP was renamed
to MCPServer (from mcp.server.mcpserver import MCPServer) and other APIs changed
```

**Before** (`pyproject.toml`)
```toml
dependencies = [
    "mcp[cli]",
```
**After**
```toml
dependencies = [
    "mcp[cli]>=1.20,<2",
```
Pinning the major version keeps the code on the API the assignment specifies (`FastMCP`, `ClientSession`, `stdio_client`) and stops a future `uv sync` from silently breaking the deployed app.

### Bug 2: Invalid OpenAI request when there are no tools
**Before** (`agent_loop.py`)
```python
resp = await client.chat.completions.create(
    model=model, messages=messages, tools=tools or None, tool_choice="auto" if tools else None,
    temperature=0,
)
```
If the tool list is empty (for example, if discovery returns nothing), the SDK puts `"tools": null, "tool_choice": null` in the request body. I confirmed this by capturing the body with a mock HTTP transport. `null` isn't a documented value for either field (`tools` must be an array, and `tool_choice` only makes sense when tools exist), so the request depends on the API quietly ignoring it. The fix leaves both fields out instead.

**After**
```python
extra = {"tools": tools, "tool_choice": "auto"} if tools else {}
resp = await client.chat.completions.create(model=model, messages=messages, temperature=0, **extra)
```

### Bug 3: The security layer blamed the wrong thing for the stacked-query attack
The red-team probe `SELECT * FROM orders; DROP TABLE inventory;--` was blocked, but the message said **"SQL comments are not allowed."** That's because the comment check ran before the stacked-statement check, and the payload ends in `--`. For an auditor reading the logs, the real attack (a second statement) was hidden.

**Before** (`sql_tool.py`)
```python
code = STRING_LITERAL_RE.sub("''", query)
if "--" in code or "/*" in code or "*/" in code:
    raise SecurityException("SQL comments are not allowed.")
code = code.rstrip().rstrip(";").rstrip()
if ";" in code:
    raise SecurityException("Only one statement is allowed (stacked queries are blocked).")
```
**After**
```python
code = STRING_LITERAL_RE.sub("''", query).rstrip().rstrip(";").rstrip()
if ";" in code:
    raise SecurityException("Only one statement is allowed (stacked queries are blocked).")
if "--" in code or "/*" in code or "*/" in code:
    raise SecurityException("SQL comments are not allowed.")
```

### Bug 4: Tool-calling hallucination: the model guessed a wrong SKU and reported "no stock"
Found during my first live run of the full ops prompt. gpt-4o-mini called:

```json
{"sql_query": "SELECT quantity FROM inventory WHERE sku = '90210' AND warehouse = 'Seattle'"}
```

The real SKU is `SKU-90210`, so the query returned 0 rows, and the final answer said *"There is no stock available for SKU-90210 in Seattle (quantity: 0). Therefore, we cannot fulfill order #4492."* That's wrong: Seattle has **140** units and the order needs 120. Every tool worked correctly. The model invented the argument value, then treated "0 rows" as a fact.

**Before** (tool description and system prompt)
```python
"Inventory is per warehouse city (inventory.warehouse). Order ids are integers (order #4492 -> 4492). "
...
- SQL must be a single read-only SELECT. Look at the table list in the tool description.
```
**After**
```python
# mcp_server.py: the real values are read from the database at startup and put in the description
f"Exact values: sku is one of {SAMPLE_SKUS} (always keep the 'SKU-' prefix); "
f"warehouse/city values: {SAMPLE_CITIES}. "
...
if result["row_count"] == 0:
    result["hint"] = ("0 rows matched. Before concluding that nothing exists, check the exact filter values "
                      "(e.g. sku must include the prefix, like 'SKU-90210') and retry.")

# agent_loop.py system prompt
- Use exact values: SKUs keep their prefix ('SKU-90210', not '90210'). If a query returns 0 rows, check
  your filter values and retry before saying something doesn't exist.
```
A regression test (`test_sql_tool_teaches_exact_value_formats_and_hints_on_zero_rows`) checks that the description lists the real SKUs and that a 0-row result carries the hint.

### Bug 5 (minor): Unhelpful rejection message in the math tool
`__import__('os').listdir('.')` was correctly blocked, but the message was `'Call' is not allowed in a math expression.` I added specific messages for function names, method calls and attribute access, so the model (and the operator) can see *why*: `Method call '.listdir()' is not allowed (attribute access is blocked).`

## 4. Known limitations (honest notes)
- The weather "dispatch risk" is a simple rule (heavy rain, ≥ 10 mm today or wind ≥ 50 km/h → HIGH). A real logistics team would tune it.
- The SQL validator rejects some harmless queries, such as a column alias called `"update"` in double quotes. For a read-only ops tool I chose false positives over false negatives.
- The UI starts a fresh MCP server process per request (about 1 s). A long-running app would keep one session open.

## 5. My verification (tick after doing it myself)
- [x] `uv run pytest -v`: 73 passed, including the live gpt-4o-mini test
- [x] The full ops prompt in the UI calls all 3 tools and gives a grounded answer
- [x] The Security Workbench blocks the SQL probe and the `__import__` probe, and the server stays alive
