#!/usr/bin/env python3
"""Build an AFL name/file catalog without reading any factor values.

The output is JSON (also valid YAML) for datasets[].catalog. CSV dictionaries
use the standard library; .xlsx dictionaries require openpyxl only at generation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import tempfile

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
RESERVED = {"open", "high", "low", "close", "volume", "returns"}


def read_dictionary(path: Path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as source:
            yield from csv.DictReader(source)
        return
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Dictionary must be .xlsx or .csv")
    try:
        from openpyxl import load_workbook
    except ImportError as error:
        raise RuntimeError("Reading .xlsx requires openpyxl; CSV needs no extra package") from error
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = iter(workbook.active.values)
        headers = next(rows)
        for values in rows:
            yield dict(zip(headers, values))
    finally:
        workbook.close()


def build_catalog(rows, available_files: dict[int, str], aliases=()):
    by_id = {}
    by_code = {}
    for row in rows:
        raw_id = row.get("FactorID")
        raw_code = row.get("FactorCode")
        if raw_id is None and raw_code is None:
            continue
        if isinstance(raw_id, bool) or not re.fullmatch(r"[0-9]+", str(raw_id)):
            raise ValueError(f"Invalid FactorID: {raw_id!r}")
        factor_id = int(raw_id)
        code = str(raw_code or "").strip()
        if factor_id <= 0 or not code:
            raise ValueError(f"Missing factor code or invalid id: {raw_id!r}")
        if factor_id in by_id or code in by_code:
            raise ValueError(f"Duplicate factor id/code: {factor_id}/{code}")
        by_id[factor_id] = {"id": factor_id, "code": code, "name": str(row.get("Name") or code)}
        by_code[code] = factor_id
    if not available_files:
        raise ValueError("No numeric AFL .parquet files found")
    unknown = sorted(set(available_files) - set(by_id))
    if unknown:
        raise ValueError(f"Factor files missing from dictionary: {unknown}")
    fields = {}

    def add(name, factor_id):
        if not IDENTIFIER.fullmatch(name) or name in RESERVED:
            raise ValueError(f"Invalid or reserved XML field name: {name!r}")
        if factor_id not in available_files:
            raise ValueError(f"No data file for factor {factor_id}")
        path = available_files[factor_id]
        if name in fields and fields[name] != path:
            raise ValueError(f"Ambiguous field name: {name!r}")
        fields[name] = path

    factors = []
    for factor_id in sorted(available_files):
        record = by_id[factor_id]
        code = record["code"]
        readable = code if IDENTIFIER.fullmatch(code) and code not in RESERVED else "afl_" + code
        if not IDENTIFIER.fullmatch(readable):
            # Always provide the unambiguous numeric-id alias, even when the
            # vendor's display code cannot be used in an XML expression.
            readable = f"afl_{factor_id}"
        add(f"afl_{factor_id}", factor_id)
        add(readable, factor_id)
        factors.append({**record, "field": readable, "file": available_files[factor_id]})
    for specification in aliases:
        name, separator, target = specification.partition("=")
        if not separator or not name or not target:
            raise ValueError("Aliases use NAME=FactorCode or NAME=FactorID")
        factor_id = int(target) if target.isascii() and target.isdigit() else by_code.get(target)
        if factor_id is None:
            raise ValueError(f"Unknown alias target: {target!r}")
        add(name, factor_id)
    return {"schema_version": 1, "fields": dict(sorted(fields.items())), "factors": factors}


def write_catalog(path: Path, document):
    content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".afl_catalog_", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--alias", action="append", default=[], metavar="NAME=ID_OR_CODE")
    args = parser.parse_args()
    try:
        files = {}
        for path in sorted(args.data_dir.glob("*.parquet")):
            if re.fullmatch(r"[0-9]+", path.stem):
                factor_id = int(path.stem)
                if factor_id in files:
                    raise ValueError(f"Duplicate numeric factor file: {path.name}")
                files[factor_id] = path.name
        catalog = build_catalog(read_dictionary(args.dictionary), files, args.alias)
        catalog["source"] = {
            "dictionary": args.dictionary.name,
            "sha256": hashlib.sha256(args.dictionary.read_bytes()).hexdigest(),
        }
        write_catalog(args.output, catalog)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"AFL catalog error: {error}\n")
    print(f"Registered {len(catalog['factors'])} factors / {len(catalog['fields'])} names in {args.output}")


if __name__ == "__main__":
    main()
