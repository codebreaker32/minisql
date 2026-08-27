"""
catalog.py — System catalog: table schemas and index metadata.

Real databases keep this in special system tables (pg_catalog, INFORMATION_SCHEMA).
MiniSQL keeps it simple: a JSON file listing each table's columns and which
columns have indexes. The catalog is the thing the planner consults to decide
"is there an index I can use for this WHERE clause?" and the thing the engine
consults to enforce column types and the PRIMARY KEY constraint.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict


@dataclass
class TableSchema:
    name: str
    columns: list[dict]          # [{"name": ..., "type": ..., "primary_key": bool}]
    indexes: list[str] = field(default_factory=list)  # column names with an index

    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]

    def column_type(self, name: str) -> str:
        for c in self.columns:
            if c["name"] == name:
                return c["type"]
        raise ValueError(f"Column {name!r} not found on table {self.name!r}")

    def pk_column(self) -> str | None:
        for c in self.columns:
            if c.get("primary_key"):
                return c["name"]
        return None


class Catalog:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "catalog.json")
        self.tables: dict[str, TableSchema] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                raw = json.load(f)
            for name, t in raw.items():
                self.tables[name] = TableSchema(**t)

    def _save(self):
        raw = {name: asdict(schema) for name, schema in self.tables.items()}
        with open(self.path, "w") as f:
            json.dump(raw, f, indent=2)

    def create_table(self, name: str, columns: list[dict]) -> None:
        if name in self.tables:
            raise ValueError(f"Table {name!r} already exists")
        names = [c["name"] for c in columns]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate column name in table {name!r}")
        if sum(1 for c in columns if c.get("primary_key")) > 1:
            raise ValueError("A table may have at most one PRIMARY KEY column")
        self.tables[name] = TableSchema(name=name, columns=columns)
        self._save()

    def add_index(self, table_name: str, column: str) -> None:
        schema = self.get_table(table_name)
        if column not in schema.column_names():
            raise ValueError(f"Column {column!r} not found on table {table_name!r}")
        if column not in schema.indexes:
            schema.indexes.append(column)
            self._save()

    def get_table(self, name: str) -> TableSchema:
        if name not in self.tables:
            raise ValueError(f"No such table: {name!r}")
        return self.tables[name]

    def has_index(self, table_name: str, column: str) -> bool:
        return column in self.get_table(table_name).indexes

    def heap_path(self, table_name: str) -> str:
        return os.path.join(self.data_dir, f"{table_name}.tbl")

    def journal_path(self) -> str:
        return os.path.join(self.data_dir, "undo.journal")
