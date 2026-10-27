"""Import a bounded UTF-8 CSV snapshot into a new SQLite database.

Only the Python standard library is used. See README.md for the supported
schema and filesystem assumptions. All public failures use CsvImportError
without including source values, SQL, or filesystem paths in their messages.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
from typing import Any

__all__ = ["CsvImportError", "import_csv"]
__version__ = "1.0.0"

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_ROWS = 1_000_000
MAX_COLUMNS = 1_000
MAX_FIELD_CHARACTERS = 65_536
MAX_HEADER_CHARACTERS = 128

_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z", re.ASCII)
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z", re.ASCII)
_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z", re.ASCII
)
_TYPES = {"text", "integer", "real", "boolean", "decimal"}
_SQL_TYPES = {
    "text": "TEXT",
    "integer": "INTEGER",
    "real": "REAL",
    "boolean": "INTEGER",
    "decimal": "TEXT",
}


class CsvImportError(ValueError):
    """The input cannot be imported safely into a new database."""


def _path(value: Any) -> str:
    result = os.fspath(value)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise CsvImportError("Invalid local file.")
    return result


def _snapshot(path: str, limit: int) -> bytes:
    # Nonblocking open is essential: fstat must reject a FIFO without waiting
    # for a writer. Regular files ignore O_NONBLOCK on supported local systems.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise CsvImportError("Invalid local file.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            result = source.read(limit + 1)
        if len(result) > limit:
            raise CsvImportError("Input size limit exceeded.")
        return result
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _validate_options(table: Any, delimiter: Any) -> None:
    if (
        type(table) is not str
        or _TABLE.fullmatch(table) is None
        or table.lower().startswith("sqlite_")
    ):
        raise CsvImportError("Invalid table name.")
    if type(delimiter) is not str or delimiter not in {",", "\t", ";"}:
        raise CsvImportError("Invalid delimiter.")


def _validate_header(header: list[str]) -> None:
    if not header or len(header) > MAX_COLUMNS:
        raise CsvImportError("Invalid CSV header.")
    names: set[str] = set()
    for name in header:
        if not name or len(name) > MAX_HEADER_CHARACTERS or "\x00" in name:
            raise CsvImportError("Invalid CSV header.")
        folded = name.casefold()
        if folded in names:
            raise CsvImportError("Invalid CSV header.")
        names.add(folded)


def _exact_keys(value: Any, expected: set[str]) -> bool:
    return (
        type(value) is dict
        and all(type(key) is str for key in value)
        and value.keys() == expected
    )


def _validate_schema(schema: Any, header: list[str]) -> list[tuple[str, bool]]:
    if schema is None:
        return [("text", False) for _ in header]
    if not _exact_keys(schema, {"columns"}):
        raise CsvImportError("Invalid schema.")
    columns = schema["columns"]
    if type(columns) is not list or len(columns) != len(header):
        raise CsvImportError("Invalid schema.")
    specifications: list[tuple[str, bool]] = []
    for name, column in zip(header, columns):
        if not _exact_keys(column, {"name", "type", "nullable"}):
            raise CsvImportError("Invalid schema.")
        if (
            type(column["name"]) is not str
            or column["name"] != name
            or type(column["type"]) is not str
            or column["type"] not in _TYPES
            or type(column["nullable"]) is not bool
        ):
            raise CsvImportError("Invalid schema.")
        specifications.append((column["type"], column["nullable"]))
    return specifications


def _convert(value: str, kind: str, nullable: bool) -> Any:
    if value == "" and nullable:
        return None
    if kind == "text":
        return value
    if kind == "integer":
        if _INTEGER.fullmatch(value) is None or len(value.lstrip("-")) > 19:
            raise CsvImportError("Invalid typed value.")
        result = int(value)
        if not -(2**63) <= result <= 2**63 - 1:
            raise CsvImportError("Invalid typed value.")
        return result
    if kind == "boolean":
        if value not in {"true", "false"}:
            raise CsvImportError("Invalid typed value.")
        return 1 if value == "true" else 0
    if _NUMBER.fullmatch(value) is None:
        raise CsvImportError("Invalid typed value.")
    if kind == "decimal":
        return value
    result = float(value)
    if not math.isfinite(result):
        raise CsvImportError("Invalid typed value.")
    return result


def _remove_stage(stage: str | None) -> None:
    if stage is None:
        return
    # These names belong solely to the newly created private database. Never
    # clean up by globbing or by removing the requested destination.
    for suffix in ("-journal", "-wal", "-shm", ""):
        try:
            os.unlink(stage + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            # Do not hide the original failure with a secondary cleanup error.
            pass


def import_csv(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    table: str = "records",
    schema: dict[str, Any] | None = None,
    delimiter: str = ",",
) -> dict[str, Any]:
    """Create a new SQLite file from one bounded, immutable CSV byte snapshot.

    Return ``table``, ``rows``, ``columns`` and the original bytes' ``sha256``.
    Raise CsvImportError on invalid input or ordinary I/O/SQLite failures.
    Existing destinations, including broken symlinks, are never overwritten.
    """
    stage: str | None = None
    connection: sqlite3.Connection | None = None
    try:
        _validate_options(table, delimiter)
        source_path = _path(source)
        destination_path = _path(destination)
        try:
            os.lstat(destination_path)
        except FileNotFoundError:
            pass
        else:
            raise CsvImportError("Destination already exists.")

        snapshot = _snapshot(source_path, MAX_SOURCE_BYTES)
        digest = hashlib.sha256(snapshot).hexdigest()
        reader = csv.reader(
            io.StringIO(snapshot.decode("utf-8-sig"), newline=""),
            delimiter=delimiter,
            strict=True,
        )
        try:
            header = next(reader)
        except StopIteration:
            raise CsvImportError("Invalid CSV header.") from None
        _validate_header(header)
        specifications = _validate_schema(schema, header)

        parent = os.path.dirname(destination_path) or "."
        descriptor, stage = tempfile.mkstemp(
            prefix=".csv-sqlite-import-", suffix=".sqlite3", dir=parent
        )
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(stage, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        declarations = ", ".join(
            _quote(name) + " " + _SQL_TYPES[kind] + ("" if nullable else " NOT NULL")
            for name, (kind, nullable) in zip(header, specifications)
        )
        connection.execute("CREATE TABLE " + _quote(table) + " (" + declarations + ")")
        statement = (
            "INSERT INTO "
            + _quote(table)
            + " VALUES ("
            + ",".join("?" for _ in header)
            + ")"
        )
        rows = 0
        for row in reader:
            if not row or len(row) != len(header):
                raise CsvImportError("Invalid CSV row.")
            if rows >= MAX_ROWS:
                raise CsvImportError("CSV row limit exceeded.")
            if any(len(value) > MAX_FIELD_CHARACTERS for value in row):
                raise CsvImportError("CSV field limit exceeded.")
            values = tuple(
                _convert(value, kind, nullable)
                for value, (kind, nullable) in zip(row, specifications)
            )
            connection.execute(statement, values)
            rows += 1
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise CsvImportError("Database validation failed.")
        connection.commit()
        connection.close()
        connection = None
        # link() is the no-overwrite publication operation, including when a
        # competing importer creates the destination after our initial check.
        os.link(stage, destination_path)
        return {"table": table, "rows": rows, "columns": header, "sha256": digest}
    except CsvImportError:
        raise
    except (OSError, ValueError, TypeError, csv.Error, sqlite3.Error, OverflowError):
        raise CsvImportError("CSV import failed.") from None
    finally:
        if connection is not None:
            try:
                connection.close()  # Rolls back an unfinished transaction.
            except sqlite3.Error:
                pass
        _remove_stage(stage)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CsvImportError("Invalid schema.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise CsvImportError("Invalid schema.")


def _load_schema(path: str) -> Any:
    try:
        snapshot = _snapshot(_path(path), MAX_SCHEMA_BYTES)
        result = json.loads(
            snapshot.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(result) is not dict:
            raise CsvImportError("Invalid schema.")
        return result
    except CsvImportError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise CsvImportError("Invalid schema.") from None


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, "error: invalid arguments\n")


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; errors never print data, SQL, or paths."""
    parser = _Parser(description="Import UTF-8 CSV into a new SQLite database.")
    parser.add_argument("CSV", help="source CSV file (at most 64 MiB)")
    parser.add_argument("OUTPUT", help="new destination database file")
    parser.add_argument("--table", default="records", help="table name (default: records)")
    parser.add_argument("--schema", metavar="JSONFILE", help="explicit passive JSON schema")
    parser.add_argument(
        "--delimiter", choices=("comma", "tab", "semicolon"), default="comma"
    )
    arguments = parser.parse_args(argv)
    try:
        schema = _load_schema(arguments.schema) if arguments.schema is not None else None
        summary = import_csv(
            arguments.CSV,
            arguments.OUTPUT,
            table=arguments.table,
            schema=schema,
            delimiter={"comma": ",", "tab": "\t", "semicolon": ";"}[arguments.delimiter],
        )
    except CsvImportError:
        sys.stderr.write("error: CSV import failed\n")
        return 2
    sys.stdout.write(json.dumps(summary, sort_keys=True, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
