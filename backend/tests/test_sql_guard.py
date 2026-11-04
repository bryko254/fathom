import pytest

from app.sql_guard import confirm_token, guard_sql


READS = [
    "SELECT 1",
    "SELECT * FROM accounts WHERE name = 'DELETE'",
    "SELECT 'DROP TABLE accounts'",
    "WITH c AS (SELECT 1 AS n) SELECT * FROM c",
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 10) SELECT * FROM c",
    "EXPLAIN SELECT * FROM accounts",
    "VALUES (1)",
]

BLOCKS = [
    "DROP TABLE accounts",
    "DELETE FROM accounts; DROP TABLE accounts",
    "SELECT * FROM accounts; DROP TABLE accounts",
    "/* comment */ DROP TABLE accounts",
    "CREATE TABLE t (id int)",
    "ALTER TABLE accounts ADD COLUMN x int",
    "TRUNCATE accounts",
    "GRANT SELECT ON accounts TO public",
    "ATTACH DATABASE 'x' AS evil",
    "PRAGMA writable_schema = ON",
    "SELECT load_extension('foo')",
    "VACUUM",
    "REPLACE INTO accounts (id, name) VALUES (1, 'a')",
    "BEGIN",
    "WITH x AS (DELETE FROM accounts RETURNING *) SELECT * FROM x",
    "",
    "SELECT * FROM accounts INTO OUTFILE '/tmp/x'",
]

WRITES = [
    "DELETE FROM accounts WHERE id = 1",
    "UPDATE accounts SET name = 'x' WHERE id = 1",
    "INSERT INTO accounts (id, name) VALUES (1, 'a')",
]


@pytest.mark.parametrize("sql", READS)
def test_reads_are_allowed(sql):
    decision = guard_sql(sql)
    assert decision.allowed, decision.reason
    assert decision.kind == "read"


@pytest.mark.parametrize("sql", BLOCKS)
def test_dangerous_sql_is_blocked(sql):
    decision = guard_sql(sql)
    assert not decision.allowed
    assert decision.kind == "blocked"
    assert decision.token is None


@pytest.mark.parametrize("sql", WRITES)
def test_writes_need_confirmation(sql):
    decision = guard_sql(sql)
    assert not decision.allowed
    assert decision.kind == "write"
    assert decision.token == confirm_token(sql)


def test_token_ignores_whitespace_and_semicolon():
    assert confirm_token("DELETE FROM accounts WHERE id = 1") == confirm_token(
        "DELETE  FROM   accounts WHERE id = 1 ;"
    )
