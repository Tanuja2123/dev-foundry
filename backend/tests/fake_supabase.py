"""Minimal in-memory stand-in for the supabase-py query builder calls used by project_store (no network)."""
import copy
import itertools
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

_PRIMARY_KEYS = {"Project": "project_id", "Repository": "repo_id"}
_clock = itertools.count(1)


class _Query:
    def __init__(self, db: "FakeSupabase", table: str):
        self.db, self.table, self.op, self.payload = db, table, "select", None
        self.filters: List = []
        self.order_by = None
        self.limit_n = None

    def select(self, *_):
        self.op = "select"
        return self

    def insert(self, row: Dict[str, Any]):
        self.op, self.payload = "insert", row
        return self

    def update(self, fields: Dict[str, Any]):
        self.op, self.payload = "update", fields
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def order(self, column, desc=False):
        self.order_by = (column, desc)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def execute(self):
        if self.table in self.db.missing_tables:
            raise Exception({"code": "PGRST205", "message": f"Could not find the table 'public.{self.table}' in the schema cache"})
        rows = self.db.tables.setdefault(self.table, [])
        if self.op == "insert":
            row = copy.deepcopy(self.payload)
            row.setdefault(_PRIMARY_KEYS[self.table], str(uuid.uuid4()))
            row.setdefault("created_at", next(_clock))
            if self.table == "Repository":
                row.setdefault("status", "PROPOSED")
                row.setdefault("structure_version", 0)
            for existing in rows:
                if self.table == "Project" and existing["conv_id"] == row["conv_id"]:
                    raise Exception("duplicate key value violates unique constraint")
            rows.append(row)
            return SimpleNamespace(data=[copy.deepcopy(row)])

        matched = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        if self.op == "update":
            for r in matched:
                r.update(copy.deepcopy(self.payload))
            return SimpleNamespace(data=copy.deepcopy(matched))
        if self.order_by:
            matched.sort(key=lambda r: r.get(self.order_by[0]), reverse=self.order_by[1])
        if self.limit_n is not None:
            matched = matched[: self.limit_n]
        return SimpleNamespace(data=copy.deepcopy(matched))


class FakeSupabase:
    def __init__(self, missing_tables=()):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.missing_tables = set(missing_tables)

    def table(self, name: str) -> _Query:
        return _Query(self, name)
