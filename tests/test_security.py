"""Red-team tests: SQL injection / writes and code execution through the math tool."""

import json

import pytest

from src.client.mcp_client import MCPToolClient
from src.server.tools.math_tool import UnsafeExpressionError, safe_evaluate
from src.server.tools.sql_tool import SecurityException, execute_read_only, validate_query

# ---------------------------------------------------------------- Test 6: SQL DDL/DML rejection
MALICIOUS_SQL = [
    "DROP TABLE inventory",
    "UPDATE orders SET status = 'cancelled'",
    "INSERT INTO orders VALUES (9999, 101, 'SKU-90210', 1, 1.0, 'Seattle', 'pending', '2026-01-01')",
    "DELETE FROM customers",
    "SELECT * FROM orders; DROP TABLE inventory;--",        # stacked query
    "SELECT 1; SELECT 2",                                  # any second statement
    "DROP/**/TABLE inventory",                             # comment-obfuscated keyword
    "SELECT * FROM orders WHERE 1=1 -- AND customer_id = 1",
    "WITH x AS (SELECT 1) DELETE FROM orders",             # write hidden behind a CTE
    "WITH x AS (SELECT 1) REPLACE INTO orders SELECT * FROM orders",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "PRAGMA writable_schema = ON",
    "SELECT load_extension('/tmp/evil.so')",
    "CREATE TABLE pwned (x)",
    "  ",
]


@pytest.mark.parametrize("sql", MALICIOUS_SQL)
def test_sql_ddl_dml_and_stacked_queries_rejected(sql):
    with pytest.raises(SecurityException):
        execute_read_only(sql)


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "DROP TABLE inventory", "CREATE TABLE t (a)",
                                 "SELECT load_extension('x')", "PRAGMA table_info(orders)",
                                 "UPDATE products SET unit_price = 0"])
def test_database_guard_holds_even_if_validator_is_bypassed(sql):
    """Defense in depth: skip layer 1 and the authorizer + mode=ro still refuse."""
    with pytest.raises(SecurityException, match="only reading is allowed"):
        execute_read_only(sql, validate=False)


def test_data_is_intact_after_attacks():
    for sql in MALICIOUS_SQL:
        try:
            execute_read_only(sql)
        except SecurityException:
            pass
    assert execute_read_only("SELECT COUNT(*) FROM inventory")["rows"] == [[17]]
    assert execute_read_only("SELECT status FROM orders WHERE order_id = 4492")["rows"] == [["pending"]]


def test_runaway_query_is_stopped_by_timeout():
    with pytest.raises(SecurityException, match="stopped"):
        execute_read_only("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n")


def test_validator_returns_single_clean_statement():
    assert validate_query("  SELECT 1 ;  ") == "SELECT 1"


# ---------------------------------------------------------------- Test 7: code execution blocked
ACE_PAYLOADS = [
    "__import__('os').listdir('.')",
    "__import__('os').system('ls')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('/etc/passwd').read()",
    "eval('1+1')",
    "exec('import os')",
    "globals()",
    "(lambda: 1)()",
    "[x for x in range(10)]",
    "'a' * 10",
    "True + 1",
    "os.getcwd()",
    "import os",
]


@pytest.mark.parametrize("payload", ACE_PAYLOADS)
def test_math_rejects_code_execution(payload):
    with pytest.raises(UnsafeExpressionError):
        safe_evaluate(payload)


@pytest.mark.parametrize("payload", ["2 ** 10 ** 10", "9 ** 9 ** 9", "10 ** 200", "1" * 300])
def test_math_resource_exhaustion_is_refused(payload):
    with pytest.raises(ValueError):
        safe_evaluate(payload)


def test_no_eval_or_exec_in_math_source():
    import inspect

    import src.server.tools.math_tool as m
    code = inspect.getsource(m)
    body = code.split('"""', 2)[2]  # skip the module docstring, which mentions them
    assert "eval(" not in body.replace("_eval(", "").replace("safe_evaluate(", "")
    assert "exec(" not in body


# ---------------------------------------------------------------- end to end through the MCP server
async def test_attacks_through_mcp_return_structured_errors_and_server_survives():
    async with MCPToolClient() as client:
        sql = await client.call_tool("query_database", {"sql_query": "SELECT * FROM orders; DROP TABLE inventory;--"})
        math = await client.call_tool("calculate_expression", {"expression": "__import__('os').listdir('.')"})
        for res in (sql, math):
            assert res.is_error
            body = json.loads(res.text)
            assert body["error_type"] in {"SecurityException", "UnsafeExpressionError"}
            assert "Traceback" not in res.text and "enterprise_ops.db" not in res.text and "/" not in body["error"]
        alive = await client.call_tool("query_database", {"sql_query": "SELECT COUNT(*) FROM inventory"})
        assert json.loads(alive.text)["rows"] == [[17]]
