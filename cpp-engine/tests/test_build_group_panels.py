#!/usr/bin/env python3
"""Synthetic-fixture checks for tools/data/build_group_panels.py. No real cache is read.

tools/data is a symlink to /home/jgo/universe_builder/cache (outside this repo); the builder script lives there.
CTest registers this test only when that link resolves (see CMakeLists.txt). Do not delete the link."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as error:
    # 77 is the CTest SKIP_RETURN_CODE for this test: a missing dependency must never pass.
    print(f"SKIP: {sys.executable} cannot import numpy/pandas/pyarrow ({error}); "
          "configure with -DLONGSHORT_PYARROW_PYTHON=<python with pyarrow>", file=sys.stderr)
    sys.exit(77)

TOOL = Path(__file__).resolve().parents[1] / "tools" / "data" / "build_group_panels.py"
spec = importlib.util.spec_from_file_location("build_group_panels", TOOL)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

# Deliberately not in sorted order: the panel must follow close.parquet's column order.
SYMBOLS = ["104", "101", "106", "103", "102", "105"]
DATES = pd.DatetimeIndex(pd.bdate_range("2020-01-02", periods=5))
# 101/102 Energy, 103/104 Financials; 3 industries; 105 unclassified; 106 absent from the master.
MASTER_ROWS = [
    (101, "Energy", "Oil and Gas Exploration"),
    (102, "Energy", "Oil and Gas Refining"),
    (103, "Financials", "Banks"),
    (104, "Financials", "Banks"),
    (105, None, None),
    (999, "Energy", "Oil and Gas Exploration"),  # not in the cache
]
# Deliberately holds a label no cache symbol uses, and is not in sorted order.
SECTOR_INDEX = [None, "Energy", "Information Technology", "Financials"]
EXPECTED_SECTOR = {"101": 1.0, "102": 1.0, "103": 3.0, "104": 3.0, "105": None, "106": None}
# industry codes are sorted labels: 1 Banks, 2 Oil and Gas Exploration, 3 Oil and Gas Refining
EXPECTED_INDUSTRY = {"101": 2.0, "102": 3.0, "103": 1.0, "104": 1.0, "105": None, "106": None}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_fixture(root, master_rows=MASTER_ROWS, id_as_index=True):
    """Synthetic cache (close/TimeStamp/tradingItemId), master and sector index file."""
    root = Path(root)
    (root / "cache" / "BaseData").mkdir(parents=True, exist_ok=True)
    close = pd.DataFrame(np.arange(len(DATES) * len(SYMBOLS), dtype=np.float32).reshape(len(DATES), -1),
                         index=DATES, columns=SYMBOLS)
    close.to_parquet(root / "cache" / "BaseData" / "close.parquet")
    pd.DataFrame({"TimeStamp": DATES}).to_parquet(root / "cache" / "BaseData" / "TimeStamp.parquet")
    pd.DataFrame({"tradingItemId": [int(s) for s in SYMBOLS]}).to_parquet(
        root / "cache" / "BaseData" / "tradingItemId.parquet")
    master = pd.DataFrame(master_rows, columns=["tradingItemId", "sector", "simpleIndustryDescription"])
    if id_as_index:
        master = master.set_index("tradingItemId")
    master.to_parquet(root / "master.parquet", index=id_as_index)
    pd.DataFrame({"sectorIx": pd.Series(SECTOR_INDEX, dtype=object)}).to_parquet(root / "sectorIx.parquet")
    return root


def write_label_index(path, column, labels):
    pd.DataFrame({column: pd.Series([None] + list(labels), dtype=object)}).to_parquet(path)


def run(root, output, extra=(), expect=0):
    command = [sys.executable, str(TOOL),
               "--master", str(Path(root) / "master.parquet"),
               "--cache", str(Path(root) / "cache"),
               "--output", str(output),
               "--sector-index", str(Path(root) / "sectorIx.parquet"), *extra]
    done = subprocess.run(command, capture_output=True, text=True)
    assert done.returncode == expect, f"exit {done.returncode}\n{done.stdout}\n{done.stderr}"
    return done


class GroupPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = write_fixture(self.directory.name)
        self.output = self.root / "out"
        self.addCleanup(self.directory.cleanup)

    def build(self, extra=(), output=None):
        output = output or self.output
        run(self.root, output, extra)
        return output

    def test_shape_order_and_dtype_follow_close_parquet(self):
        output = self.build()
        for name in ("sector", "industry"):
            panel = pd.read_parquet(output / f"{name}.parquet")
            self.assertEqual(list(panel.columns), SYMBOLS)
            self.assertTrue(panel.index.equals(DATES))
            self.assertEqual(set(panel.dtypes), {np.dtype("float32")})
            # static classification: every column is constant down the calendar
            for column in panel.columns:
                values = panel[column].to_numpy()
                self.assertTrue(np.all(np.isnan(values)) or len(np.unique(values)) == 1)

    def test_codes_match_master_labels_through_the_index_files(self):
        output = self.build()
        sector_labels = pd.read_parquet(output / "sectorIx.parquet")["sectorIx"].tolist()
        industry_labels = pd.read_parquet(output / "industryIx.parquet")["industryIx"].tolist()
        self.assertEqual(sector_labels, SECTOR_INDEX)       # order taken from --sector-index, unused label kept
        self.assertEqual(industry_labels, [None, "Banks", "Oil and Gas Exploration", "Oil and Gas Refining"])
        sector = pd.read_parquet(output / "sector.parquet")
        industry = pd.read_parquet(output / "industry.parquet")
        master = {str(row[0]): row for row in MASTER_ROWS}
        for symbol in SYMBOLS:
            if EXPECTED_SECTOR[symbol] is None:
                continue
            code = sector[symbol].iloc[0]
            self.assertEqual(code, EXPECTED_SECTOR[symbol])
            self.assertEqual(sector_labels[int(code)], master[symbol][1])
            code = industry[symbol].iloc[0]
            self.assertEqual(code, EXPECTED_INDUSTRY[symbol])
            self.assertEqual(industry_labels[int(code)], master[symbol][2])

    def test_unclassified_and_absent_symbols_are_nan_for_every_date(self):
        output = self.build()
        counts = json.loads((output / "MANIFEST.json").read_text())["counts"]
        for name in ("sector", "industry"):
            panel = pd.read_parquet(output / f"{name}.parquet")
            for symbol in ("105", "106"):
                self.assertTrue(panel[symbol].isna().all(), f"{name}[{symbol}] should be all NaN")
            self.assertEqual(int(panel.isna().to_numpy().sum()), 2 * len(DATES))
        self.assertEqual(counts["symbols_in_master"], 5)          # 101-105
        self.assertEqual(counts["symbols_absent_from_master"], 1)  # 106
        self.assertEqual(counts["sector_nan_symbols"], 2)
        self.assertEqual(counts["sector_nan_cells"], 2 * len(DATES))
        self.assertEqual(counts["sector_k"], 3)
        self.assertEqual(counts["industry_k"], 3)

    def test_master_with_id_as_a_column_gives_the_same_panel(self):
        other = write_fixture(Path(self.directory.name) / "v2", id_as_index=False)
        first = self.build()
        second = run(other, other / "out") and (other / "out")
        self.assertEqual(sha256(first / "sector.parquet"), sha256(second / "sector.parquet"))
        self.assertEqual(sha256(first / "industry.parquet"), sha256(second / "industry.parquet"))

    def test_industry_spanning_two_sectors_fails(self):
        rows = list(MASTER_ROWS)
        rows[2] = (103, "Financials", "Oil and Gas Exploration")  # same industry under two sectors
        root = write_fixture(Path(self.directory.name) / "nested", master_rows=rows)
        done = run(root, root / "out", expect=1)
        self.assertIn("Oil and Gas Exploration", done.stderr)
        self.assertIn("span", done.stderr)
        self.assertFalse((root / "out" / "sector.parquet").exists())

    def test_rebuild_is_byte_identical(self):
        output = self.build()
        names = [item["name"] for item in json.loads((output / "MANIFEST.json").read_text())["files"]]
        names.append("MANIFEST.json")
        before = {name: sha256(output / name) for name in names}
        self.build()  # same inputs, same directory
        self.assertEqual(before, {name: sha256(output / name) for name in names})
        elsewhere = self.build(output=self.root / "out2")
        for name in ("sector.parquet", "industry.parquet", "sectorIx.parquet", "industryIx.parquet"):
            self.assertEqual(before[name], sha256(elsewhere / name), name)

    def test_row_group_split_does_not_change_the_values(self):
        plain = pd.read_parquet(self.build() / "sector.parquet")
        split = pd.read_parquet(self.build(extra=["--row-group-rows", "2"], output=self.root / "split")
                                / "sector.parquet")
        pd.testing.assert_frame_equal(plain, split)

    def test_check_against_reports_cell_agreement_and_mismatching_symbols(self):
        reference = pd.DataFrame({symbol: np.full(len(DATES), EXPECTED_SECTOR[symbol] or np.nan,
                                                  dtype=np.float64) for symbol in SYMBOLS}, index=DATES)
        reference.loc[DATES[2:], "103"] = 5.0  # a symbol whose reference code changes over time
        path = self.root / "reference_sector.parquet"
        reference.to_parquet(path)
        output = self.build(extra=["--check-against", str(path)])
        check = json.loads((output / "MANIFEST.json").read_text())["check"]
        self.assertTrue(check["same_index"] and check["same_columns"])
        self.assertEqual(check["cells"], len(DATES) * len(SYMBOLS))
        self.assertEqual(check["agreeing_cells"], len(DATES) * len(SYMBOLS) - 3)
        self.assertEqual(check["mismatching_symbols"], 1)
        self.assertEqual(check["mismatching_symbols_varying_reference"], 1)
        self.assertEqual(check["mismatching_symbols_by_kind"], {"reference_varies": 1})
        self.assertEqual(check["mismatching_cells_by_kind"],
                         {"reference_nan_built_code": 0, "built_nan_reference_code": 0,
                          "both_finite_differ": 3})
        self.assertEqual(check["mismatch_detail"]["103"]["reference_codes"], [3.0, 5.0])
        report = (output / "build_report.md").read_text()
        self.assertIn("--check-against", report)
        self.assertIn("90.000000%", report)
        self.assertIn("| 103 | 3 | reference_varies | 3.0 | [3.0, 5.0] |", report)

    def test_check_against_separates_partial_fill_from_a_different_code(self):
        reference = pd.DataFrame({symbol: np.full(len(DATES), EXPECTED_SECTOR[symbol] or np.nan,
                                                  dtype=np.float64) for symbol in SYMBOLS}, index=DATES)
        reference.loc[DATES[:3], "101"] = np.nan   # base filled only the tail of the calendar
        reference.loc[:, "104"] = 1.0              # constant but different code
        reference.loc[:, "105"] = 2.0              # classified there, unclassified in the master
        path = self.root / "reference_sector.parquet"
        reference.to_parquet(path)
        check = json.loads((self.build(extra=["--check-against", str(path)])
                            / "MANIFEST.json").read_text())["check"]
        self.assertEqual(check["mismatching_symbols_by_kind"],
                         {"code_differs": 1, "reference_partial": 1, "unclassified_here": 1})
        self.assertEqual(check["mismatching_cells_by_kind"],
                         {"reference_nan_built_code": 3, "built_nan_reference_code": 5,
                          "both_finite_differ": 5})
        # the partial-fill symbol is summarised separately; the real code disagreements are listed in full
        self.assertEqual(sorted(check["mismatch_detail"]), ["104", "105"])
        self.assertEqual(check["reference_partial_symbols"], 1)
        self.assertEqual(check["reference_partial_sample"]["101"]["reference_first_finite_date"],
                         str(DATES[3].date()))

    def test_report_and_manifest_record_axis_cross_checks_and_hashes(self):
        output = self.build()
        manifest = json.loads((output / "MANIFEST.json").read_text())
        checks = manifest["axes"]["cross_checks"]
        self.assertTrue(checks["TimeStamp.parquet"]["matches_close_index"])
        self.assertTrue(checks["tradingItemId.parquet"]["matches_close_columns"])
        self.assertEqual(manifest["axes"]["rows"], len(DATES))
        self.assertEqual(manifest["axes"]["columns"], len(SYMBOLS))
        report = (output / "build_report.md").read_text()
        self.assertEqual([item["name"] for item in manifest["files"]],
                         ["sector.parquet", "industry.parquet", "sectorIx.parquet",
                          "industryIx.parquet", "build_report.md"])
        for item in manifest["files"]:
            self.assertEqual(item["sha256"], sha256(output / item["name"]))
            # the report lists every file but itself; MANIFEST.json carries the report hash too
            if item["name"] != "build_report.md":
                self.assertIn(item["sha256"], report)

    def test_physical_schema_is_the_wide_loader_contract(self):
        output = self.build()
        for name in ("sector", "industry"):
            schema = pq.ParquetFile(output / f"{name}.parquet").schema_arrow
            self.assertEqual(schema.names, SYMBOLS + ["__index_level_0__"])
            self.assertEqual(schema.field("__index_level_0__").type, pa.timestamp("ns"))
            self.assertEqual({schema.field(symbol).type for symbol in SYMBOLS}, {pa.float32()})
            self.assertEqual(schema.pandas_metadata["index_columns"], ["__index_level_0__"])

    def test_duplicate_trading_item_id_keeps_the_first_row(self):
        rows = list(MASTER_ROWS)
        rows.insert(3, (rows[0][0], "Utilities", "Electric Utilities"))  # second row for the same id
        root = write_fixture(Path(self.directory.name) / "duplicate", master_rows=rows)
        run(root, root / "out")
        manifest = json.loads((root / "out" / "MANIFEST.json").read_text())
        self.assertEqual(manifest["counts"]["master_duplicate_rows"], 1)
        self.assertEqual(manifest["counts"]["master_rows"], len(MASTER_ROWS))
        symbol = str(rows[0][0])
        # kept the first row, so the dropped row's labels never reach the code tables
        self.assertEqual(pd.read_parquet(root / "out" / "sector.parquet")[symbol].iloc[0],
                         EXPECTED_SECTOR[symbol])
        self.assertNotIn("Utilities", pd.read_parquet(root / "out" / "sectorIx.parquet")["sectorIx"].tolist())
        self.assertNotIn("Electric Utilities",
                         pd.read_parquet(root / "out" / "industryIx.parquet")["industryIx"].tolist())

    def test_empty_and_whitespace_labels_count_as_unclassified(self):
        rows = list(MASTER_ROWS)
        rows[0] = (rows[0][0], "   ", "")      # whitespace-only sector, empty industry
        rows[1] = (rows[1][0], "Energy", "\t")  # only the industry is blank
        root = write_fixture(Path(self.directory.name) / "blank", master_rows=rows)
        run(root, root / "out")
        sector = pd.read_parquet(root / "out" / "sector.parquet")
        industry = pd.read_parquet(root / "out" / "industry.parquet")
        first, second = str(rows[0][0]), str(rows[1][0])
        self.assertTrue(sector[first].isna().all())
        self.assertTrue(industry[first].isna().all())
        self.assertEqual(sector[second].iloc[0], 1.0)      # Energy still classified
        self.assertTrue(industry[second].isna().all())
        counts = json.loads((root / "out" / "MANIFEST.json").read_text())["counts"]
        self.assertEqual(counts["sector_nan_symbols"], 3)   # the blank one plus 105/106
        self.assertEqual(counts["industry_nan_symbols"], 4)
        self.assertEqual(counts["symbols_in_master"], 5)    # blank labels are still master rows

    def test_industry_index_fixes_the_industry_code_order(self):
        order = ["Oil and Gas Refining", "Banks", "Oil and Gas Exploration"]
        path = self.root / "industryIx.parquet"
        write_label_index(path, "industryIx", order)
        output = self.build(extra=["--industry-index", str(path)])
        self.assertEqual(pd.read_parquet(output / "industryIx.parquet")["industryIx"].tolist(),
                         [None] + order)
        industry = pd.read_parquet(output / "industry.parquet")
        self.assertEqual(industry["101"].iloc[0], 3.0)   # Oil and Gas Exploration
        self.assertEqual(industry["102"].iloc[0], 1.0)   # Oil and Gas Refining
        self.assertEqual(industry["103"].iloc[0], 2.0)   # Banks

    def test_unknown_sector_label_is_rejected(self):
        rows = list(MASTER_ROWS)
        rows[0] = (101, "Utilities", "Oil and Gas Exploration")  # not in the given sectorIx
        root = write_fixture(Path(self.directory.name) / "unknown", master_rows=rows)
        done = run(root, root / "out", expect=1)
        self.assertIn("Utilities", done.stderr)


if __name__ == "__main__":
    unittest.main()
