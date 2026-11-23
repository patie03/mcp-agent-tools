"""MCP server exposing three sandboxed tools over stdio.

Run standalone (it waits for a client on stdin/stdout):
    uv run python -m src.server.mcp_server

Inspect it with the official MCP Inspector:
    uv run mcp dev src/server/mcp_server.py

Every tool returns a JSON string. On failure it returns {"error": "..."} with a short,
safe message (no stack traces, no file paths) so the LLM can read it and self-correct,
and the server process keeps running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `mcp dev src/server/mcp_server.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server.fastmcp import FastMCP

from src.server.tools.math_tool import UnsafeExpressionError, safe_evaluate
from src.server.tools.sql_tool import SecurityException, describe_schema, execute_read_only
from src.server.tools.weather_tool import WeatherError, get_weather

# stdio transport: stdout carries the protocol, so logs must go to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
log = logging.getLogger("mcp-agent-tools")

TOOL_TIMEOUT_SECONDS = 15
SCHEMA_TEXT = describe_schema()
SAMPLE_SKUS = ", ".join(repr(r[0]) for r in execute_read_only("SELECT sku FROM products ORDER BY sku")["rows"])
SAMPLE_CITIES = ", ".join(repr(r[0]) for r in execute_read_only("SELECT DISTINCT warehouse FROM inventory ORDER BY warehouse")["rows"])
ZERO_ROW_HINT = ("0 rows matched. Before concluding that nothing exists, check the exact filter values "
                 "(e.g. sku must include the prefix, like 'SKU-90210') and retry.")

mcp = FastMCP("enterprise-ops-tools")


def _ok(payload: dict) -> str:
    return json.dumps(payload, default=str)


def _err(kind: str, message: str) -> str:
    return json.dumps({"error": message, "error_type": kind})


@mcp.tool(description=(
    "Run ONE read-only SQL SELECT against the enterprise operations SQLite database and return "
    "the rows as JSON. Use it for orders, inventory, products and customers. "
    f"Tables: {SCHEMA_TEXT}. "
    "Inventory is per warehouse city (inventory.warehouse). Order ids are integers (order #4492 -> 4492). "
    f"Exact values: sku is one of {SAMPLE_SKUS} (always keep the 'SKU-' prefix); "
    f"warehouse/city values: {SAMPLE_CITIES}. "
    "Only a single SELECT (or WITH ... SELECT) is accepted; writes, comments and stacked queries are rejected."
))
async def query_database(sql_query: str) -> str:
    try:
        result = await asyncio.wait_for(asyncio.to_thread(execute_read_only, sql_query), TOOL_TIMEOUT_SECONDS)
        if result["row_count"] == 0:
            result["hint"] = ZERO_ROW_HINT
        return _ok(result)
    except SecurityException as exc:
        return _err("SecurityException", f"Query rejected: {exc}")
    except ValueError as exc:
        return _err("SQLError", str(exc))
    except asyncio.TimeoutError:
        return _err("Timeout", "The query timed out.")
    except Exception:  # never leak internals
        log.exception("query_database failed")
        return _err("InternalError", "The database tool failed unexpectedly.")


@mcp.tool(description=(
    "Get LIVE current weather and today's precipitation for a city (Open-Meteo). Returns temperature (C), "
    "conditions, precipitation now/today (mm), chance of precipitation, wind (km/h) and a "
    "dispatch_weather_risk of LOW, MODERATE or HIGH for ground shipping. Argument: a plain city name, e.g. 'Seattle'."
))
async def get_current_weather(city: str) -> str:
    try:
        return _ok(await asyncio.wait_for(get_weather(city), TOOL_TIMEOUT_SECONDS))
    except WeatherError as exc:
        return _err("WeatherError", str(exc))
    except asyncio.TimeoutError:
        return _err("Timeout", "The weather lookup timed out.")
    except Exception:
        log.exception("get_current_weather failed")
        return _err("InternalError", "The weather tool failed unexpectedly.")


@mcp.tool(description=(
    "Evaluate an arithmetic expression exactly and safely (no eval). Supports numbers, + - * / // % **, "
    "parentheses, pi, e and sqrt, abs, round, floor, ceil, log, log10, exp, sin, cos, tan, min, max. "
    "ALWAYS use this for any calculation instead of doing math yourself, e.g. '22740 * 0.035'."
))
async def calculate_expression(expression: str) -> str:
    try:
        return _ok(safe_evaluate(expression))
    except UnsafeExpressionError as exc:
        return _err("UnsafeExpressionError", f"Expression rejected: {exc}")
    except (ValueError, ArithmeticError) as exc:
        return _err("MathError", str(exc))
    except Exception:
        log.exception("calculate_expression failed")
        return _err("InternalError", "The math tool failed unexpectedly.")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
