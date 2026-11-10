import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(ROOT, "prisma", "migrations")


def _schema():
    with open(os.path.join(ROOT, "prisma", "schema.prisma")) as f:
        return f.read()


def _all_migration_sql():
    out = []
    for name in sorted(os.listdir(MIGRATIONS)):
        path = os.path.join(MIGRATIONS, name, "migration.sql")
        if os.path.isfile(path):
            with open(path) as f:
                out.append(f.read())
    return "\n".join(out)


def test_every_mapped_table_has_a_migration():
    tables = re.findall(r'@@map\("([a-z_]+)"\)', _schema())
    sql = _all_migration_sql()
    missing = [t for t in tables if f'CREATE TABLE "{t}"' not in sql]
    assert tables and not missing, missing


def test_event_lane_migration_contents():
    sql = _all_migration_sql()
    assert 'CREATE EXTENSION IF NOT EXISTS "vector"' in sql
    assert '"embedding" vector(384)' in sql
    assert "USING hnsw" in sql
    assert "BEFORE UPDATE OR DELETE ON \"ledger_entry\"" in sql
