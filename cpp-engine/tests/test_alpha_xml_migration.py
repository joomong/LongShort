"""Exercise the migration CLI against disposable saved-alpha directories."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

try:
    import yaml  # noqa: F401
except ImportError:
    sys.exit(77)

TOOL = Path(__file__).resolve().parents[1] / "tools/migrate_alpha_xml.py"


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = self.root / "alphas/one"
        self.saved.mkdir(parents=True)
        self.xml = b'<!-- keep --><data expr="close / adj_factor" delay="1"/><rank/>'
        self.metadata = (
            "level: xml\nsimStart: 2000-01-03\nsimEnd: '2026-10-15'\n"
            "isStart: 2010-01-01\nosStart: '2024-01-01'\nuniverse: TOP500_CAP\n"
            "executionPrice: ohlc4\nauthor: jgo\ndescription: A & B\n"
        ).encode()
        (self.saved / "alpha.xml").write_bytes(self.xml)
        (self.saved / "metadata.yml").write_bytes(self.metadata)

    def run_tool(self, *options):
        return subprocess.run([sys.executable, str(TOOL), str(self.saved.parent), *options],
                              capture_output=True, text=True)

    def test_dry_run_leaves_originals_and_apply_backs_up_before_removing_yaml(self):
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual((self.saved / "alpha.xml").read_bytes(), self.xml)
        self.assertTrue((self.saved / "metadata.yml").exists())
        backup = self.root / "backups"
        result = self.run_tool("--apply", "--backup-dir", str(backup))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.saved / "metadata.yml").exists())
        self.assertEqual(next(backup.rglob("alpha.xml")).read_bytes(), self.xml)
        self.assertEqual(next(backup.rglob("metadata.yml")).read_bytes(), self.metadata)
        doc = ET.parse(self.saved / "alpha.xml").getroot()
        self.assertEqual(doc.tag, "alpha")
        self.assertFalse(doc.attrib)
        self.assertEqual(doc.find("pipeline/data").attrib["delay"], "1")
        self.assertEqual(doc.find("metadata/simulation").attrib["simStart"], "2000-01-03")
        self.assertEqual(doc.findtext("metadata/description"), "A & B")
        self.assertEqual(doc.findtext("metadata/author"), "jgo")
        self.assertIsNone(doc.find("metadata/storage"))
        self.assertEqual(self.run_tool("--apply", "--backup-dir", str(backup)).returncode, 0)

    def test_invalid_second_alpha_prevents_all_changes(self):
        bad = self.saved.parent / "two"
        bad.mkdir()
        (bad / "alpha.xml").write_text("<broken>")
        (bad / "metadata.yml").write_bytes(self.metadata)
        result = self.run_tool("--apply", "--backup-dir", str(self.root / "backups"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.saved / "alpha.xml").read_bytes(), self.xml)
        self.assertTrue((self.saved / "metadata.yml").exists())


if __name__ == "__main__":
    unittest.main()
