#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "build_afl_catalog.py"
spec = importlib.util.spec_from_file_location("afl_catalog", TOOL)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


class AflCatalogTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"FactorID": 963, "FactorCode": "EPSNumRevFY1C", "Name": "Number of EPS FY1 Revisions"},
            {"FactorID": 104, "FactorCode": "10DMACD", "Name": "MACD"},
            {"FactorID": None, "FactorCode": None},
        ]
        self.files = {104: "104.parquet", 963: "963.parquet"}

    def test_names_ids_and_digit_prefix_share_same_file(self):
        fields = catalog.build_catalog(self.rows, self.files)["fields"]
        self.assertEqual(fields["EPSNumRevFY1C"], fields["afl_963"])
        self.assertEqual(fields["afl_10DMACD"], "104.parquet")
        self.assertNotIn("number_of_revision", fields)

    def test_explicit_alias_resolves_exact_code_or_id(self):
        result = catalog.build_catalog(self.rows, self.files,
            ["number_of_revision=EPSNumRevFY1C", "another_name=963"])
        self.assertEqual(result["fields"]["number_of_revision"], "963.parquet")
        self.assertEqual(result["fields"]["another_name"], "963.parquet")

    def test_ambiguous_or_invalid_aliases_fail(self):
        for alias in ("close=963", "returns=963", "bad-name=963", "EPSNumRevFY1C=104", "x=unknown", "x"):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                catalog.build_catalog(self.rows, self.files, [alias])

    def test_unknown_files_and_duplicate_dictionary_rows_fail(self):
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.rows, {99: "99.parquet"})
        with self.assertRaises(ValueError):
            catalog.build_catalog(self.rows + [self.rows[0]], self.files)

    def test_cli_only_reads_dictionary_and_file_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # These files are intentionally not valid Parquet: catalog creation
            # must never read a factor's values.
            (root / "963.parquet").write_bytes(b"not a parquet file")
            (root / "activeFlag.parquet").write_bytes(b"not a parquet file")
            dictionary = root / "dictionary.csv"
            dictionary.write_text("FactorID,FactorCode,Name\n963,EPSNumRevFY1C,Revisions\n")
            output = root / "catalog.json"
            command = [sys.executable,str(TOOL),"--dictionary",str(dictionary),
                       "--data-dir",str(root),"--output",str(output)]
            subprocess.run(command,check=True,capture_output=True)
            result = json.loads(output.read_text())
            self.assertEqual(len(result["factors"]),1)
            self.assertEqual(result["source"]["dictionary"],"dictionary.csv")
            before = output.stat().st_mtime_ns
            subprocess.run(command,check=True,capture_output=True)
            self.assertEqual(before,output.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
