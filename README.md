# csv-sqlite-import

Turn one local CSV file into a **new SQLite database**, with optional explicit
column types, a source SHA-256 digest, and atomic publication without overwriting
an existing destination. Python 3.10+; standard library only. No installation,
service, account, or network access is needed.

[Leia em português](README.pt-BR.md).

## Quick start

Run from this repository:

```sh
python -m csv_sqlite_import examples/inventory.csv inventory.sqlite3 \
  --table inventory --schema examples/inventory.schema.json
```

The command prints one JSON object containing `table`, `rows`, `columns`, and
`sha256`. The example has three synthetic inventory records, including a quoted
comma and a multiline note. Omit `--schema` to preserve every field as text.

```python
from pathlib import Path
from csv_sqlite_import import CsvImportError, import_csv

try:
    summary = import_csv(
        Path("examples/inventory.csv"),
        Path("inventory-text.sqlite3"),
        table="inventory",
    )
except CsvImportError:
    print("The input could not be imported.")
else:
    print(summary)
```

`source` and `destination` accept strings and `os.PathLike` objects returning
strings. Specify local file paths; shell expansion, directory searches, downloads,
and input modification are never performed. Relative paths use the process's
current directory. The output must not exist, even as a broken symlink.

## Types and schemas

A schema must have exactly this structure, with columns in the same order and
with names matching the CSV header exactly:

```json
{
  "columns": [
    {"name": "label", "type": "text", "nullable": false},
    {"name": "count", "type": "integer", "nullable": true}
  ]
}
```

Every column requires exactly `name`, `type`, and `nullable`; extra keys are
rejected. The API accepts only exact JSON-native dictionaries, lists, strings,
and booleans in this structure. In particular, `nullable` must be a boolean, not
`0`, `1`, or a string. The CLI rejects duplicate JSON keys, nonfinite constants,
and schemas larger than 64 KiB. Schema files must be regular UTF-8 JSON files.
Schemas are passive data: no expressions, callbacks, date inference, locale
conversion, extensions, or code are evaluated.

| Type | Accepted input | SQLite storage |
| --- | --- | --- |
| `text` | Original field, including whitespace | `TEXT` |
| `integer` | ASCII `-?(0\|[1-9][0-9]*)`, signed 64-bit range | `INTEGER` |
| `real` | JSON-number grammar, finite after conversion | `REAL` |
| `boolean` | Exactly `true` or `false` | `INTEGER` 1 or 0 |
| `decimal` | JSON-number grammar, retained exactly | `TEXT` |

The JSON-number grammar is `-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`.
It excludes leading plus signs, leading zeroes, whitespace, `NaN`, and infinity.
Real values use Python's binary floating point and can round or underflow; use
`decimal` to retain the original number spelling, trailing zeroes, and exponent.
Decimal values are stored as text, so SQLite will order them as text unless a
query explicitly converts them.

For nullable columns, an empty field becomes SQL `NULL`, including nullable
`text`. For nonnullable `text`, an empty field remains `""`. Other nonnullable
types reject empty fields. Quoted and unquoted empty CSV fields behave the same.
Without a schema all columns are nonnullable text, with no inference or trimming.

## CSV rules and limits

- Source: one regular UTF-8 file, optionally beginning with a UTF-8 BOM, at most
  **64 MiB**. It is read once into a bounded byte snapshot. This is not an
  unlimited streaming importer; decoding and parsing require additional memory.
- The SHA-256 digest covers the exact imported source bytes, including a BOM,
  line endings, and quotes. They are not normalized before hashing.
- Parsing uses `csv.reader(strict=True)` with `newline=''`. Quoted commas,
  embedded newlines, and whitespace are preserved.
- Delimiters are comma, tab, and semicolon. The API uses `','`, `'\t'`, or `';'`;
  the CLI uses `--delimiter comma|tab|semicolon`.
- A header is required: 1–1,000 nonempty names, at most 128 characters each, no
  NUL characters, and no duplicate names after Unicode case folding. Names are
  not trimmed. A header-only file creates an empty table.
- At most 1,000,000 data records and 65,536 characters per field. Every data
  record must have exactly the header's width. Blank records are invalid.
- The process-wide `csv.field_size_limit()` is never changed. A caller's smaller
  standard-library field limit can reject a field below this tool's own limit.
- Table names must match ASCII `[A-Za-z_][A-Za-z0-9_]{0,62}` and cannot start with
  `sqlite_` in any case. Column names may contain punctuation and SQL-shaped
  text; every identifier is quoted and all values use bound parameters.

## Publication and failures

The importer creates a mode-0600 staging database in the output's parent
directory. It creates the table and imports rows in one transaction, runs
`PRAGMA integrity_check`, commits, and closes SQLite. It then publishes with
`os.link`, which fails atomically if any destination entry already exists. The
temporary database and its own journal files are cleaned up afterward.

Invalid CSV, schema, types, or ordinary I/O/SQLite failures raise
`CsvImportError`, a `ValueError` subclass. Error messages omit paths, SQL, and
cell contents. Failed imports publish nothing; existing files, directories,
symlinks, and broken symlinks stay untouched. Concurrent imports to the same
destination can have only one winner.

This assumes a trusted local filesystem with hard-link support and an existing
output parent directory. It does not promise power-loss durability, protection
against a hostile actor changing the parent directory, or consistent results
when another process rewrites the source while it is being read. The importer
does not append, update, overwrite, open an existing destination database, or
load triggers, extensions, or providers.

CLI success exits with status 0 and JSON on stdout. Failure exits with status 2,
a generic message on stderr, and no JSON output. `--help` exits with status 0.

## Development

```sh
python -m unittest discover -s tests -v
```

The public tests use temporary directories and synthetic data. They cover
publication races, rollback and cleanup, filesystem restrictions, schema/type
validation, exact digests, SQL-shaped headers, quoting, BOMs, and multiline CSV.
GitHub Actions runs them on Python 3.10, 3.12, and 3.14, with read-only repository
permissions and checkout credentials disabled. No third-party dependencies are
required.

MIT © 2026 Martin Lofrano. See [LICENSE](LICENSE).
