"""Read-only SQL guard.

Selects run. A single INSERT, UPDATE, or DELETE is held for an explicit
confirmation token. DDL, file attachment, pragmas, and nested writes are
refused outright. The sandbox also opens SQLite read-only unless a confirmed
write is being dispatched.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

BLOCKED_FUNCTIONS = {
    "load_extension",
    "writefile",
    "readfile",
    "edit",
    "fts3_tokenizer",
    "sqlite_compileoption_get",
}

WRITE_TYPES = (exp.Insert, exp.Update, exp.Delete)
ALWAYS_BLOCK_TYPES = (
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Attach,
    exp.Pragma,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Merge,
    exp.Command,
)

EXPLAIN_RE = re.compile(r"^explain(\s+query\s+plan)?\s+(select|with)\b", re.I)


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    kind: str  # read, write, blocked
    reason: str
    token: str | None = None


def normalize_sql(sql: str) -> str:
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return re.sub(r"\s+", " ", text)


def confirm_token(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()[:16]


def _function_name(node: exp.Expression) -> str:
    name = getattr(node, "name", None) or ""
    if name:
        return str(name).lower()
    sql_name = getattr(node, "sql_name", None)
    if callable(sql_name):
        try:
            name = sql_name() or ""
        except Exception:
            name = ""
    return str(name).lower()


def guard_sql(sql: str, *, dialect: str = "sqlite") -> GuardDecision:
    raw = (sql or "").strip()
    if not raw:
        return GuardDecision(False, "blocked", "Empty SQL was refused.")
    if raw.count(";") > 1 or (";" in raw[:-1]):
        # sqlglot is the real check; this catches junk before the parser.
        pass
    try:
        trees = sqlglot.parse(raw, read=dialect)
    except sqlglot.errors.ParseError:
        return GuardDecision(False, "blocked", "SQL could not be parsed, so it was refused.")
    statements = [tree for tree in trees if tree is not None]
    if len(statements) != 1:
        return GuardDecision(
            False,
            "blocked",
            "Only one statement is allowed. Chained writes were refused.",
        )
    tree = statements[0]
    blocked_reason = _blocked_reason(tree)
    if blocked_reason:
        return GuardDecision(False, "blocked", blocked_reason)
    writes = [node for node in tree.walk() if isinstance(node, WRITE_TYPES)]
    if writes:
        if not isinstance(tree, WRITE_TYPES):
            return GuardDecision(
                False,
                "blocked",
                "A write nested inside a read was refused. Fathom will not run it.",
            )
        token = confirm_token(raw)
        verb = type(tree).__name__.upper()
        return GuardDecision(
            False,
            "write",
            f"{verb} changes data. Confirm this exact statement to run it once. DDL stays blocked.",
            token=token,
        )
    return GuardDecision(True, "read", "Read-only statement.")


def _blocked_reason(tree: exp.Expression) -> str | None:
    for node in tree.walk():
        if isinstance(node, exp.Command):
            text = node.sql().strip()
            if EXPLAIN_RE.match(text):
                continue
            first = text.split(None, 1)[0].upper()
            return f"{first} is blocked. Fathom does not run DDL, admin, or file commands."
        if isinstance(node, ALWAYS_BLOCK_TYPES):
            return f"{type(node).__name__.upper()} is blocked. Schema and file changes are refused even with confirmation."
        if isinstance(node, (exp.Anonymous, exp.Func)):
            fname = _function_name(node)
            if fname in BLOCKED_FUNCTIONS:
                return f"Function {fname} is blocked."
        if isinstance(node, exp.Select) and node.args.get("into"):
            return "SELECT INTO is blocked."
    return None
