"""Every examples/datasets/*.yaml must name universes that exist in its universe parquet.

The engine matches universe.name/names exactly against the `universe` column of the interval
membership file at universe.path (relative paths resolve against the YAML directory). Exits 77
(CTest skip) only when PyYAML/PyArrow or the example's `base.cache` is unavailable.
"""
import sys
import unittest
from pathlib import Path

try:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    import yaml
except ImportError as error:
    print(f"skip: {error}")
    sys.exit(77)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "datasets"
CONFIGS = sorted(EXAMPLES.glob("*.yaml"))


def universe_parquet(config_path):
    config = yaml.safe_load(config_path.read_text())
    universe = config.get("universe")
    if not isinstance(universe, dict) or not universe.get("path"):
        return config, None
    path = Path(universe["path"])
    return config, path if path.is_absolute() else config_path.parent / path


def missing_caches():
    """Caches that are not accessible from this machine (a skip, not a failure).

    Only `base.cache` decides the skip; it resolves against the YAML directory like the
    engine does. A missing universe parquet under an accessible cache is a broken example
    and fails in the test below."""
    missing = []
    for config_path in CONFIGS:
        config, _ = universe_parquet(config_path)
        raw = (config.get("base") or {}).get("cache", "")
        if not raw:
            continue
        cache = Path(raw)
        if not cache.is_absolute():
            cache = config_path.parent / cache
        if not cache.exists():
            missing.append(str(cache))
    return missing


class ExampleConfigs(unittest.TestCase):
    def test_examples_exist(self):
        self.assertTrue(CONFIGS, f"no example configs under {EXAMPLES}")

    def test_universe_names_exist_in_parquet(self):
        for config_path in CONFIGS:
            with self.subTest(config=config_path.name):
                config, parquet = universe_parquet(config_path)
                if parquet is None:
                    continue
                universe = config["universe"]
                wanted = list(universe.get("names") or [])
                if universe.get("name"):
                    wanted.append(universe["name"])
                self.assertTrue(wanted, "interval universe.path needs universe.name or names")
                self.assertTrue(parquet.exists(), f"universe parquet does not exist: {parquet}")
                table = pq.read_table(parquet, columns=["universe"])
                present = set(pc.unique(table.column("universe")).to_pylist())
                self.assertEqual(sorted(set(wanted) - present), [],
                                 f"names absent from {parquet}; present: {sorted(n for n in present if n)}")


if __name__ == "__main__":
    missing = missing_caches()
    if missing:
        print("skip: cache not accessible: " + ", ".join(missing))
        sys.exit(77)
    unittest.main()
