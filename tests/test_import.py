"""Public contract tests using only synthetic, local, temporary fixtures."""

import concurrent.futures
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import csv_sqlite_import as importer
from csv_sqlite_import import CsvImportError, import_csv


PROJECT = Path(__file__).resolve().parents[1]


def schema_for(*columns):
    return {
        "columns": [
            {"name": name, "type": kind, "nullable": nullable}
            for name, kind, nullable in columns
        ]
    }


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "input.csv"
        self.destination = self.directory / "output.sqlite3"

    def write(self, contents):
        self.source.write_bytes(contents if isinstance(contents, bytes) else contents.encode())

    def records(self, table="records"):
        with sqlite3.connect(self.destination) as connection:
            return connection.execute('SELECT * FROM "' + table + '"').fetchall()

    def assert_clean(self):
        self.assertEqual(list(self.directory.glob(".csv-sqlite-import-*")), [])

    def assert_rejected(self, contents, **options):
        self.write(contents)
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.destination, **options)
        self.assertFalse(os.path.lexists(self.destination))
        self.assert_clean()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "csv_sqlite_import", *map(str, arguments)],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_bom_multiline_quotes_whitespace_and_exact_digest(self):
        original = b'\xef\xbb\xbflabel,note,empty\r\n" a, b ","first\r\nsecond ""quote""",\r\n'
        self.write(original)
        summary = import_csv(self.source, self.destination)
        self.assertEqual(
            summary,
            {
                "table": "records",
                "rows": 1,
                "columns": ["label", "note", "empty"],
                "sha256": hashlib.sha256(original).hexdigest(),
            },
        )
        self.assertEqual(self.records(), [(" a, b ", 'first\r\nsecond "quote"', "")])
        self.assertEqual(self.source.read_bytes(), original)
        self.assert_clean()

    def test_header_only(self):
        self.write("name,value\n")
        self.assertEqual(import_csv(self.source, self.destination)["rows"], 0)
        self.assertEqual(self.records(), [])

    def test_sql_shaped_names_and_values_are_literal(self):
        names = ['x"); DROP TABLE records; --', 'a"b', "select"]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(names)
        writer.writerow(["'); DROP TABLE records; --", '"quoted"', "  keep  "])
        self.write(output.getvalue())
        summary = import_csv(self.source, self.destination)
        self.assertEqual(summary["columns"], names)
        self.assertEqual(self.records(), [("'); DROP TABLE records; --", '"quoted"', "  keep  ")])
        with sqlite3.connect(self.destination) as connection:
            self.assertEqual(
                [row[1] for row in connection.execute('PRAGMA table_info("records")')], names
            )

    def test_explicit_types_and_storage(self):
        self.write("n,r,b,d,t\n-9223372036854775808,1.25e2,true,1.2300,\n9223372036854775807,-0,false,-2.5E+9, yes \n")
        schema = schema_for(
            ("n", "integer", False), ("r", "real", False),
            ("b", "boolean", False), ("d", "decimal", False), ("t", "text", False),
        )
        import_csv(self.source, self.destination, schema=schema)
        self.assertEqual(
            self.records(),
            [(-(2**63), 125.0, 1, "1.2300", ""), (2**63 - 1, 0.0, 0, "-2.5E+9", " yes ")],
        )
        with sqlite3.connect(self.destination) as connection:
            types = connection.execute(
                "SELECT typeof(n), typeof(r), typeof(b), typeof(d), typeof(t) FROM records"
            ).fetchone()
        self.assertEqual(types, ("integer", "real", "integer", "text", "text"))

    def test_nullable_empty_text_and_typed_values(self):
        self.write('text,number,real,boolean,decimal,required\n,"",,,,\n')
        schema = schema_for(
            ("text", "text", True), ("number", "integer", True),
            ("real", "real", True), ("boolean", "boolean", True),
            ("decimal", "decimal", True), ("required", "text", False),
        )
        import_csv(self.source, self.destination, schema=schema)
        self.assertEqual(self.records(), [(None, None, None, None, None, "")])

    def test_integer_rejects_overflow_and_non_ascii_or_noncanonical_values(self):
        for value in ["9223372036854775808", "-9223372036854775809", "+1", "01", "-01", "1.0", "１", " 1", ""]:
            with self.subTest(value=value):
                self.assert_rejected(
                    'n\n"' + value + '"\n', schema=schema_for(("n", "integer", False))
                )

    def test_json_number_and_boolean_rejections(self):
        for kind in ("real", "decimal"):
            for value in ("NaN", "Infinity", "-Infinity", "+1", ".5", "1.", "00", "1_000", "0x10", " 1", ""):
                with self.subTest(kind=kind, value=value):
                    self.assert_rejected(
                        'n\n"' + value + '"\n', schema=schema_for(("n", kind, False))
                    )
        self.assert_rejected("n\n1e9999\n", schema=schema_for(("n", "real", False)))
        for value in ("True", "FALSE", "1", "0", "yes", ""):
            with self.subTest(value=value):
                self.assert_rejected(
                    'n\n"' + value + '"\n', schema=schema_for(("n", "boolean", False))
                )

    def test_decimal_preserves_large_exponents_without_evaluation(self):
        self.write("n\n1.000e999999\n")
        import_csv(self.source, self.destination, schema=schema_for(("n", "decimal", False)))
        self.assertEqual(self.records(), [("1.000e999999",)])

    def test_bad_headers_rows_encoding_and_csv_rollback(self):
        invalid = [
            b"", b"\n", b",name\n1,2\n", b"A,a\n1,2\n",
            "Straße,STRASSE\n1,2\n", "x" * 129 + "\n1\n", "a\x00b\n1\n",
            "a,b\n1\n", "a,b\n1,2,3\n", "a\nvalid\n\n", 'a\nvalid\n"unfinished',
            b"a\n\xff\n",
        ]
        for contents in invalid:
            with self.subTest(contents=repr(contents)[:60]):
                self.assert_rejected(contents)

    def test_schema_requires_exact_keys_order_and_native_types(self):
        class CustomDict(dict):
            pass

        invalid = [
            {}, [], {"columns": []}, {"columns": (), "extra": 1},
            {"columns": [{"name": "a", "type": "text"}]},
            {"columns": [{"name": "a", "type": "text", "nullable": False, "extra": None}]},
            schema_for(("b", "text", False)), schema_for(("a", "date", False)),
            schema_for(("a", "TEXT", False)), schema_for(("a", "text", 0)),
            schema_for(("a", "text", "false")),
            CustomDict(schema_for(("a", "text", False))),
            {"columns": [CustomDict(name="a", type="text", nullable=False)]},
        ]
        for schema in invalid:
            with self.subTest(schema=repr(schema)):
                self.assert_rejected("a\nvalue\n", schema=schema)
        self.assert_rejected(
            "a,b\n1,2\n", schema=schema_for(("b", "text", False), ("a", "text", False))
        )

    def test_invalid_table_names_and_delimiters(self):
        for table in ("", "sqlite_meta", "SQLITE_x", "1name", "é", "a-b", "a;DROP TABLE x", "a" * 64, None):
            with self.subTest(table=table):
                self.assert_rejected("a\nv\n", table=table)
        for delimiter in ("", "||", "|", "\n", None):
            with self.subTest(delimiter=delimiter):
                self.assert_rejected("a\nv\n", delimiter=delimiter)

    def test_tab_and_semicolon(self):
        for number, delimiter in enumerate(("\t", ";")):
            self.write("a" + delimiter + "b\nx" + delimiter + "y\n")
            destination = self.directory / (str(number) + ".sqlite3")
            summary = import_csv(self.source, destination, table="_Records9", delimiter=delimiter)
            self.assertEqual(summary["columns"], ["a", "b"])
            with sqlite3.connect(destination) as connection:
                self.assertEqual(connection.execute("SELECT * FROM _Records9").fetchall(), [("x", "y")])

    def test_field_boundary_and_global_csv_limit_unchanged(self):
        original_limit = csv.field_size_limit()
        self.write("a\n" + "x" * 65_536 + "\n")
        if original_limit >= 65_536:
            import_csv(self.source, self.destination)
            self.assertEqual(len(self.records()[0][0]), 65_536)
            self.destination.unlink()
        self.assert_rejected("a\n" + "x" * 65_537 + "\n")
        self.assertEqual(csv.field_size_limit(), original_limit)
        try:
            csv.field_size_limit(4)
            self.assert_rejected("a\n12345\n")
            self.assertEqual(csv.field_size_limit(), 4)
        finally:
            csv.field_size_limit(original_limit)

    def test_column_boundary(self):
        names = ["c" + str(index) for index in range(1_000)]
        self.write(",".join(names) + "\n")
        self.assertEqual(import_csv(self.source, self.destination)["columns"], names)
        self.destination.unlink()
        self.assert_rejected(",".join(names + ["overflow"]) + "\n")

    def test_row_limit_rolls_back(self):
        self.write("a\n" + "v\n" * 1_000_001)
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_source_size_bound_with_sparse_file(self):
        with self.source.open("wb") as source:
            source.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_existing_files_directories_and_links_remain_untouched(self):
        self.write("a\nx\n")
        target = self.directory / "target"
        target.write_bytes(b"original target")
        self.destination.write_bytes(b"original destination")
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"original destination")
        self.destination.unlink()
        self.destination.mkdir()
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.destination)
        self.assertTrue(self.destination.is_dir())
        self.destination.rmdir()
        for link_target in (target, self.directory / "does-not-exist"):
            self.destination.symlink_to(link_target)
            with self.assertRaises(CsvImportError):
                import_csv(self.source, self.destination)
            self.assertTrue(self.destination.is_symlink())
            self.assertEqual(os.readlink(self.destination), str(link_target))
            self.destination.unlink()
        self.assertEqual(target.read_bytes(), b"original target")
        self.assert_clean()

    def test_missing_directory_source_and_invalid_paths(self):
        for source in (self.source, self.directory, b"input.csv", None, "invalid\x00path"):
            with self.subTest(source=repr(source)):
                with self.assertRaises(CsvImportError):
                    import_csv(source, self.destination)
                self.assertFalse(self.destination.exists())
        self.write("a\nx\n")
        with self.assertRaises(CsvImportError):
            import_csv(self.source, self.directory / "missing" / "output.sqlite3")
        self.assert_clean()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support is required")
    def test_fifo_source_and_schema_rejected_without_waiting(self):
        fifo = self.directory / "pipe"
        os.mkfifo(fifo)
        result = self.cli(fifo, self.destination)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.write("a\nx\n")
        result = self.cli(self.source, self.destination, "--schema", fifo)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_mode_and_integrity(self):
        self.write("a\nx\n")
        import_csv(self.source, self.destination)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)
        with sqlite3.connect(self.destination) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assert_clean()

    def test_concurrent_atomic_publication_has_one_winner(self):
        sources = [self.directory / "left.csv", self.directory / "right.csv"]
        for index, source in enumerate(sources):
            source.write_text("origin\n" + str(index) + "\n", encoding="utf-8")
        original_link = os.link
        barrier = threading.Barrier(2)

        def publish(source, destination):
            barrier.wait(timeout=10)
            return original_link(source, destination)

        def attempt(source):
            try:
                return import_csv(source, self.destination)
            except CsvImportError:
                return None

        with mock.patch.object(importer.os, "link", side_effect=publish):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, sources))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        winner = next(index for index, result in enumerate(results) if result is not None)
        self.assertEqual(self.records(), [(str(winner),)])
        self.assertEqual(winners[0]["sha256"], hashlib.sha256(sources[winner].read_bytes()).hexdigest())
        self.assert_clean()

    def test_late_destination_creation_is_preserved(self):
        self.write("a\nx\n")
        original_link = os.link

        def competing_link(source, destination):
            Path(destination).write_bytes(b"competing destination")
            return original_link(source, destination)

        with mock.patch.object(importer.os, "link", side_effect=competing_link):
            with self.assertRaises(CsvImportError):
                import_csv(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"competing destination")
        self.assert_clean()

    def test_sqlite_and_publication_errors_are_generic_and_clean(self):
        self.write("a\nx\n")
        for target, error in (
            ("sqlite3.connect", sqlite3.OperationalError("secret SQL /private/path")),
            ("os.link", OSError("secret /private/path")),
        ):
            with self.subTest(target=target):
                with mock.patch("csv_sqlite_import." + target, side_effect=error):
                    with self.assertRaises(CsvImportError) as caught:
                        import_csv(self.source, self.destination)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("/private/path", str(caught.exception))
                self.assertFalse(self.destination.exists())
                self.assert_clean()

    def test_type_failure_after_valid_rows_is_private_and_atomic(self):
        self.write("number\n1\n2\nprivate-cell\n")
        with self.assertRaises(CsvImportError) as caught:
            import_csv(self.source, self.destination, schema=schema_for(("number", "integer", False)))
        message = str(caught.exception)
        self.assertNotIn("private-cell", message)
        self.assertNotIn(str(self.source), message)
        self.assertFalse(self.destination.exists())
        self.assert_clean()

    def test_cli_success_help_and_generic_failure(self):
        self.write("a;b\nx;y\n")
        result = self.cli(self.source, self.destination, "--delimiter", "semicolon")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["rows"], 1)
        self.assertEqual(self.records(), [("x", "y")])
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--schema", result.stdout)
        result = self.cli(self.source, self.destination)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(str(self.destination), result.stderr)
        result = self.cli("--private-value")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private-value", result.stderr)

    def test_cli_json_schema_is_strict_bounded_and_passive(self):
        self.write("a\n1\n")
        schema_path = self.directory / "schema.json"
        invalid = [
            '{"columns":[],"columns":[]}',
            '{"columns":[{"name":"a","type":"text","nullable":false,"nullable":true}]}',
            '{"columns":NaN}', '{"columns":Infinity}', '{"columns":-Infinity}',
            'null', 'true', '[]', '{"columns":', '\ufeff{}',
            ' ' * (64 * 1024 + 1), '[' * 2_000 + ']' * 2_000,
            json.dumps(schema_for(("a", "integer", 1))),
            json.dumps(schema_for(("a", "__import__(\"os\").system(\"echo unsafe\")", False))),
        ]
        for contents in invalid:
            with self.subTest(contents=contents[:60]):
                schema_path.write_text(contents, encoding="utf-8")
                result = self.cli(self.source, self.destination, "--schema", schema_path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "error: CSV import failed\n")
                self.assertFalse(self.destination.exists())
                self.assert_clean()
        schema_path.write_text(json.dumps(schema_for(("a", "integer", False))), encoding="utf-8")
        result = self.cli(self.source, self.destination, "--schema", schema_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [(1,)])


if __name__ == "__main__":
    unittest.main()
