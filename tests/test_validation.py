"""Input-only validation contracts using synthetic, local fixtures."""

import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import csv_sqlite_import as importer
from csv_sqlite_import import CsvImportError, import_csv, validate_csv


PROJECT = Path(__file__).resolve().parents[1]


def schema_for(*columns):
    return {
        "columns": [
            {"name": name, "type": kind, "nullable": nullable}
            for name, kind, nullable in columns
        ]
    }


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "input.csv"

    def write(self, contents):
        self.source.write_bytes(contents if isinstance(contents, bytes) else contents.encode())

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-m", "csv_sqlite_import", *map(str, arguments)],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_full_typed_summary_matches_import_and_exact_bytes(self):
        original = (
            b'\xef\xbb\xbflabel,n,r,b,d,optional\r\n'
            b'" a, b ",-9223372036854775808,1.25e2,true,1.2300,\r\n'
            b'"first\r\nsecond ""quote""",9223372036854775807,-0,false,1e999999,value\r\n'
        )
        self.write(original)
        schema = schema_for(
            ("label", "text", False), ("n", "integer", False),
            ("r", "real", False), ("b", "boolean", False),
            ("d", "decimal", False), ("optional", "text", True),
        )
        checked = validate_csv(self.source, table="inventory", schema=schema)
        self.assertEqual(
            checked,
            {
                "table": "inventory",
                "rows": 2,
                "columns": ["label", "n", "r", "b", "d", "optional"],
                "sha256": hashlib.sha256(original).hexdigest(),
            },
        )
        self.assertEqual(
            checked,
            import_csv(self.source, self.directory / "output.sqlite3", table="inventory", schema=schema),
        )
        self.assertEqual(self.source.read_bytes(), original)

    def test_validation_never_connects_stages_publishes_or_opens_for_writing(self):
        self.write("a\nvalue\n")
        before = sorted(self.directory.iterdir())
        original_open = os.open

        def read_only_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(importer.sqlite3, "connect", side_effect=AssertionError("SQLite opened")),
            mock.patch.object(importer.tempfile, "mkstemp", side_effect=AssertionError("staging created")),
            mock.patch.object(importer.os, "link", side_effect=AssertionError("publication attempted")),
            mock.patch.object(importer.os, "open", side_effect=read_only_open),
        ):
            self.assertEqual(validate_csv(str(self.source))["rows"], 1)
            self.write("a\nvalid\nwrong,width\n")
            with self.assertRaises(CsvImportError):
                validate_csv(self.source)
        self.assertEqual(sorted(self.directory.iterdir()), before)
        self.assertEqual(self.source.read_text(), "a\nvalid\nwrong,width\n")

    def test_header_only_and_each_delimiter(self):
        for delimiter in (",", "\t", ";"):
            with self.subTest(delimiter=delimiter):
                self.write("a" + delimiter + "b\n")
                self.assertEqual(validate_csv(self.source, delimiter=delimiter)["rows"], 0)
                self.write("a" + delimiter + "b\nx" + delimiter + "y\n")
                self.assertEqual(validate_csv(self.source, delimiter=delimiter)["columns"], ["a", "b"])

    def test_malformed_late_records_encoding_headers_and_typed_values_fail_privately(self):
        cases = [
            (b"", None),
            (b"A,a\n1,2\n", None),
            (b"a\nvalid\n\n", None),
            (b"a\nvalid\nwrong,width\n", None),
            (b'a\nvalid\n"unfinished', None),
            (b"a\nvalid\n\xff\n", None),
        ]
        for kind, value in (
            ("integer", "9223372036854775808"),
            ("integer", "private-cell"),
            ("real", "1e9999"),
            ("decimal", "+1"),
            ("boolean", "TRUE"),
        ):
            valid = "true" if kind == "boolean" else "1"
            cases.append((f"a\n{valid}\n{value}\n", schema_for(("a", kind, False))))
        for contents, schema in cases:
            with self.subTest(contents=repr(contents), schema=schema):
                self.write(contents)
                with self.assertRaises(CsvImportError) as caught:
                    validate_csv(self.source, schema=schema)
                self.assertNotIn("private-cell", str(caught.exception))
                self.assertNotIn(str(self.source), str(caught.exception))
                self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_schemas_options_and_nonregular_sources_are_rejected(self):
        self.write("a\n1\n")
        for options in (
            {"table": "sqlite_private"}, {"delimiter": "|"}, {"schema": {}},
            {"schema": schema_for(("a", "integer", 1))},
            {"schema": schema_for(("wrong", "text", False))},
        ):
            with self.subTest(options=options):
                with self.assertRaises(CsvImportError):
                    validate_csv(self.source, **options)
        for source in (self.directory, self.directory / "missing", None, b"input.csv"):
            with self.subTest(source=source):
                with self.assertRaises(CsvImportError):
                    validate_csv(source)

    def test_source_column_field_and_row_limits(self):
        with self.source.open("wb") as source:
            source.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaises(CsvImportError):
            validate_csv(self.source)
        self.write(",".join("c" + str(index) for index in range(1_001)) + "\n")
        with self.assertRaises(CsvImportError):
            validate_csv(self.source)
        self.write("a\n" + "x" * 65_537 + "\n")
        with self.assertRaises(CsvImportError):
            validate_csv(self.source)
        with mock.patch.object(importer, "MAX_ROWS", 2):
            self.write("a\nx\ny\n")
            self.assertEqual(validate_csv(self.source)["rows"], 2)
            self.write("a\nx\ny\nz\n")
            with self.assertRaises(CsvImportError):
                validate_csv(self.source)
        self.assertEqual(list(self.directory.iterdir()), [self.source])

    def test_caller_csv_field_limit_is_preserved(self):
        original = csv.field_size_limit()
        try:
            csv.field_size_limit(4)
            self.write("a\n12345\n")
            with self.assertRaises(CsvImportError):
                validate_csv(self.source)
            self.assertEqual(csv.field_size_limit(), 4)
        finally:
            csv.field_size_limit(original)

    def test_cli_check_summary_and_schema_without_output(self):
        self.write("n;label\n1;example\n2;\n")
        schema = schema_for(("n", "integer", False), ("label", "text", True))
        schema_path = self.directory / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        before = sorted(self.directory.iterdir())
        result = self.cli(
            "--check", self.source, "--delimiter", "semicolon", "--table", "items",
            "--schema", schema_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(
            json.loads(result.stdout),
            validate_csv(self.source, table="items", schema=schema, delimiter=";"),
        )
        self.assertEqual(sorted(self.directory.iterdir()), before)

    def test_cli_rejects_check_output_missing_import_output_and_bad_inputs(self):
        self.write("a\nvalid\n")
        destination = self.directory / "private-output.sqlite3"
        destination.write_bytes(b"preserve destination")
        for arguments in (
            (self.source,),
            (self.source, destination, "--check"),
            (self.source, "--check", "--table", "sqlite_private"),
        ):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(str(destination), result.stderr)
                self.assertNotIn(str(self.source), result.stderr)
        self.write("a\nvalid\nwrong,width\n")
        result = self.cli(self.source, "--check")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "error: CSV import failed\n")
        self.assertEqual(destination.read_bytes(), b"preserve destination")

    def test_cli_check_rejects_duplicate_schema_keys(self):
        self.write("a\n1\n")
        schema_path = self.directory / "schema.json"
        schema_path.write_text('{"columns":[],"columns":[]}', encoding="utf-8")
        result = self.cli(self.source, "--check", "--schema", schema_path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "error: CSV import failed\n")


if __name__ == "__main__":
    unittest.main()
