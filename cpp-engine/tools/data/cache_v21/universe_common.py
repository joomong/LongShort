"""cache_v21/universe_common.py - shared paths, constants and small helpers for the T5 universe modules.

T5 = `derived/marketcap.parquet` + `universe/membership{,_v1rule}.parquet` (BRIEF §3-5) built on the
raw-basis cache_v2.1 stage (T2 prices, T1 master).  Rules re-used verbatim from the prototype
`build_cache_v2.py` (imported, never modified): universe parameters (v1 METADATA), TOKENS_TO_REMOVE,
fund screen constants, the rolling/rank/sticky helpers and the market-cap formula.

Only `universe_*.py` modules are written by T5; `db.py` is imported for the read-only DB access.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
           "POLARS_MAX_THREADS"):
    os.environ.setdefault(_v, "8")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

HERE = Path(__file__).resolve().parent
TOOLS_DATA = HERE.parent
for _p in (str(HERE), str(TOOLS_DATA)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRATCH = Path("/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad")
STAGE = SCRATCH / "stage_v21"
T1_MASTER = STAGE / "T1" / "master"
T1_DONE_V2 = STAGE / "T1" / "DONE_v2.txt"
T2_ROOT = STAGE / "T2"
T5_ROOT = STAGE / "T5"
T5_WORK = STAGE / "T5_work"
REPORTS = SCRATCH / "orch_v21" / "reports"

PROTO_ROOT = Path("/home/jgo/61/data2/temp_stock/cache_v2_cash_1d")
V1_SNAPSHOT = Path("/home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14")

START = "2000-01-03"
NS_PER_DAY = 86_400_000_000_000
DAY_SHIFT = 20                     # key = id_like << 20 | day-since-epoch (days < 2^20)
USD = 160
FX_SNAP = 6
UNIVERSE_NAMES = ["ALL", "Top1000", "Top700", "Top500", "Top300", "Top200", "Top100", "Top10"]

log = logging.getLogger("cache_v21.universe")


def setup_logging(logfile: Path | None = None, level=logging.INFO) -> None:
    root = logging.getLogger("cache_v21")
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile))
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def rss_gb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024


def to_ns_i8(x) -> np.ndarray:
    """int64 ns since epoch from any datetime-like array (arrow timestamp[s] etc.)."""
    return np.asarray(pd.to_datetime(np.asarray(x))).astype("datetime64[ns]").astype(np.int64)


def days_from_ns(ns: np.ndarray) -> np.ndarray:
    return np.floor_divide(np.asarray(ns, dtype=np.int64), NS_PER_DAY)


def make_key(id_like: np.ndarray, days: np.ndarray) -> np.ndarray:
    """Composite int64 key (id << 20 | day).  ids < 2^43, days < 2^20 (year 4840)."""
    id_like = np.asarray(id_like, dtype=np.int64)
    days = np.asarray(days, dtype=np.int64)
    if (days < 0).any() or (days >= (1 << DAY_SHIFT)).any():
        raise ValueError("day index out of key range")
    return (id_like << DAY_SHIFT) | days


def lookup_sorted(sorted_keys: np.ndarray, keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Positions of `keys` in `sorted_keys` (unique, ascending); returns (pos, found_mask)."""
    keys = np.asarray(keys, dtype=np.int64)
    if sorted_keys.size == 0:
        return np.zeros(keys.shape, dtype=np.int64), np.zeros(keys.shape, dtype=bool)
    pos = np.searchsorted(sorted_keys, keys)
    posc = np.minimum(pos, sorted_keys.size - 1)
    found = sorted_keys[posc] == keys
    return posc, found


def write_parquet(table: pa.Table | pd.DataFrame, path: Path, row_group_size: int = 2_000_000) -> dict:
    """Atomic zstd parquet write; returns {"rows", "bytes"}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(table, pd.DataFrame):
        table = pa.Table.from_pandas(table, preserve_index=False)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd", row_group_size=row_group_size)
    os.replace(tmp, path)
    return {"rows": table.num_rows, "bytes": path.stat().st_size}


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False))


def md_table(df: pd.DataFrame, floatfmt: str = "{:.4g}") -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        cells = []
        for v in row:
            if isinstance(v, float):
                cells.append("" if np.isnan(v) else floatfmt.format(v))
            elif isinstance(v, (pd.Timestamp, np.datetime64)):
                cells.append("" if pd.isna(v) else pd.Timestamp(v).strftime("%Y-%m-%d"))
            else:
                cells.append("" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def fmt_int(n) -> str:
    return f"{int(n):,}"


class Timer:
    def __init__(self, label: str):
        self.label = label
        self.t0 = time.time()

    def done(self, extra: str = "") -> float:
        dt = time.time() - self.t0
        log.info("%s: %.1fs%s (rss %.1f GB)", self.label, dt, (" " + extra) if extra else "", rss_gb())
        return dt


def t1_v2_ready() -> bool:
    return T1_DONE_V2.exists()


def load_instruments(master_root: Path = T1_MASTER) -> pd.DataFrame:
    return pq.read_table(master_root / "instruments.parquet").to_pandas()


def load_current(master_root: Path = T1_MASTER) -> pd.DataFrame:
    return pq.read_table(master_root / "current.parquet").to_pandas()


def load_lifecycle(master_root: Path = T1_MASTER) -> pd.DataFrame:
    return pq.read_table(master_root / "lifecycle.parquet").to_pandas()


def load_exclusions(master_root: Path = T1_MASTER) -> pd.DataFrame | None:
    p = master_root / "tradable_exclusions.parquet"
    if not p.exists():
        return None
    return pq.read_table(p).to_pandas()
