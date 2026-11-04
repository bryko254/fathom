import sqlite3

import pytest

from app.sql_import import SqlImportError, load_sql_dump, prepare_sql_dump


MYSQL_DUMP = """
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
SET NAMES utf8mb4;
LOCK TABLES `orders` WRITE;
CREATE TABLE `orders` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `note` varchar(40) DEFAULT NULL,
  `total` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_note` (`note`)
) ENGINE=InnoDB AUTO_INCREMENT=10 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
UNLOCK TABLES;
INSERT INTO `orders` (`id`, `note`, `total`) VALUES (1, 'keep `ticks`', 12.50);
"""

POSTGRES_DUMP = """
SET statement_timeout = 0;
SET client_encoding = 'UTF8';
SELECT pg_catalog.set_config('search_path', '', false);
CREATE TABLE public.customers (
    id integer NOT NULL,
    full_name text,
    city text
);
COPY public.customers (id, full_name, city) FROM stdin;
1	Amina Otieno	Nairobi
2	\\N	Lagos
\\.
ALTER TABLE public.customers OWNER TO postgres;
"""


def test_mysql_dump_loads_without_engine_noise(tmp_path):
    dest = tmp_path / "mysql.sqlite"
    load_sql_dump(MYSQL_DUMP, dest)
    conn = sqlite3.connect(dest)
    try:
        row = conn.execute("SELECT id, note, total FROM orders").fetchone()
    finally:
        conn.close()
    assert row[0] == 1
    assert row[1] == "keep `ticks`"
    assert "ENGINE" not in prepare_sql_dump(MYSQL_DUMP).upper()
    assert "AUTO_INCREMENT" not in prepare_sql_dump(MYSQL_DUMP).upper()
    assert "LOCK TABLES" not in prepare_sql_dump(MYSQL_DUMP).upper()


def test_postgres_copy_becomes_inserts(tmp_path):
    dest = tmp_path / "pg.sqlite"
    load_sql_dump(POSTGRES_DUMP, dest)
    conn = sqlite3.connect(dest)
    try:
        rows = conn.execute("SELECT id, full_name, city FROM customers ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [(1, "Amina Otieno", "Nairobi"), (2, None, "Lagos")]
    prepared = prepare_sql_dump(POSTGRES_DUMP)
    assert "pg_catalog" not in prepared
    assert "COPY " not in prepared.upper()
    assert "OWNER TO" not in prepared.upper()


def test_attach_is_still_denied(tmp_path):
    dest = tmp_path / "evil.sqlite"
    script = "CREATE TABLE accounts (id integer);\nATTACH DATABASE 'other.sqlite' AS evil;\n"
    with pytest.raises(SqlImportError, match="rejected a statement"):
        load_sql_dump(script, dest)
    assert not dest.exists() or sqlite3.connect(dest).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='evil'"
    ).fetchone()[0] == 0


def test_load_extension_is_refused(tmp_path):
    dest = tmp_path / "ext.sqlite"
    with pytest.raises(SqlImportError, match="load_extension"):
        load_sql_dump("SELECT load_extension('libevil');", dest)
    assert not dest.exists() or dest.stat().st_size == 0


def test_unfinished_copy_explains_itself():
    with pytest.raises(SqlImportError, match=r"COPY"):
        prepare_sql_dump("COPY public.t (id) FROM stdin;\n1\n")


def test_leftover_syntax_names_the_statement(tmp_path):
    dest = tmp_path / "bad.sqlite"
    with pytest.raises(SqlImportError, match="Near: CREATE WIDGET"):
        load_sql_dump("CREATE WIDGET nonsense;", dest)
