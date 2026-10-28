"""Run from the repository with: python -m examples.check_inventory.

Validate the synthetic inventory, including every typed value, without creating
a database. Destination filesystem and SQLite availability are not checked.
"""

import json
from pathlib import Path

from csv_sqlite_import import validate_csv


def main():
    examples = Path(__file__).resolve().parent
    schema = json.loads((examples / "inventory.schema.json").read_text(encoding="utf-8"))
    summary = validate_csv(examples / "inventory.csv", table="inventory", schema=schema)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
