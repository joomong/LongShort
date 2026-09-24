#!/usr/bin/env python3
"""Build the settlement events parquet the C++ engine reads through its `events:` block.

Sources (all read-only):
  * local master parquet   - the trading items the cache covers and their securityEndDate
  * xfl_target.ciqsecurity - authoritative securityEndDate (cross-check / fallback)
  * xfl_target.co_acthist  - corporate action code plus the free-text deal terms
  * xfl_target.ciqgvkeyiid - (gvkey, iid) <-> tradingItemId mapping (objectid is the trading item)
  * xfl_target.ciqpriceequity - acquirer/target closes when the local cache has none
  * local close cache      - the prices the engine actually marks positions against

Output: `events.parquet` (one row per trading item with an end date) and a Korean report.

Contract notes that the engine loader (`cpp-engine/src/data/parquet_reader.cpp:358`) enforces:
  * the id column must be int64 or string and must not repeat,
  * the end date column must be a timestamp/date/`YYYY-MM-DD` string; null means "no event",
  * the return column, when configured, overrides `default_delisting_return` when finite.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# --------------------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------------------
VENDOR_LOADER = "/home/base/git/sim/vendor/uss/save_uss_master_market_data.py"
DEFAULT_MASTER = "/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet"
DEFAULT_CLOSE = "/home/base/cache/cache_v1_cash_1d/BaseData/close.parquet"
DEFAULT_UNIVERSE_DIR = "/home/base/cache/cache_v1_cash_1d/TopUniverseV1"
SCHEMA_NAME = "xfl_target"
# Exchanges the local cache collects (save_uss_master_market_data.py:101).
US_EXCHANGES = (102, 104, 106, 173, 458)
USD_CURRENCY = 160
BANKRUPTCY_CODES = ("BK11", "BK7", "BKR", "LIQ")
# Guards against aggregate deal values leaking into per-share terms.
MAX_RATIO = 100.0
MAX_CASH = 10000.0
# Action codes whose terms describe a share exchange we can follow into a successor.
ACQUISITION_CODES = ("ACQ", "MGD", "LBO", "PVT")
# Only these describe what a holder receives; SPN/OTH/SEC/REA text is not a settlement.
TERM_CODES = ACQUISITION_CODES + ("N/C",)

EVENT_COLUMNS = [
    "tradingItemId", "endDate", "settlementDate", "actionCode", "settlementReturn",
    "suggestedReturn", "cashPerShare", "successorTradingItemId", "successorRatio",
    "acquirerName", "source", "confidence", "actionText", "matchMethod",
    "endDateSource", "endDateConflict", "dbEndDateReason", "endDateOriginal", "endDateGapDays",
    "reviewFlag", "endDateBasis", "acquirerPriceDate",
    "hasCashLeg", "hasStockLeg", "cacheUnitStatus", "overrideSource",
    "cashPerShareCache", "successorRatioCache", "cacheUnitFactorTarget", "cacheUnitFactorSuccessor",
    "targetCacheMark", "targetCacheMarkDate", "cacheUnitFactorTargetDate",
    "successorCacheClose", "successorCacheCloseDate",
]


# --------------------------------------------------------------------------------------
# Read-only xfeed access
# --------------------------------------------------------------------------------------
def db_constants(path: str = VENDOR_LOADER) -> dict:
    """Read the vendor loader's DB_* constants without importing its dependencies."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    out = {}
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"):
        found = re.search(rf'^{key}\s*=\s*["\']([^"\']*)["\']', text, re.M)
        if not found:
            raise RuntimeError(f"{key} is missing from {path}")
        out[key] = found.group(1)
    return out


def connect(statement_timeout_ms: int):
    """Open a strictly read-only connection; credentials are never logged."""
    import psycopg2

    c = db_constants()
    conn = psycopg2.connect(
        host=c["DB_HOST"], database=c["DB_NAME"], user=c["DB_USER"],
        password=c["DB_PASSWORD"], port=c["DB_PORT"],
        options=("-c default_transaction_read_only=on "
                 f"-c statement_timeout={int(statement_timeout_ms)}"),
    )
    conn.set_session(readonly=True)
    return conn


def chunked(values, size=5000):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


def query(conn, sql, params=None, columns=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        names = columns or [d[0] for d in cur.description]
    return pd.DataFrame(rows, columns=names)


# --------------------------------------------------------------------------------------
# Local close cache
# --------------------------------------------------------------------------------------
class CloseCache:
    """Wide close panel keyed by tradingItemId string, the engine's own mark source.

    The file is one row group with ~28k columns; a column-subset read costs ~0.25 s per
    column while the whole table decodes in about a second, so it is read in full once.
    """

    def __init__(self, path: str):
        table = pq.read_table(path, memory_map=True)
        index_name = next((n for n in table.schema.names if n.startswith("__index_level")), None)
        if index_name is None:
            raise RuntimeError(f"No timestamp index column in {path}")
        self.dates = pd.to_datetime(table.column(index_name).to_numpy()).values
        self._table = table
        self._columns = {n: i for i, n in enumerate(table.schema.names) if n != index_name}
        self._cache: dict[str, np.ndarray] = {}

    def next_trading_day(self, when):
        """First cache bar strictly after `when`."""
        if when is pd.NaT:
            return pd.NaT
        at = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(when).normalize()), "right"))
        return pd.Timestamp(self.dates[at]) if at < len(self.dates) else pd.NaT

    def settlement_bar(self, tid: str, end) -> tuple[object, bool]:
        """The bar the engine settles on: the first bar after `end` with no valid close.

        The engine defers a settlement while the security still quotes (`simulator.cpp`
        `event_now = event_due && !observed`), so the bar is not simply "end date + 1" for
        the securities whose quotes run past their administrative end date. Returns
        (bar, deferred) and falls back to the next bar when the security never goes quiet.
        """
        if end is pd.NaT:
            return (pd.NaT, False)
        nxt = self.next_trading_day(end)
        values = self.series(tid)
        if values is None or nxt is pd.NaT:
            return (nxt, False)
        start = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(nxt)), "left"))
        quiet = np.flatnonzero(~np.isfinite(values[start:]))
        if quiet.size == 0:
            return (nxt, False)  # still quoting at the end of the panel
        at = start + int(quiet[0])
        return (pd.Timestamp(self.dates[at]), at > start)

    def value_on(self, tid: str, when) -> float:
        """Cache close on exactly `when`, or NaN."""
        values = self.series(tid)
        if values is None or when is pd.NaT:
            return np.nan
        stamp = np.datetime64(pd.Timestamp(when).normalize())
        at = int(np.searchsorted(self.dates, stamp, "left"))
        if at >= len(values) or self.dates[at] != stamp:
            return np.nan
        return float(values[at])

    def last_valid_before(self, tid: str, when) -> tuple[float, object]:
        """Last valid close strictly before `when`; never looks forward."""
        values = self.series(tid)
        if values is None or when is pd.NaT:
            return (np.nan, pd.NaT)
        stop = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(when).normalize()), "left"))
        if stop <= 0:
            return (np.nan, pd.NaT)
        valid = np.flatnonzero(np.isfinite(values[:stop]))
        if valid.size == 0:
            return (np.nan, pd.NaT)
        return (float(values[valid[-1]]), pd.Timestamp(self.dates[valid[-1]]))

    def at_or_last_before(self, tid: str, when, max_bars: int = 5) -> tuple[float, object]:
        """Close on `when`, else the newest close within `max_bars` bars before it.

        This is the only quote rule the acquirer leg may use: reaching forward for the next
        available price would put post-settlement information into the settlement return.
        The backward reach is bounded because an acquirer whose own quotes stopped years
        earlier (a mis-resolved dead trading item) must not price today's settlement.
        """
        values = self.series(tid)
        if values is None or when is pd.NaT:
            return (np.nan, pd.NaT)
        at = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(when).normalize()), "right"))
        low = max(0, at - (max_bars + 1))
        window = values[low:at]
        valid = np.flatnonzero(np.isfinite(window))
        if valid.size == 0:
            return (np.nan, pd.NaT)
        chosen = low + int(valid[-1])
        return (float(values[chosen]), pd.Timestamp(self.dates[chosen]))

    def has(self, tid: str) -> bool:
        return tid in self._columns

    def series(self, tid: str):
        if tid not in self._columns:
            return None
        values = self._cache.get(tid)
        if values is None:
            values = np.asarray(self._table.column(tid).to_numpy(zero_copy_only=False), dtype=np.float64)
            values = np.where(np.isfinite(values) & (values > 0), values, np.nan)
            self._cache[tid] = values
        return values

    def last_on_or_before(self, tid: str, when) -> tuple[float, object]:
        """Last valid close at or before `when` - the engine's mark at the settlement bar."""
        values = self.series(tid)
        if values is None or when is pd.NaT:
            return (np.nan, pd.NaT)
        stop = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(when).normalize()), "right"))
        if stop <= 0:
            return (np.nan, pd.NaT)
        valid = np.flatnonzero(np.isfinite(values[:stop]))
        if valid.size == 0:
            return (np.nan, pd.NaT)
        return (float(values[valid[-1]]), pd.Timestamp(self.dates[valid[-1]]))

    def first_on_or_after(self, tid: str, when, max_days: int = 10) -> tuple[float, object]:
        """First valid close at or after `when`, i.e. the acquirer quote a holder receives."""
        values = self.series(tid)
        if values is None or when is pd.NaT:
            return (np.nan, pd.NaT)
        start = int(np.searchsorted(self.dates, np.datetime64(pd.Timestamp(when).normalize()), "left"))
        limit = np.datetime64(pd.Timestamp(when).normalize() + pd.Timedelta(days=max_days))
        stop = int(np.searchsorted(self.dates, limit, "right"))
        if start >= len(values):
            return (np.nan, pd.NaT)
        window = values[start:stop]
        valid = np.flatnonzero(np.isfinite(window))
        if valid.size == 0:
            return (np.nan, pd.NaT)
        at = start + int(valid[0])
        return (float(values[at]), pd.Timestamp(self.dates[at]))


# --------------------------------------------------------------------------------------
# Deal-term parsing
# --------------------------------------------------------------------------------------
NUM = r"(\d[\d,]*(?:\.\d+)?|\.\d+)"
_WS = re.compile(r"\s+")
# A '$' introduced by a non-USD currency marker must not be read as a USD amount.
_FOREIGN_WORD = re.compile(
    r"(?:^|[^A-Z])(?:CAD|CAN|CDN|AUD|AUS|HKD|NZD|SGD|SGP|TWD|JPY|GBP|EUR|BRL|ILS|MXN|CNY|RMB|"
    r"CHF|SEK|NOK|DKK|ZAR|INR|KRW|HK|NZ|SG|NT)\s*$")
# A single-letter sigil only counts when it is glued to the sign (C$, A$, R$); "RECEIVED A $9.89"
# is a USD amount.
_FOREIGN_SIGIL = re.compile(r"(?:^|[^A-Z])(?:C|A|R|S|HK|NZ|SG|NT|MX)$")
_SCALED = re.compile(r"^\s*(?:MILLION|BILLION|MM|BN)\b")
# A per-share amount quoted in another currency cannot be added to a USD mark.
_FOREIGN_AMOUNT = re.compile(
    r"(?:^|[^A-Z])(?:C|A|R|S|HK|NZ|SG|NT|MX)\$\s*\.?\d|"
    r"(?:^|[^A-Z])(?:CAD|CAN|CDN|AUD|AUS|HKD|NZD|SGD|TWD|JPY|GBP|EUR|BRL|ILS|MXN|CNY|RMB|"
    r"CHF|SEK|NOK|DKK|ZAR|INR|KRW)\s*\$?\s*\.?\d|"
    r"[\u20ac\u00a3\u00a5]\s*\.?\d")
# "... RESULTING IN A CASH EQUIVALENT OF $x" restates the stock leg's value; it is not a
# second leg of the consideration and must never be added to it.
_EQUIVALENT_VALUE = re.compile(
    r"(?:CASH\s+)?EQUIV(?:ALENT)?S?\s*(?:OF|TO|:|=)?\s*\$\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)|"
    r"VALUED\s+AT\s*\$\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)|"
    r"WORTH\s*\$\s*(?:\d[\d,]*(?:\.\d+)?|\.\d+)")
_UNIT = r"(?:SHARES?|UNITS?|GDRS?|ADSS?|ADRS?)"
_QUAL = r"(?:OF\s+A\s+|OF\s+AN\s+|OF\s+)?"
_MOD = r"(?:NEW\s+|COMMON\s+|STOCK\s+|ORD\s+|ORDINARY\s+|CLASS\s+[A-Z]\s+|POST[- ]SPLIT\s+){0,3}"
WORD_NUMBERS = {"ONE": 1.0, "TWO": 2.0, "THREE": 3.0, "FOUR": 4.0, "FIVE": 5.0, "SIX": 6.0,
                "SEVEN": 7.0, "EIGHT": 8.0, "NINE": 9.0, "TEN": 10.0}
_WORD_ALT = "|".join(WORD_NUMBERS)
_NAME = r"([A-Z0-9&'./,\-() ]{2,60}?)"
_RECEIVED = re.compile(r"\b(?:RECEIVED|RECEIVE|RECEIVES|RECEIVING)\b")
# "0.6531 SHARES OF FREEPORT", "3.7222 OF A SHARE OF NOBLE", "0.483 OF SHARES OF ARES"
_STOCK_LEG = re.compile(r"(?:" + NUM + r"|\b(" + _WORD_ALT + r")\b)" + r"\s*" + _QUAL + _MOD + _UNIT + r"\b(?:\s+OF\s+" + _NAME +
                        r"(?=\s+FOR\b|\s+IN\s+EXCHANGE\b|,|\.|$))?")
# "0.45464 OF BAM FOR EVERY SHARE HELD" - the stock leg written with a bare ticker.
_STOCK_TICKER = re.compile(NUM + r"\s+OF\s+([A-Z]{1,6})\s+(?:ORD\s+" + _UNIT + r"\s+)?FOR\s+(?:EACH|EVERY)\b")
# "0.4778 OF AN ENSCO PLC ADS FOR EACH SHARE HELD" - the unit sits behind the company name.
_STOCK_NAME_UNIT = re.compile(NUM + r"\s+OF\s+AN?\s+" + _NAME + r"\s+(?:ADS|ADR|GDR|SHARE|UNIT)S?\b")
# "... FOR EVERY THREE SHARES HELD" - a denominator other than one share.
_PER_DENOM = re.compile(r"FOR\s+(?:EACH|EVERY)\s+(" + _WORD_ALT + r"|\d+)\s+(?:SHARES|UNITS)\b")
# "FOR 0.475 PLD SHARES FOR EVERY DRE SHARE HELD" - ticker in front of the unit, no "OF".
_STOCK_TICKER_UNIT = re.compile(NUM + r"\s+([A-Z]{1,6})\s+" + _UNIT + r"\s+FOR\s+(?:EACH|EVERY)\b")
_CASH_FOR = re.compile(r"FOR\s*\$\s*" + NUM)
# "... FOR $60.25 PER SHARE." is the headline price even when a later clause quotes a
# dividend wrap-up or a cash-equivalent figure.
_CASH_HEADLINE = re.compile(r"FOR\s*\$\s*" + NUM +
                            r"\s*(?:PER\s+(?:EACH\s+)?SHARE|A\s+SHARE|IN\s+CASH|,|\.|$)")
_CASH_IN_CASH = re.compile(r"\$\s*" + NUM + r"\s+IN\s+CASH")
# "RECEIVED 0.3132 COMMON SHARE AND 17.5 CASH OF ..." - the sign is simply missing.
_CASH_BARE = re.compile(r"(?:AND|&|PLUS)\s+" + NUM + r"\s+(?:IN\s+)?CASH\b")
_NO_TERMS = re.compile(r"NO\s+PER[\s-]SHARE\s+TERMS")
# A stock leg the vendor names without giving an exchange ratio ("RECEIVED STOCK SHARES OF X").
_STOCK_DECLARED = re.compile(
    r"(?:RECEIVED|RECEIVE|RECEIVES|RECEIVING|RECEVED)\s+(?:CASH\s+AND\s+)?"
    r"(?:STOCK|COMMON|ORDINARY|NEW)\s+(?:SHARES?|UNITS?)\b|"
    r"IN\s+A\s+(?:CASH[\s-]AND[\s-])?STOCK\s+MERGER|"
    r"SHARE\s+EXCHANGE|SHARE\s+FOR\s+SHARE")
# "1-FOR-2.649 REVERSE STOCK SPLIT", "0.4464-FOR-1 REVERSE STOCK SPLIT": new per old = A/B.
_SPLIT = re.compile(r"(\d[\d.,]*)\s*-?\s*FOR\s*-?\s*(\d[\d.,]*)\s+(?:REVERSE\s+)?(?:STOCK\s+)?SPLIT")
# Elections (cash *or* stock, or a prorated menu) have no single per-share consideration.
_ELECTION = re.compile(
    r"\bEITHER\b|\bELECT(?:ED|ION|S|ING)?\s+TO\s+RECEIVE\b|CASH[\s-]OR[\s-]STOCK|"
    r"OR\s+THE\s+EQUIVALENT\b|\bOR\s+\$\s*[\d.,]+|"
    r"\$\s*[\d.,]+\s*(?:IN\s+)?CASH\s+OR\b|"
    r"\$\s*[\d.,]+\s*(?:PER\s+SHARE\s+)?(?:IN\s+CASH\s+)?OR\s+\.?\d[\d.,]*\s*(?:" + _QUAL + _MOD + _UNIT + r")")


def normalise(text: str) -> str:
    return _WS.sub(" ", text.upper().replace("{", " ").replace("}", " ")).strip()


def _value(raw: str) -> float:
    return float(raw.replace(",", ""))


def usd_amounts(text: str):
    """USD per-share amounts plus the count of valuation-only figures that were skipped.

    Skips foreign-currency amounts, aggregate (million/billion) figures and
    "cash equivalent of $x" style restatements of a stock leg.
    """
    spans = [found.span() for found in _EQUIVALENT_VALUE.finditer(text)]
    out, excluded = [], 0
    for found in re.finditer(r"\$\s*" + NUM, text):
        prefix = text[max(0, found.start() - 12):found.start()]
        if _FOREIGN_WORD.search(prefix) or _FOREIGN_SIGIL.search(prefix):
            continue
        if _SCALED.match(text[found.end():found.end() + 12]):
            continue
        if any(low <= found.start() < high for low, high in spans):
            excluded += 1
            continue
        out.append((found.start(), _value(found.group(1))))
    return out, excluded


def parse_split_ratio(text) -> float:
    """New shares received per old share from a split stated in a name-change text."""
    if not isinstance(text, str) or not text.strip():
        return np.nan
    found = _SPLIT.search(normalise(text))
    if not found:
        return np.nan
    new, old = _value(found.group(1)), _value(found.group(2))
    if not (old > 0 and 0 < new / old <= MAX_RATIO):
        return np.nan
    return new / old


def _dollar_prefixed(match, text: str) -> bool:
    """True when the number that matched is a USD amount, not an exchange ratio.

    Vendor typos like "FOR $11.10 SHARES FOR EACH SHARE HELD" would otherwise read the cash
    amount as a share count and invent a stock leg on a cash-only deal.
    """
    return text[:match.start()].rstrip().endswith("$")


def parse_terms(text) -> dict:
    """Extract per-share cash, exchange ratio and the acquirer name from an actiontxt."""
    result = {"cash": np.nan, "ratio": np.nan, "acquirerText": None, "kind": "no_text",
              "note": "", "stockDeclared": False}
    if not isinstance(text, str) or not text.strip():
        return result
    body = normalise(text)
    result["stockDeclared"] = bool(_STOCK_DECLARED.search(body))
    if _NO_TERMS.search(body):
        result["kind"] = "no_terms"
        return result
    if _ELECTION.search(body):
        # Proration makes the realised consideration unknowable from this text, but a share
        # alternative is still a declared stock leg.
        result["kind"] = "election"
        result["stockDeclared"] = True
        return result
    # The consideration is stated after the last "received"; texts without one are cash phrasings.
    anchors = list(_RECEIVED.finditer(body))
    clause = body[anchors[-1].end():] if anchors else body
    ratio, acquirer = np.nan, None
    # The denominator phrase ("FOR EVERY THREE SHARES HELD") is not a second stock leg.
    denominator_spans = [found.span() for found in _PER_DENOM.finditer(clause)]
    legs = [leg for leg in _STOCK_LEG.finditer(clause)
            if not _dollar_prefixed(leg, clause)
            and not any(low <= leg.start() < high for low, high in denominator_spans)]
    if len(legs) > 1:
        # e.g. Virgin Media: 0.2582 class A *and* 0.1928 class C - one successor cannot hold both.
        result["kind"] = "multi_class"
        result["stockDeclared"] = True
        return result
    shares = legs[0] if legs else None
    if shares:
        ratio = WORD_NUMBERS[shares.group(2)] if shares.group(2) else _value(shares.group(1))
        acquirer = (shares.group(3) or "").strip(" ,.") or None
    else:
        ticker = next((m for m in _STOCK_TICKER.finditer(clause) if not _dollar_prefixed(m, clause)),
                      None) or \
            next((m for m in _STOCK_TICKER_UNIT.finditer(clause) if not _dollar_prefixed(m, clause)),
                 None) or \
            next((m for m in _STOCK_NAME_UNIT.finditer(clause) if not _dollar_prefixed(m, clause)),
                 None)
        if ticker:
            ratio = _value(ticker.group(1))
            acquirer = ticker.group(2)
    if np.isfinite(ratio):
        # "ONE SHARE OF X FOR EVERY THREE SHARES HELD" pays 1/3 of a share per share held.
        denominator = _PER_DENOM.search(clause)
        if denominator:
            token = denominator.group(1)
            divisor = WORD_NUMBERS.get(token, None) or float(token)
            if divisor > 0:
                ratio /= divisor
    if np.isfinite(ratio) and not (0 < ratio <= MAX_RATIO):
        ratio, acquirer = np.nan, None  # a share *count*, not a per-share exchange ratio
    amounts, excluded_clause = usd_amounts(clause)
    body_amounts, excluded_body = usd_amounts(body)
    cash = np.nan
    headline = _CASH_HEADLINE.search(body)
    if headline and any(abs(value - _value(headline.group(1))) < 1e-9 for _, value in body_amounts):
        cash = _value(headline.group(1))
    elif amounts:
        explicit = _CASH_IN_CASH.search(clause) or _CASH_FOR.search(clause)
        cash = _value(explicit.group(1)) if explicit else amounts[0][1]
    else:
        bare = _CASH_BARE.search(clause)
        if bare:
            cash = _value(bare.group(1))
    if np.isfinite(cash) and not (0 < cash <= MAX_CASH):
        cash = np.nan
    if max(excluded_clause, excluded_body):
        result["note"] = "excluded_equivalent"
    if _FOREIGN_AMOUNT.search(body) and not np.isfinite(cash):
        # The consideration is quoted in another currency: the cash leg cannot be divided by a
        # USD mark, and keeping only the stock leg would understate what a holder received.
        result["kind"] = "foreign_currency"
        return result
    result["cash"] = cash
    result["ratio"] = ratio
    result["acquirerText"] = acquirer
    if np.isfinite(ratio):
        result["stockDeclared"] = True
    if np.isfinite(cash) and np.isfinite(ratio):
        result["kind"] = "mixed"
    elif np.isfinite(ratio):
        result["kind"] = "stock"
    elif np.isfinite(cash):
        result["kind"] = "cash"
    else:
        result["kind"] = "unparsed"
    return result


# --------------------------------------------------------------------------------------
# Pipeline steps
# --------------------------------------------------------------------------------------
OVERRIDE_REQUIRED = ("tradingItemId", "endDate", "actionCode", "cashPerShare",
                     "successorTradingItemId", "successorRatio", "source", "confidence",
                     "evidence_url", "note", "entered_on")
OVERRIDE_SOURCES = ("sec_8k", "filingdata", "press_release", "co_acthist_text", "web_news")
OVERRIDE_CONFIDENCE = ("high", "medium", "low")
# The only way an election deal may be entered: the consideration a holder who made *no*
# election actually received, after proration. A blended average or the cash option alone
# is not what any holder got, so no other rule is accepted.
OVERRIDE_ELECTION_RULES = ("non_election_default",)
_ELECTION_WORDS = re.compile(r"\bEITHER\b|\bELECT", re.I)
# A note that explicitly records the *absence* of an election is not an election row.
_ELECTION_NEGATED = re.compile(
    r"NO\s+(?:CASH[^.]{0,24})?ELECTION(?!\s+(?:SHARE|UNIT))|NOT\s+AN?\s+ELECTION|"
    r"NO\s+PRORATION|SAME\s+RATIO", re.I)
# "No Election Share(s)" is the contractual term itself, so it counts as a citation.
_ELECTION_EVIDENCE = re.compile(r"NON[- ]ELECT|NO[- ]ELECTION|PRORAT|DEFAULT", re.I)


def validate_override_frame(frame: pd.DataFrame) -> list:
    """Every override row must carry citable evidence and at least one leg of consideration."""
    errors = []
    optional = ("election_rule", "supersedes", "superseded_on")
    missing = [name for name in OVERRIDE_REQUIRED if name not in frame.columns]
    if missing:
        errors.append(f"override file is missing required column(s): {', '.join(missing)}")
        return errors
    blank = lambda value: value is None or (isinstance(value, float) and not np.isfinite(value)) \
        or str(value).strip() in ("", "nan", "<NA>", "None")
    for position, (index, row) in enumerate(frame.iterrows(), start=2):
        label = f"row {position} (tradingItemId={row.get('tradingItemId', index)})"
        if blank(row["tradingItemId"]) or blank(row["endDate"]):
            errors.append(f"{label}: tradingItemId and endDate are required")
        if blank(row["source"]):
            errors.append(f"{label}: source is required "
                          f"({'|'.join(OVERRIDE_SOURCES)})")
        elif str(row["source"]).strip() not in OVERRIDE_SOURCES:
            errors.append(f"{label}: unknown source {row['source']!r}; "
                          f"use one of {'|'.join(OVERRIDE_SOURCES)}")
        if blank(row["evidence_url"]):
            errors.append(f"{label}: evidence_url is required")
        if blank(row["confidence"]) or str(row["confidence"]).strip() not in OVERRIDE_CONFIDENCE:
            errors.append(f"{label}: confidence must be one of {'|'.join(OVERRIDE_CONFIDENCE)}")
        if blank(row["entered_on"]):
            errors.append(f"{label}: entered_on is required")
        if blank(row["cashPerShare"]) and blank(row["successorRatio"]):
            errors.append(f"{label}: needs cashPerShare or successorRatio (no structure otherwise)")
        rule = "" if "election_rule" not in frame.columns or blank(row.get("election_rule")) \
            else str(row["election_rule"]).strip()
        note = "" if blank(row["note"]) else str(row["note"])
        if rule and rule not in OVERRIDE_ELECTION_RULES:
            errors.append(f"{label}: election_rule must be one of "
                          f"{'|'.join(OVERRIDE_ELECTION_RULES)}, not {rule!r}")
        if _ELECTION_WORDS.search(note) and not _ELECTION_NEGATED.search(note) and not rule:
            errors.append(f"{label}: the note describes an election but election_rule is empty; "
                          f"only 'non_election_default' (the allocation a non-electing holder "
                          f"received after proration) may be entered")
        if rule and not _ELECTION_EVIDENCE.search(note):
            errors.append(f"{label}: election_rule={rule} requires the note to cite the "
                          f"non-election/proration clause")
        has_supersedes = "supersedes" in frame.columns and not blank(row.get("supersedes"))
        has_date = "superseded_on" in frame.columns and not blank(row.get("superseded_on"))
        if has_supersedes != has_date:
            errors.append(f"{label}: supersedes and superseded_on must be filled together")
    return errors


# Rejections that withdraw the evaluation inputs (a term set proved wrong or unusable).
CACHE_REJECTIONS = ("cash_R_out_of_range", "R_out_of_range", "rename_not_1to1", "non_usd_listing")


def decide_end_date_cap(end_date, last_quote, corroborating, tolerance: int = 5) -> tuple:
    """Decide whether an end date may be pulled back to the security's last cache quote.

    A gap between the end date and the last quote can mean the security stopped trading, that
    the vendor stopped collecting it, or that the id changed - price data alone cannot tell
    them apart. xfeed has **no effective-date column** for corporate actions (checked: only
    ciqestimate*/sec_idhist carry eff* fields), so `co_acthist.datadate` is a *record* date and
    a date match is an estimate, never a confirmation. The cap is therefore taken when:
      * the record date sits on the last quote (`record_date_match`, still an estimate), or
      * the gap is small enough to be ordinary settlement lag (`gap_estimate`).
    Anything else keeps its original end date and is flagged for review.
    Returns (end_date, source_suffix, gap_days, review_flag, basis).
    """
    if last_quote is pd.NaT or last_quote is None or end_date is pd.NaT:
        return (end_date, "", 0, "", "original")
    gap = (pd.Timestamp(end_date) - pd.Timestamp(last_quote)).days
    if gap <= 0:
        return (end_date, "", 0, "", "original")
    if corroborating is not None and corroborating is not pd.NaT and \
            abs((pd.Timestamp(corroborating) - pd.Timestamp(last_quote)).days) <= tolerance:
        return (last_quote, "+capped_recorddate_match", gap, "", "record_date_match")
    if gap <= tolerance:
        return (last_quote, "+capped_estimate", gap, "", "gap_estimate")
    return (end_date, "", gap, "enddate_gap", "original")


def resolve_cache_units(*, has_cash: bool, has_stock: bool, cash: float, ratio: float,
                        has_successor: bool, as_traded_target: float, cache_at_kt: float,
                        cache_mark: float, as_traded_successor: float, cache_successor: float,
                        reject_reason: str = "") -> dict:
    """Convert the raw deal terms into the cache units the engine multiplies by.

    All-or-nothing: if any declared leg cannot be converted, both evaluation inputs are NaN and
    `status` says which leg failed. Anchors are only accepted when the DB print and the cache
    close exist on the *same* date, so a missing DB row can never certify k = 1 (review R4).
    """
    nan = float("nan")
    out = {"cashPerShareCache": nan, "successorRatioCache": nan,
           "cacheUnitFactorTarget": nan, "cacheUnitFactorSuccessor": nan, "cacheUnitStatus": ""}
    if reject_reason:
        out["cacheUnitStatus"] = f"rejected:{reject_reason}"
        return out
    if not has_cash and not has_stock:
        out["cacheUnitStatus"] = "no_terms"
        return out
    cache_mark_ok = np.isfinite(cache_mark) and cache_mark > 0
    cache_kt_ok = np.isfinite(cache_at_kt) and cache_at_kt > 0
    cache_successor_ok = np.isfinite(cache_successor) and cache_successor > 0
    # k_t is measured on the *end date* bar, not on the mark bar: when a retired line keeps
    # quoting afterwards those later prints can already be on the successor's basis (TYC 2016),
    # and anchoring there would apply the exchange ratio twice.
    if cache_kt_ok and np.isfinite(as_traded_target):
        out["cacheUnitFactorTarget"] = as_traded_target / cache_at_kt
    if cache_successor_ok and np.isfinite(as_traded_successor):
        out["cacheUnitFactorSuccessor"] = as_traded_successor / cache_successor
    if has_stock:
        if not has_successor:
            out["cacheUnitStatus"] = "no_successor_id"
            return out
        if not np.isfinite(ratio):
            # The successor is known, only the exchange ratio is missing: a backfill needs one
            # number, so this must not be confused with a row that has no terms at all.
            out["cacheUnitStatus"] = "no_ratio"
            return out
        if not cache_successor_ok:
            out["cacheUnitStatus"] = "no_cache_successor_at_settlement"
            return out
        if not np.isfinite(as_traded_successor):
            out["cacheUnitStatus"] = "no_db_successor"
            return out
    if not (cache_mark_ok and cache_kt_ok):
        out["cacheUnitStatus"] = "no_cache_target"
        return out
    if not np.isfinite(as_traded_target):
        out["cacheUnitStatus"] = "no_db_target"
        return out
    if has_cash and not (np.isfinite(cash) and cash > 0):
        out["cacheUnitStatus"] = "no_terms"
        return out
    k_target = out["cacheUnitFactorTarget"]
    if has_cash:
        out["cashPerShareCache"] = cash / k_target
    if has_stock:
        out["successorRatioCache"] = ratio * out["cacheUnitFactorSuccessor"] / k_target
    out["cacheUnitStatus"] = "ok"
    return out


def load_master(path: str) -> pd.DataFrame:
    master = pd.read_parquet(path)
    if master.index.name != "tradingItemId":
        raise RuntimeError(f"Master index must be tradingItemId, found {master.index.name}")
    keep = [c for c in ("symbol", "companyName", "securityEndDate", "securityStartDate",
                        "exchangeId", "ISOCode", "currentPrimary", "primaryFlag") if c in master.columns]
    master = master[keep].copy()
    master.index = pd.Index(master.index.astype("int64"), name="tradingItemId")
    return master


def fetch_db_end_dates(conn, ids) -> pd.DataFrame:
    frames = []
    for chunk in chunked(ids):
        frames.append(query(conn, f"""
            SELECT t.tradingitemid, t.tickersymbol, t.exchangeid, t.currencyid,
                   t.tradingitemstatusid, s.securityenddate, s.securitystartdate
            FROM {SCHEMA_NAME}.ciqtradingitem t
            JOIN {SCHEMA_NAME}.ciqsecurity s ON s.securityid = t.securityid
            WHERE t.tradingitemid = ANY(%s)""", (chunk,)))
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out = out.rename(columns={"tradingitemid": "tradingItemId", "securityenddate": "dbEndDate",
                              "securitystartdate": "dbStartDate", "tickersymbol": "dbTicker"})
    return out.set_index("tradingItemId")


def fetch_gvkey_map(conn, ids=None, gvkeys=None) -> pd.DataFrame:
    frames = []
    if ids is not None:
        for chunk in chunked(ids):
            frames.append(query(conn, f"""
                SELECT objectid, gvkey, iid, activeflag, symbolstartdate, symbolenddate
                FROM {SCHEMA_NAME}.ciqgvkeyiid WHERE objectid = ANY(%s)""", (chunk,)))
    if gvkeys is not None:
        for chunk in chunked(gvkeys):
            frames.append(query(conn, f"""
                SELECT objectid, gvkey, iid, activeflag, symbolstartdate, symbolenddate
                FROM {SCHEMA_NAME}.ciqgvkeyiid WHERE gvkey = ANY(%s)""", (chunk,)))
    if not frames:
        return pd.DataFrame(columns=["objectid", "gvkey", "iid", "activeflag", "symbolstartdate", "symbolenddate"])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def fetch_actions(conn, gvkeys) -> pd.DataFrame:
    frames = []
    for chunk in chunked(gvkeys):
        frames.append(query(conn, f"""
            SELECT gvkey, iid, datadate, actioncd, actiontxt, gvkeyaqc,
                   newconm, newtic1, oldconm, oldtic1
            FROM {SCHEMA_NAME}.co_acthist WHERE gvkey = ANY(%s)""", (chunk,)))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_trading_items(conn, ids) -> pd.DataFrame:
    frames = []
    for chunk in chunked(ids):
        frames.append(query(conn, f"""
            SELECT t.tradingitemid, t.tickersymbol, t.exchangeid, t.currencyid,
                   t.tradingitemstatusid, t.primaryflag AS ti_primary, s.primaryflag AS sec_primary,
                   s.securityenddate, c.companyname
            FROM {SCHEMA_NAME}.ciqtradingitem t
            JOIN {SCHEMA_NAME}.ciqsecurity s ON s.securityid = t.securityid
            LEFT JOIN {SCHEMA_NAME}.ciqcompany c ON c.companyid = s.companyid
            WHERE t.tradingitemid = ANY(%s)""", (chunk,)))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates("tradingitemid").set_index("tradingitemid")


def fetch_last_price_dates(conn, ids) -> dict:
    """tradingItemId -> max(ciqpriceequity.pricingdate), the last day the item actually traded.

    `ciqsecurity.securityenddate` is a *security*-level date: when a security rolls onto a new
    trading item the retired item inherits a date thousands of days later, and 2026-09-11 (the
    last load date) sits on 16,087 securities as a rolling marker. The end date the engine
    needs is the item's own, so a DB date is only trusted when the item was still priced.
    """
    from psycopg2.extras import execute_values

    out = {}
    for chunk in chunked([(int(i),) for i in ids], 5000):
        with conn.cursor() as cur:
            execute_values(cur, f"""
                SELECT v.tid, (SELECT max(p.pricingdate) FROM {SCHEMA_NAME}.ciqpriceequity p
                               WHERE p.tradingitemid = v.tid)
                FROM (VALUES %s) AS v(tid)""", chunk, template="(%s::int)", page_size=5000)
            for tid, when in cur.fetchall():
                if when is not None:
                    out[int(tid)] = pd.Timestamp(when)
    return out


def fetch_as_traded(conn, requests) -> dict:
    """requests: (tradingItemId, low, high) -> {tid: [(date, as-traded close), ...]} sorted by date.

    `ciqpriceequity.priceclose` is restated onto the current share basis whenever a split
    or exchange happens later, and `adjustmentfactor` carries the conversion back to what
    actually traded that day (GOOG 2015-10-05: 32.0735 x 20 = 641.47). The local cache
    instead holds whatever `priceclose` said on the day it was collected, so the two
    disagree exactly on the securities a settlement has to compare. Settlement returns are
    scale-free ratios, so both legs are taken as-traded from the DB; the cache is used only
    to decide *which* bars the engine marks and settles on.
    """
    from psycopg2.extras import execute_values

    out = {}
    if not requests:
        return out
    for chunk in chunked(requests, 2000):
        with conn.cursor() as cur:
            execute_values(cur, f"""
                SELECT v.tid, p.pricingdate, p.priceclose, p.adjustmentfactor
                FROM (VALUES %s) AS v(tid, lo, hi)
                JOIN {SCHEMA_NAME}.ciqpriceequity p
                  ON p.tradingitemid = v.tid AND p.pricingdate >= v.lo AND p.pricingdate <= v.hi
                WHERE p.priceclose IS NOT NULL AND p.priceclose > 0""",
                chunk, template="(%s::int,%s::timestamp,%s::timestamp)", page_size=2000)
            for tid, when, close, factor in cur.fetchall():
                scale = float(factor) if factor is not None and float(factor) > 0 else 1.0
                out.setdefault(int(tid), []).append((pd.Timestamp(when), float(close) * scale))
    for values in out.values():
        values.sort()
    return out


def pick_action(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """One action per trading item: nearest datadate to the end date, deal codes first."""
    priority = frame["actioncd"].map(
        lambda c: 0 if c in ACQUISITION_CODES else (1 if c in BANKRUPTCY_CODES else 2))
    ranked = frame.assign(_priority=priority, _gap=frame["gapDays"].abs())
    ranked = ranked.sort_values([key, "_gap", "_priority", "datadate"],
                                ascending=[True, True, True, False])
    return ranked.drop_duplicates(subset=[key], keep="first").drop(columns=["_priority", "_gap"])


def match_actions(targets: pd.DataFrame, actions: pd.DataFrame, gmap: pd.DataFrame,
                  window: int) -> tuple[pd.DataFrame, dict]:
    """Exact (gvkey, iid) match first, then oldtic1 + end-date proximity."""
    stats = {}
    keys = gmap[["objectid", "gvkey", "iid"]].dropna().drop_duplicates()
    exact = actions.merge(keys, on=["gvkey", "iid"], how="inner")
    exact = exact.merge(targets[["endDate"]], left_on="objectid", right_index=True, how="inner")
    exact["gapDays"] = (exact["datadate"] - exact["endDate"]).dt.days
    stats["exact_rows_any_gap"] = len(exact)
    stats["exact_targets_any_gap"] = int(exact["objectid"].nunique())
    exact = exact[exact["gapDays"].abs() <= window]
    picked = pick_action(exact, "objectid") if len(exact) else exact
    if len(picked):
        picked = picked.assign(matchMethod="gvkeyiid").set_index("objectid")
    stats["exact_matched"] = len(picked)

    done = picked.index if len(picked) else pd.Index([], dtype="int64")
    remaining = targets.index.difference(done)
    # Index.difference drops the name when the two indexes disagree, so restore it.
    symbols = targets.loc[remaining, ["symbol", "endDate"]].rename_axis("tradingItemId").reset_index()
    symbols["symbol_u"] = symbols["symbol"].astype(str).str.upper().str.strip()
    pool = actions[actions["oldtic1"].notna()].copy()
    pool["oldtic1_u"] = pool["oldtic1"].astype(str).str.upper().str.strip()
    fallback = symbols.merge(pool, left_on="symbol_u", right_on="oldtic1_u", how="inner")
    fallback["gapDays"] = (fallback["datadate"] - fallback["endDate"]).dt.days
    fallback = fallback[fallback["gapDays"].abs() <= window]
    picked2 = pick_action(fallback, "tradingItemId") if len(fallback) else fallback
    if len(picked2):
        picked2 = picked2.assign(matchMethod="oldtic1").set_index("tradingItemId")
    stats["oldtic1_matched"] = len(picked2)

    columns = ["gvkey", "iid", "datadate", "actioncd", "actiontxt", "gvkeyaqc",
               "newconm", "newtic1", "oldconm", "oldtic1", "gapDays", "matchMethod"]
    frames = [f[[c for c in columns if c in f.columns]] for f in (picked, picked2) if len(f)]
    matched = pd.concat(frames) if frames else pd.DataFrame(columns=columns)
    matched.index.name = "tradingItemId"
    return matched, stats


def resolve_acquirers(conn, deals: pd.DataFrame, master: pd.DataFrame,
                      gmap_by_gvkey: pd.DataFrame, closes: "CloseCache" = None,
                      settlement_bars: dict = None) -> tuple:
    """gvkeyaqc -> ciqgvkeyiid -> the acquirer line that was actually quoted on the settlement bar.

    A gvkey usually maps to several trading items (share classes, the pre- and post-merger line,
    foreign listings). Requiring a quote on the settlement bar itself resolves most of them
    uniquely - it is the only line a holder could have been credited with that day. When more
    than one line survives, the acquirer is left unresolved (`ambiguous`) rather than guessed,
    because a multi-class consideration cannot be carried by one successor id anyway.
    """
    master_symbols = {}
    for tid, row in master[["symbol", "currentPrimary"]].dropna(subset=["symbol"]).iterrows():
        key = str(row["symbol"]).upper().strip()
        current = master_symbols.get(key)
        if current is None or (bool(row.get("currentPrimary")) and not current[1]):
            master_symbols[key] = (int(tid), bool(row.get("currentPrimary")))

    candidates = gmap_by_gvkey.groupby("gvkey")["objectid"].apply(lambda s: sorted(set(int(x) for x in s)))
    needed = sorted({tid for ids in candidates for tid in ids})
    info = fetch_trading_items(conn, needed) if needed else pd.DataFrame()
    in_master = set(master.index.astype("int64"))

    def score(tid: int) -> tuple:
        row = info.loc[tid] if tid in info.index else None
        exchange_ok = row is not None and row["exchangeid"] in US_EXCHANGES
        usd = row is not None and row["currencyid"] == USD_CURRENCY
        primary = row is not None and row["ti_primary"] == 1 and row["sec_primary"] == 1
        # USD first: the cash leg and the target mark are USD, so a foreign line cannot be mixed in.
        return (usd, tid in in_master, primary, exchange_ok, -tid)

    def is_usd(tid) -> bool:
        return tid in info.index and info.loc[tid]["currencyid"] == USD_CURRENCY

    settlement_bars = settlement_bars or {}
    resolved, outcomes = {}, {}
    for tid, row in deals.iterrows():
        acquirer, outcome = None, "unresolved"
        gvkeyaqc = row.get("gvkeyaqc")
        if isinstance(gvkeyaqc, str) and gvkeyaqc.strip() and gvkeyaqc in candidates.index:
            options = list(candidates.loc[gvkeyaqc])
            bar = settlement_bars.get(tid)

            def priced(option) -> bool:
                return (closes is not None and bar is not None and bar is not pd.NaT and
                        np.isfinite(closes.value_on(str(option), bar)))

            # An exact (gvkey, iid) match is never discarded: the filters only *choose* among
            # candidates, so a successor whose line is missing from the cache keeps its id and
            # fails later at the conversion step instead of losing the link entirely.
            tiers = (lambda o: score(o)[0] and o in in_master and priced(o),
                     lambda o: score(o)[0] and o in in_master,
                     lambda o: score(o)[0],
                     lambda o: True)
            usable = options
            for keep in tiers:
                narrowed = [option for option in options if keep(option)]
                if narrowed:
                    usable = narrowed
                    break
            if len(usable) == 1:
                acquirer, outcome = usable[0], "gvkeyaqc"
            elif len(usable) > 1:
                ticker = row.get("newtic1") or row.get("acquirerTicker")
                named = [option for option in usable
                         if isinstance(ticker, str) and ticker.strip() and
                         str(master["symbol"].get(option, "")).upper().strip() == ticker.upper().strip()]
                if len(named) == 1:
                    acquirer, outcome = named[0], "gvkeyaqc_ticker"
                else:
                    acquirer, outcome = max(usable, key=score), "gvkeyaqc_scored"
        if acquirer is None:
            for ticker in (row.get("newtic1"), row.get("acquirerTicker")):
                if isinstance(ticker, str) and ticker.strip():
                    hit = master_symbols.get(ticker.upper().strip())
                    if hit:
                        acquirer, outcome = hit[0], "ticker"
                        break
        if acquirer is not None and acquirer in info.index and not is_usd(acquirer):
            acquirer, outcome = None, "non_usd_acquirer"  # cannot be added to a USD cash leg
        resolved[tid] = acquirer
        outcomes[tid] = outcome
    return pd.Series(resolved, dtype="object"), pd.Series(outcomes, dtype="object")


def resolve_successor_rename(targets: pd.DataFrame, siblings: pd.DataFrame, matched: pd.DataFrame,
                             closes: "CloseCache") -> pd.Series:
    """A name change that mints a new trading item keeps the same (gvkey, iid).

    `siblings` must cover every trading item of the matched gvkeys, not only the targets,
    because the successor itself normally has no end date and is therefore not a target.
    """
    pool = siblings.dropna(subset=["gvkey", "iid"]).copy()
    pool["objectid"] = pool["objectid"].astype("int64")
    grouped = pool.groupby(["gvkey", "iid"])["objectid"].apply(lambda s: sorted(set(s)))
    out = {}
    for tid, row in matched[matched["actioncd"] == "N/C"].iterrows():
        key = (row["gvkey"], row["iid"])
        if key not in grouped.index:
            continue
        end = pd.Timestamp(targets.at[tid, "endDate"])
        best = None
        for candidate in grouped.loc[key]:
            # The successor keeps quoting, so it must exist in the price cache the engine reads.
            if candidate == tid or not closes.has(str(candidate)):
                continue
            price, when = closes.first_on_or_after(str(candidate), end, max_days=60)
            if not np.isfinite(price) or when < end - pd.Timedelta(days=10):
                continue
            if best is None or when < best[1]:
                best = (candidate, when)
        if best:
            out[tid] = best[0]
    return pd.Series(out, dtype="object")


def read_universe_members(path: str, start: pd.Timestamp) -> set:
    """Trading items with at least one membership day at or after `start`."""
    table = pq.read_table(path, memory_map=True)
    index_name = next((n for n in table.schema.names if n.startswith("__index_level")), None)
    dates = pd.to_datetime(table.column(index_name).to_numpy()).values
    keep = dates >= np.datetime64(start)
    members = set()
    for name in table.schema.names:
        if name == index_name:
            continue
        values = np.asarray(table.column(name).to_numpy(zero_copy_only=False), dtype=np.float64)
        if np.nansum(values[keep]) > 0:
            members.add(name)
    del table
    return members


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", default=DEFAULT_MASTER)
    parser.add_argument("--out", default="data/events/events.parquet")
    parser.add_argument("--report", default=None, help="default: <out dir>/events_report.md")
    parser.add_argument("--override", default="data/events/events_override.csv")
    parser.add_argument("--close-cache", default=DEFAULT_CLOSE)
    parser.add_argument("--universe-dir", default=DEFAULT_UNIVERSE_DIR)
    parser.add_argument("--start", default="2009-01-01", help="emit events whose end date is on or after this")
    parser.add_argument("--universe-start", default="2010-01-01")
    parser.add_argument("--max-cash-return", type=float, default=0.5,
                        help="reject a cash-only deal whose implied |R| exceeds this (vendor/parse error)")
    parser.add_argument("--max-return", type=float, default=0.5,
                        help="reject any settlement return whose |R| exceeds this")
    parser.add_argument("--end-date-tolerance", type=int, default=5,
                        help="days within which an effective date corroborates a cap, and the gap a cap may be estimated over")
    parser.add_argument("--db-end-date-tolerance", type=int, default=10,
                        help="days after the last print a ciqsecurity end date may still be the item's own")
    parser.add_argument("--acquirer-stale-bars", type=int, default=5,
                        help="how many bars before the settlement bar an acquirer quote may come from")
    parser.add_argument("--max-rename-return", type=float, default=0.2,
                        help="reject an assumed 1:1 name-change continuation whose |R| exceeds this")
    parser.add_argument("--window", type=int, default=200, help="|datadate - endDate| day window for matching")
    parser.add_argument("--statement-timeout", type=int, default=300000)
    parser.add_argument("--skip-universe", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="run the parser and cache-unit boundary tests, then exit")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    start = pd.Timestamp(args.start)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report) if args.report else out_path.parent / "events_report.md"
    log = lambda *a: print(*a, file=sys.stderr, flush=True)

    override_path = Path(args.override) if args.override else None
    override_frame = pd.DataFrame()
    override_note = "override 파일 없음 — 건너뜀"
    if override_path and override_path.exists():
        override_frame = pd.read_csv(override_path, dtype={"tradingItemId": "Int64",
                                                           "successorTradingItemId": "Int64"})
        problems = validate_override_frame(override_frame)
        if problems:
            for problem in problems:
                print(f"override error: {problem}", file=sys.stderr)
            print(f"{len(problems)} override row(s) rejected; fix {override_path} and re-run",
                  file=sys.stderr)
            return 1
        override_frame = override_frame.set_index(override_frame["tradingItemId"].astype("int64"))
        override_frame.index.name = "tradingItemId"
        override_note = (f"{len(override_frame)}건 적용 ({override_path}); 근거 종류 "
                         + ", ".join(f"{k} {v}건" for k, v in
                                     override_frame["source"].value_counts().items()))

    log("[1/9] master")
    master = load_master(args.master)
    ids = [int(x) for x in master.index]

    conn = connect(args.statement_timeout)
    try:
        log("[2/9] ciqsecurity end dates")
        db_info = fetch_db_end_dates(conn, ids)
        joined = master.join(db_info, how="left")
        log("[2b/9] last priced date per trading item")
        last_px = fetch_last_price_dates(conn, ids)
        joined["lastPx"] = pd.Series(last_px).reindex(joined.index)
        # A DB end date is the item's own only when the item was still priced around it;
        # otherwise it is a security-level date or the loader's rolling marker and the local
        # master (or the last print) is the better end date.
        db_present = joined["dbEndDate"].notna()
        px_present = joined["lastPx"].notna()
        # Two-sided: a DB date far *before* the last print is a security-level date just as
        # much as one far after it (the item kept trading under the same security).
        trusted = db_present & (~px_present |
                                ((joined["dbEndDate"] - joined["lastPx"]).abs() <=
                                 pd.Timedelta(days=args.db_end_date_tolerance)))
        untrusted = db_present & ~trusted
        chosen = pd.Series(pd.NaT, index=joined.index, dtype="datetime64[ns]")
        source = pd.Series("", index=joined.index, dtype=object)
        # No DB date at all: the local master is the only end date there is.
        local_only = ~db_present & joined["securityEndDate"].notna()
        chosen[local_only] = joined.loc[local_only, "securityEndDate"]
        source[local_only] = "local"
        # A DB date the item was still being priced for is the item's own end date.
        chosen[trusted] = joined.loc[trusted, "dbEndDate"]
        source[trusted] = "db"
        # Otherwise the DB date belongs to the security (or is the loader's rolling marker).
        use_local = untrusted & joined["securityEndDate"].notna()
        chosen[use_local] = joined.loc[use_local, "securityEndDate"]
        source[use_local] = "local"
        use_px = untrusted & joined["securityEndDate"].isna() & px_present
        chosen[use_px] = joined.loc[use_px, "lastPx"]
        source[use_px] = "lastpx"
        rest = untrusted & ~use_local & ~use_px
        chosen[rest] = joined.loc[rest, "dbEndDate"]
        source[rest] = "db"
        joined["endDate"] = chosen
        joined["endDateSource"] = source
        joined["dbEndDateReason"] = np.where(untrusted, "security_level", "")
        joined["endDateConflict"] = (joined["securityEndDate"].notna() & db_present &
                                     (joined["securityEndDate"] != joined["dbEndDate"]))
        db_only = joined["securityEndDate"].isna() & db_present
        master_only = joined["securityEndDate"].notna() & ~db_present
        conflicts = joined[joined["endDateConflict"]]
        for tid, row in override_frame.iterrows():
            if tid not in joined.index:
                log(f"      override id {tid} is absent from the master; skipped")
                continue
            joined.at[tid, "endDate"] = pd.Timestamp(row["endDate"]).normalize()
            joined.at[tid, "endDateSource"] = "override"
            joined.at[tid, "endDateConflict"] = False
            joined.at[tid, "dbEndDateReason"] = ""
        all_targets = joined[joined["endDate"].notna()].copy()
        targets = all_targets[all_targets["endDate"] >= start].copy()
        targets["endDate"] = pd.to_datetime(targets["endDate"]).dt.normalize()
        log(f"      end dates {len(all_targets)}, on/after {args.start}: {len(targets)}; "
            f"source {dict(joined.loc[joined['endDate'].notna(), 'endDateSource'].value_counts())}")
        conflict_path = out_path.parent / "enddate_conflicts.csv"
        conflict_rows = conflicts[["symbol", "securityEndDate", "dbEndDate", "lastPx",
                                   "endDateSource", "dbEndDateReason"]].copy()
        conflict_rows.columns = ["ticker", "localEndDate", "dbEndDate", "lastpx", "adopted", "reason"]
        conflict_rows["dbMinusLastpxDays"] = (conflict_rows["dbEndDate"] -
                                              conflict_rows["lastpx"]).dt.days
        conflict_rows = conflict_rows[["ticker", "localEndDate", "dbEndDate", "lastpx",
                                       "dbMinusLastpxDays", "adopted", "reason"]]
        conflict_rows.rename_axis("tradingItemId").to_csv(conflict_path)
        conflict_stats = conflict_rows["adopted"].value_counts().to_dict()
        never_settle = int(((conflict_rows["adopted"] == "db") &
                            (conflict_rows["dbMinusLastpxDays"].fillna(0) > 30)).sum())
        log(f"      wrote {conflict_path}")

        log("[3/9] gvkey/iid map")
        gmap = fetch_gvkey_map(conn, ids=[int(x) for x in targets.index])
        gmap["objectid"] = gmap["objectid"].astype("int64")

        log("[4/9] co_acthist")
        gvkeys = sorted(set(gmap["gvkey"].dropna()))
        actions = fetch_actions(conn, gvkeys)
        actions["datadate"] = pd.to_datetime(actions["datadate"])
        log(f"      {len(actions)} action rows over {len(gvkeys)} gvkeys")

        log("[5/9] matching")
        matched, match_stats = match_actions(targets, actions, gmap, args.window)
        log(f"      matched {len(matched)} / {len(targets)}")

        log("[6/9] parsing deal terms")
        parsed = pd.DataFrame(
            [parse_terms(text) if code in TERM_CODES else
             {"cash": np.nan, "ratio": np.nan, "acquirerText": None, "kind": "not_a_settlement",
              "note": "", "stockDeclared": False}
             for text, code in zip(matched["actiontxt"], matched["actioncd"])],
            index=matched.index) if len(matched) else \
            pd.DataFrame(columns=["cash", "ratio", "acquirerText", "kind", "note", "stockDeclared"])
        deals = matched.join(parsed)

        log("[7/9] acquirer identification")
        closes = CloseCache(args.close_cache)
        # An end date later than the security's last quote only delays the settlement: the mark
        # is frozen while the acquirer keeps moving, so R picks up weeks of unrelated drift
        # (TFCF.A: end date 2019-04-15 vs last print 2019-03-19 gave R = +18%). Cap every end
        # date at the last cache quote on or before it; the original is kept for audit.
        targets["endDateOriginal"] = targets["endDate"]
        effective = pd.to_datetime(deals["datadate"]).reindex(targets.index) if len(deals) else \
            pd.Series(pd.NaT, index=targets.index)
        capped_dates, suffixes, gaps, flags, bases = [], [], [], [], []
        for tid, row in targets.iterrows():
            if row.endDateSource == "override":
                # An override end date comes from a filing and passed the independent audit.
                capped_dates.append(row.endDate); suffixes.append(""); gaps.append(0)
                flags.append(""); bases.append("confirmed")
                continue
            _, last_quote = closes.last_on_or_before(str(tid), row.endDate)
            new_end, suffix, gap, flag, basis = decide_end_date_cap(
                row.endDate, last_quote, effective.get(tid), args.end_date_tolerance)
            capped_dates.append(new_end); suffixes.append(suffix); gaps.append(gap)
            flags.append(flag); bases.append(basis)
        targets["endDate"] = capped_dates
        targets["endDateGapDays"] = gaps
        targets["reviewFlag"] = flags
        targets["endDateBasis"] = bases
        targets["endDateSource"] = targets["endDateSource"] + pd.Series(suffixes, index=targets.index)
        counts = pd.Series(suffixes).value_counts().to_dict()
        log(f"      end date cap: record_date_match {counts.get('+capped_recorddate_match', 0)}, "
            f"estimate {counts.get('+capped_estimate', 0)}, "
            f"flagged {int((targets['reviewFlag'] == 'enddate_gap').sum())} "
            f"(gap median {int(targets.loc[targets['reviewFlag'] == 'enddate_gap', 'endDateGapDays'].median()) if (targets['reviewFlag'] == 'enddate_gap').any() else 0}d)")
        settlement_bars = {tid: closes.settlement_bar(str(tid), row.endDate)[0]
                           for tid, row in targets.iterrows()}
        # A declared stock leg needs an acquirer even when the vendor omitted the exchange ratio
        # ("RECEIVED STOCK SHARES OF <target>"): the id is what makes the ratio fillable later.
        needs_acquirer = deals[deals["actioncd"].isin(ACQUISITION_CODES) &
                               (deals["ratio"].notna() | deals["cash"].notna() |
                                deals["stockDeclared"].fillna(False).astype(bool))]
        acquirer_gvkeys = sorted({g for g in needs_acquirer["gvkeyaqc"].dropna() if str(g).strip()})
        gmap_by_gvkey = fetch_gvkey_map(conn, gvkeys=acquirer_gvkeys) if acquirer_gvkeys else \
            pd.DataFrame(columns=["objectid", "gvkey", "iid", "activeflag", "symbolstartdate", "symbolenddate"])
        share_deals = needs_acquirer[needs_acquirer["ratio"].notna() |
                                     needs_acquirer["stockDeclared"].fillna(False).astype(bool)].copy()
        share_deals["acquirerTicker"] = share_deals["newtic1"]
        if len(share_deals):
            acquirer_ids, acquirer_outcomes = resolve_acquirers(
                conn, share_deals, master, gmap_by_gvkey, closes, settlement_bars)
        else:
            acquirer_ids = acquirer_outcomes = pd.Series(dtype="object")
        log(f"      acquirer lookup {len(share_deals)} rows: "
            f"{dict(acquirer_outcomes.value_counts()) if len(acquirer_outcomes) else {}}")

        log("[8/9] prices")
        rename_rows = deals[deals["actioncd"] == "N/C"]
        siblings = fetch_gvkey_map(conn, gvkeys=sorted(set(rename_rows["gvkey"].dropna()))) \
            if len(rename_rows) else gmap
        successors = resolve_successor_rename(targets, siblings, deals, closes) if len(deals) \
            else pd.Series(dtype="object")

        events = pd.DataFrame(index=targets.index)
        events["symbol"] = targets["symbol"]
        # The master's ISOCode is the *primary* listing's currency, so the trading item's own
        # ciqtradingitem.currencyid decides whether a USD cash term may be divided by its mark.
        events["isUsd"] = (targets["currencyid"] == USD_CURRENCY) if "currencyid" in targets.columns \
            else pd.Series(True, index=targets.index)
        if "currencyid" in targets.columns:
            events.loc[targets["currencyid"].isna(), "isUsd"] = \
                targets.loc[targets["currencyid"].isna(), "ISOCode"].fillna("USD").str.upper() == "USD"
        events["endDate"] = targets["endDate"]
        events["endDateOriginal"] = targets["endDateOriginal"]
        events["endDateGapDays"] = targets["endDateGapDays"]
        events["reviewFlag"] = targets["reviewFlag"]
        events["endDateBasis"] = targets["endDateBasis"]
        events["endDateSource"] = targets["endDateSource"]
        events["endDateConflict"] = targets["endDateConflict"].astype(bool)
        events["dbEndDateReason"] = targets["dbEndDateReason"]
        events["actionCode"] = deals["actioncd"] if len(deals) else None
        events["settlementDate"] = pd.to_datetime(deals["datadate"]).dt.normalize() if len(deals) else pd.NaT
        events["settlementDate"] = events["settlementDate"].fillna(events["endDate"])
        events["cashPerShare"] = deals["cash"] if len(deals) else np.nan
        events["ratio"] = deals["ratio"] if len(deals) else np.nan
        events["matchMethod"] = deals["matchMethod"] if len(deals) else None
        events["matchMethod"] = events["matchMethod"].fillna("none")
        events["actionText"] = (deals["actiontxt"].astype("object").where(deals["actiontxt"].notna())
                                if len(deals) else None)
        events["parseKind"] = deals["kind"] if len(deals) else None
        events["parseNote"] = deals["note"] if len(deals) else None
        events["stockLegDeclared"] = (deals["stockDeclared"].astype(bool) if len(deals)
                                      else False)
        events["acquirerName"] = None
        if len(deals):
            names = deals["newconm"].where(deals["newconm"].notna(), deals["acquirerText"])
            events["acquirerName"] = names
        events["isOverride"] = False
        events["overrideSource"] = ""
        events["acquirerTradingItemId"] = acquirer_ids.reindex(events.index) if len(acquirer_ids) else None
        events["successorTradingItemId"] = events["acquirerTradingItemId"]
        events["successorRatio"] = events["ratio"]
        events["assumedRename"] = False
        if len(successors):
            rename_ids = successors.reindex(events.index)
            take = rename_ids.notna() & events["successorTradingItemId"].isna()
            events.loc[take, "successorTradingItemId"] = rename_ids[take]
            split = pd.Series([parse_split_ratio(t) for t in events["actionText"]], index=events.index)
            events.loc[take, "successorRatio"] = split[take].fillna(1.0)
            # A rename must preserve value, whether the ratio was read from a split note or
            # assumed 1:1; the price basis of a re-listed item is not always comparable, so
            # every inferred continuation is checked against --max-rename-return below.
            events.loc[take, "assumedRename"] = True

        # Settlement bar = the first bar after the end date on which the security has no
        # valid close (the engine defers while quotes continue). The mark is the last valid
        # close strictly before that bar. Both dates come from the cache; the prices are taken
        # as-traded from ciqpriceequity (see fetch_as_traded).
        bars = [closes.settlement_bar(str(tid), row.endDate) for tid, row in events.iterrows()]
        # Overrides replace the parsed terms *before* pricing so that their marks, settlement
        # bars and anchor factors come from exactly the same measurement path (review R4).
        for tid, row in override_frame.iterrows():
            if tid not in events.index:
                continue
            successor = int(row["successorTradingItemId"]) if pd.notna(row.get("successorTradingItemId")) else None
            events.at[tid, "isOverride"] = True
            events.at[tid, "actionCode"] = row.get("actionCode")
            events.at[tid, "settlementDate"] = (pd.Timestamp(row["settlementDate"]).normalize()
                                                if pd.notna(row.get("settlementDate"))
                                                else events.at[tid, "endDate"])
            events.at[tid, "cashPerShare"] = float(row["cashPerShare"]) if pd.notna(row.get("cashPerShare")) else np.nan
            events.at[tid, "ratio"] = float(row["successorRatio"]) if pd.notna(row.get("successorRatio")) else np.nan
            events.at[tid, "successorRatio"] = events.at[tid, "ratio"]
            # A ratio with no id keeps whatever the resolver found; an override with no ratio at
            # all asserts there is no stock leg, so any auto-resolved successor is withdrawn.
            if successor is not None:
                events.at[tid, "successorTradingItemId"] = successor
            elif not pd.notna(row.get("successorRatio")):
                events.at[tid, "successorTradingItemId"] = None
            events.at[tid, "acquirerName"] = row.get("successorTicker") if pd.notna(row.get("successorTicker")) else None
            events.at[tid, "actionText"] = str(row.get("note"))[:200] if pd.notna(row.get("note")) else None
            events.at[tid, "parseKind"] = "override"
            events.at[tid, "overrideSource"] = str(row.get("source") or "")
            events.at[tid, "parseNote"] = ""
            events.at[tid, "matchMethod"] = "override"
            events.at[tid, "assumedRename"] = False
            events.at[tid, "stockLegDeclared"] = successor is not None or pd.notna(row.get("successorRatio"))

        events["conversionDate"] = [b[0] for b in bars]
        events["deferredSettlement"] = [b[1] for b in bars]
        events["conversionDate"] = events["conversionDate"].fillna(events["settlementDate"])
        marks = [closes.last_valid_before(str(tid), row.conversionDate)
                 for tid, row in events.iterrows()]
        events["cacheMark"] = [m[0] for m in marks]
        events["markDate"] = [m[1] for m in marks]

        # k_t is measured on the end-date bar (ktDate), the last bar the security still quoted
        # on its own basis. The guard anchor stays on the mark bar.
        kt = [closes.last_on_or_before(str(tid), row.endDate) for tid, row in events.iterrows()]
        events["cacheAtKt"] = [k[0] for k in kt]
        events["ktDate"] = [k[1] for k in kt]

        target_requests = []
        for tid, row in events.iterrows():
            if pd.notna(row["ktDate"]):
                target_requests.append((int(tid), row["ktDate"].date(), row["ktDate"].date()))
            else:  # no cache quote at all: fall back to the last DB print before the end date
                target_requests.append((int(tid), (row["endDate"] - pd.Timedelta(days=30)).date(),
                                        row["endDate"].date()))
        log(f"      ciqpriceequity: {len(target_requests)} target marks")
        target_prices = fetch_as_traded(conn, target_requests)
        anchor_price = []
        for tid, row in events.iterrows():
            series = target_prices.get(int(tid))
            when, price = (series[-1] if series else (pd.NaT, np.nan))
            # The anchor may only use a DB print on exactly the ktDate bar. Substituting the
            # cache value would silently certify k_t = 1 (review R4).
            anchor_price.append(price if (pd.notna(row["ktDate"]) and when == row["ktDate"])
                                else np.nan)
        events["asTradedTargetAtMark"] = anchor_price
        # The settlement return's denominator is the engine's own mark restated onto the
        # as-traded basis established at ktDate, so the file R and the cache-unit terms agree.
        factor = (pd.to_numeric(events["asTradedTargetAtMark"], errors="coerce") /
                  pd.to_numeric(events["cacheAtKt"], errors="coerce"))
        events["lastClose"] = pd.to_numeric(events["cacheMark"], errors="coerce") * factor
        events["lastCloseDate"] = events["markDate"]

        # The acquirer leg may only use the settlement bar itself or an earlier bar. Reaching
        # forward for the next available quote would price the settlement with information the
        # engine does not have on that bar.
        acquirer_requests = []
        for tid, row in events.iterrows():
            acquirer = row["successorTradingItemId"]
            if acquirer is None or (isinstance(acquirer, float) and not np.isfinite(acquirer)):
                continue
            if pd.isna(row["conversionDate"]):
                continue
            _, when = closes.at_or_last_before(str(int(acquirer)), row["conversionDate"],
                                               args.acquirer_stale_bars)
            if when is pd.NaT:
                if closes.has(str(int(acquirer))):
                    continue  # quotes exist but none near the settlement bar: no usable price
                low = (row["conversionDate"] - pd.Timedelta(days=10)).date()  # no cache column
            else:
                low = when.date()
            acquirer_requests.append((int(acquirer), low, row["conversionDate"].date(), tid))
        log(f"      ciqpriceequity: {len(acquirer_requests)} acquirer quotes")
        acquirer_prices = fetch_as_traded(conn, [(a, lo, hi) for a, lo, hi, _ in acquirer_requests])
        events["acquirerClose"] = np.nan
        events["acquirerCloseDate"] = pd.NaT
        events["successorCacheClose"] = np.nan
        events["asTradedSuccessorAtBar"] = np.nan
        for acquirer, low, high, tid in acquirer_requests:
            # Several events share one acquirer, so the merged series is filtered back to this
            # event's own window and the *latest* quote at or before the settlement bar is used.
            window = [(when, price) for when, price in acquirer_prices.get(acquirer, [])
                      if pd.Timestamp(low) <= when <= pd.Timestamp(high)]
            if window:
                when, price = window[-1]
                events.at[tid, "acquirerClose"] = price
                events.at[tid, "acquirerCloseDate"] = when
            # The cache-unit anchor is pinned to the settlement bar itself: no earlier or later
            # bar may stand in for it.
            bar = events.at[tid, "conversionDate"]
            events.at[tid, "successorCacheClose"] = closes.value_on(str(acquirer), bar)
            exact = [price for when, price in acquirer_prices.get(acquirer, [])
                     if pd.notna(bar) and when == pd.Timestamp(bar)]
            if exact:
                events.at[tid, "asTradedSuccessorAtBar"] = exact[0]
    finally:
        conn.close()

    log("[9/9] settlement returns, override, output")
    cash = pd.to_numeric(events["cashPerShare"], errors="coerce")
    ratio = pd.to_numeric(events["successorRatio"], errors="coerce")
    # Both legs are already as-traded (fetch_as_traded), so the ratio is basis-consistent.
    acquirer_close = pd.to_numeric(events["acquirerClose"], errors="coerce")
    last_close = pd.to_numeric(events["lastClose"], errors="coerce")
    proceeds = cash.fillna(0.0) + ratio.fillna(0.0) * acquirer_close.fillna(0.0)
    stock_ok = ratio.isna() | np.isfinite(acquirer_close)
    have_terms = cash.notna() | ratio.notna()
    # co_acthist cash amounts are USD; a non-USD listing would divide them by a local-currency mark.
    usd = events["isUsd"].fillna(True).astype(bool)
    usable = have_terms & stock_ok & np.isfinite(last_close) & (last_close > 0) & usd
    candidate = pd.Series(np.where(usable, proceeds / last_close - 1.0, np.nan), index=events.index)

    cash_only = cash.notna() & ratio.isna()
    too_big = candidate.abs() > args.max_return
    cash_too_big = cash_only & (candidate.abs() > args.max_cash_return)
    # A rename that preserves value cannot move the mark much; a large jump means the event
    # was a separation or a split we could not read, so the 1:1 assumption is withdrawn.
    rename_too_big = events.get("assumedRename", pd.Series(False, index=events.index)).fillna(False) & \
        (candidate.abs() > args.max_rename_return)
    rejected = candidate.notna() & (too_big | cash_too_big | rename_too_big)
    events["rejectReason"] = np.where(
        rejected, np.where(cash_too_big, "cash_R_out_of_range",
                           np.where(rename_too_big, "rename_not_1to1", "R_out_of_range")),
        np.where(have_terms & ~usd, "non_usd_listing",
                 np.where(have_terms & ~stock_ok, "no_acquirer_quote",
                          np.where(have_terms & ~(np.isfinite(last_close) & (last_close > 0)),
                                   "no_target_mark", ""))))
    events["settlementReturn"] = candidate.where(~rejected)
    # cashPerShare / successorRatio / successorTradingItemId are the engine's primary inputs now,
    # so a term set we proved wrong must be withdrawn from all of them, not just from R.
    events["rejectedCash"] = cash.where(rejected)
    events["rejectedRatio"] = ratio.where(rejected)
    events["rejectedReturn"] = candidate.where(rejected)
    # The raw terms stay: they are the recorded economic fact. What a rejection withdraws is
    # the *evaluation input* - cacheUnitStatus becomes rejected:<reason> and both cache-unit
    # columns are NaN below, so the engine cannot compute from a term set we proved wrong.
    events["suggestedReturn"] = np.where(events["actionCode"].isin(BANKRUPTCY_CODES), -1.0, np.nan)
    # A bankruptcy/liquidation return is a policy suggestion, never an explicit settlement return.
    events.loc[events["actionCode"].isin(BANKRUPTCY_CODES), "settlementReturn"] = np.nan
    # The successor leg is priced on the settlement bar, which can precede the vendor's record
    # date; that is not an error but it means the record date did not confirm the valuation day.
    early = (events["acquirerCloseDate"].notna() & events["settlementDate"].notna() &
             (pd.to_datetime(events["acquirerCloseDate"]) < pd.to_datetime(events["settlementDate"])))
    events["reviewFlag"] = [";".join(part for part in (flag, "succ_valued_before_record" if hit else "")
                                     if part)
                            for flag, hit in zip(events["reviewFlag"].fillna(""), early)]
    events["source"] = np.where(events["matchMethod"] == "none", "master", "co_acthist")
    known = np.isfinite(pd.to_numeric(events["settlementReturn"], errors="coerce")) | \
        events["actionCode"].isin(BANKRUPTCY_CODES)
    events["confidence"] = np.where(known, "parsed", "unknown")

    override_rows = len(override_frame)
    is_override = events["isOverride"].fillna(False).astype(bool)
    events.loc[is_override, "source"] = "override"
    events.loc[is_override, "confidence"] = "manual"

    # ---- structure flags and cache-unit evaluation inputs ------------------------------
    # Structure is an economic fact read off the raw terms; conversion is a separate question.
    cash_raw = pd.to_numeric(events["cashPerShare"], errors="coerce")
    ratio_raw = pd.to_numeric(events["successorRatio"], errors="coerce")
    has_successor = events["successorTradingItemId"].map(
        lambda v: v is not None and not (isinstance(v, float) and not np.isfinite(v)))
    events["hasCashLeg"] = (np.isfinite(cash_raw) & (cash_raw > 0)).fillna(False)
    events["hasStockLeg"] = (np.isfinite(ratio_raw) | has_successor.astype(bool) |
                             events["stockLegDeclared"].fillna(False).astype(bool))
    events["cacheMark"] = pd.to_numeric(events["cacheMark"], errors="coerce")
    events["cacheAtKt"] = pd.to_numeric(events["cacheAtKt"], errors="coerce")
    cache_reject = events["rejectReason"].fillna("").where(
        events["rejectReason"].isin(CACHE_REJECTIONS), "")
    resolved = [resolve_cache_units(
        has_cash=bool(row.hasCashLeg), has_stock=bool(row.hasStockLeg),
        cash=float(row.cashPerShare) if pd.notna(row.cashPerShare) else np.nan,
        ratio=float(row.successorRatio) if pd.notna(row.successorRatio) else np.nan,
        has_successor=bool(has_successor.at[row.Index]),
        as_traded_target=float(row.asTradedTargetAtMark) if pd.notna(row.asTradedTargetAtMark) else np.nan,
        cache_at_kt=float(row.cacheAtKt) if pd.notna(row.cacheAtKt) else np.nan,
        cache_mark=float(row.cacheMark) if pd.notna(row.cacheMark) else np.nan,
        as_traded_successor=float(row.asTradedSuccessorAtBar) if pd.notna(row.asTradedSuccessorAtBar) else np.nan,
        cache_successor=float(row.successorCacheClose) if pd.notna(row.successorCacheClose) else np.nan,
        reject_reason=cache_reject.at[row.Index]) for row in events.itertuples()]
    for name in ("cashPerShareCache", "successorRatioCache", "cacheUnitFactorTarget",
                 "cacheUnitFactorSuccessor", "cacheUnitStatus"):
        events[name] = [item[name] for item in resolved]
    convertible = events["cashPerShareCache"].notna() | events["successorRatioCache"].notna()
    events["targetCacheMark"] = events["cacheMark"].where(convertible)
    events["targetCacheMarkDate"] = events["markDate"].where(convertible)
    events["successorCacheCloseGuard"] = events["successorCacheClose"].where(
        events["successorRatioCache"].notna())
    events["successorCacheCloseDate"] = events["conversionDate"].where(
        events["successorRatioCache"].notna())

    events = events[events["endDate"].notna()].copy()
    events.index.name = "tradingItemId"
    if events.index.duplicated().any():
        raise RuntimeError("duplicate tradingItemId rows in the event table")

    out = pd.DataFrame(index=events.index)
    out["tradingItemId"] = [str(int(x)) for x in events.index]
    out["endDate"] = pd.to_datetime(events["endDate"]).dt.normalize()
    out["settlementDate"] = pd.to_datetime(events["settlementDate"]).dt.normalize()
    out["actionCode"] = events["actionCode"].astype("object").where(events["actionCode"].notna(), None)
    out["settlementReturn"] = pd.to_numeric(events["settlementReturn"], errors="coerce").astype("float64")
    out["suggestedReturn"] = pd.to_numeric(events["suggestedReturn"], errors="coerce").astype("float64")
    out["cashPerShare"] = pd.to_numeric(events["cashPerShare"], errors="coerce").astype("float64")
    successor_ids = events["successorTradingItemId"]
    out["successorTradingItemId"] = [None if (s is None or (isinstance(s, float) and not np.isfinite(s)))
                                     else str(int(s)) for s in successor_ids]
    out["successorRatio"] = pd.to_numeric(events["successorRatio"], errors="coerce").astype("float64")
    out["acquirerName"] = events["acquirerName"].astype("object").where(events["acquirerName"].notna(), None)
    out["source"] = events["source"].astype("object")
    out["confidence"] = events["confidence"].astype("object")
    out["actionText"] = [None if not isinstance(t, str) else t[:200] for t in events["actionText"]]
    out["matchMethod"] = events["matchMethod"].astype("object")
    out["endDateSource"] = events["endDateSource"].astype("object")
    out["endDateConflict"] = events["endDateConflict"].fillna(False).astype(bool)
    out["dbEndDateReason"] = events["dbEndDateReason"].fillna("").astype("object")
    out["endDateOriginal"] = pd.to_datetime(events["endDateOriginal"]).dt.normalize()
    out["endDateGapDays"] = pd.to_numeric(events["endDateGapDays"], errors="coerce").fillna(0).astype("int32")
    out["reviewFlag"] = events["reviewFlag"].fillna("").astype("object")
    out["endDateBasis"] = events["endDateBasis"].fillna("original").astype("object")
    # The bar whose close priced the acquirer leg; never later than the settlement bar.
    out["acquirerPriceDate"] = pd.to_datetime(events["acquirerCloseDate"])
    # Cache-unit terms: what the engine multiplies by its own close panel.
    for name, column in (("cashPerShareCache", "cashPerShareCache"),
                         ("successorRatioCache", "successorRatioCache"),
                         ("cacheUnitFactorTarget", "cacheUnitFactorTarget"),
                         ("cacheUnitFactorSuccessor", "cacheUnitFactorSuccessor"),
                         ("targetCacheMark", "targetCacheMark"),
                         ("successorCacheClose", "successorCacheCloseGuard")):
        out[name] = pd.to_numeric(events[column], errors="coerce").astype("float64")
    out["targetCacheMarkDate"] = pd.to_datetime(events["targetCacheMarkDate"])
    out["cacheUnitFactorTargetDate"] = pd.to_datetime(events["ktDate"]).where(
        pd.to_numeric(events["cacheUnitFactorTarget"], errors="coerce").notna())
    out["successorCacheCloseDate"] = pd.to_datetime(events["successorCacheCloseDate"])
    out["hasCashLeg"] = events["hasCashLeg"].fillna(False).astype(bool)
    out["hasStockLeg"] = events["hasStockLeg"].fillna(False).astype(bool)
    out["cacheUnitStatus"] = events["cacheUnitStatus"].fillna("").astype("object")
    out["overrideSource"] = events["overrideSource"].fillna("").astype("object")
    out = out[EVENT_COLUMNS].reset_index(drop=True)

    schema = pa.schema([
        ("tradingItemId", pa.string()), ("endDate", pa.timestamp("ns")), ("settlementDate", pa.timestamp("ns")),
        ("actionCode", pa.string()), ("settlementReturn", pa.float64()), ("suggestedReturn", pa.float64()),
        ("cashPerShare", pa.float64()), ("successorTradingItemId", pa.string()), ("successorRatio", pa.float64()),
        ("acquirerName", pa.string()), ("source", pa.string()), ("confidence", pa.string()),
        ("actionText", pa.string()), ("matchMethod", pa.string()),
        ("endDateSource", pa.string()), ("endDateConflict", pa.bool_()),
        ("dbEndDateReason", pa.string()), ("endDateOriginal", pa.timestamp("ns")),
        ("endDateGapDays", pa.int32()), ("reviewFlag", pa.string()), ("endDateBasis", pa.string()),
        ("acquirerPriceDate", pa.timestamp("ns")),
        ("hasCashLeg", pa.bool_()), ("hasStockLeg", pa.bool_()), ("cacheUnitStatus", pa.string()),
        ("overrideSource", pa.string()),
        ("cashPerShareCache", pa.float64()), ("successorRatioCache", pa.float64()),
        ("cacheUnitFactorTarget", pa.float64()), ("cacheUnitFactorSuccessor", pa.float64()),
        ("targetCacheMark", pa.float64()), ("targetCacheMarkDate", pa.timestamp("ns")),
        ("cacheUnitFactorTargetDate", pa.timestamp("ns")),
        ("successorCacheClose", pa.float64()), ("successorCacheCloseDate", pa.timestamp("ns")),
    ])
    pq.write_table(pa.Table.from_pandas(out, schema=schema, preserve_index=False), out_path)
    log(f"      wrote {out_path} ({len(out)} rows)")

    universes = {}
    if not args.skip_universe:
        for name in ("Top500", "Top1000", "ALL"):
            path = Path(args.universe_dir) / f"{name}.parquet"
            if path.exists():
                log(f"      universe {name}")
                universes[name] = read_universe_members(str(path), pd.Timestamp(args.universe_start))

    symbols_by_id = {int(k): (None if pd.isna(v) else str(v)) for k, v in master["symbol"].items()}
    write_report(report_path, args, out, events, targets, all_targets, match_stats, universes,
                 conflicts, int(db_only.sum()), int(master_only.sum()), override_note, override_rows,
                 symbols_by_id, set(closes._columns), conflict_stats, never_settle)
    log(f"      wrote {report_path}")
    verify(out_path, len(out), closes)
    return 0


# --------------------------------------------------------------------------------------
# Report + self-verification
# --------------------------------------------------------------------------------------
# Rows the Phase 2 review flagged (review_p2_report.md P2-01/P2-02/P2-18) with the settlement
# return the pre-fix 2026-09-11 build produced, so the report can show before -> after.
REVIEW_FIXES = [
    ("2601589", "CMA", "P2-01 cash equivalent", 0.150444, "+0.0934 (1.8663x51.95/88.67-1)"),
    ("40272951", "DISH", "P2-01 cash equivalent", -0.121600, "주식 전액"),
    ("2639311", "PXD", "P2-01 cash equivalent", 0.001300, "주식 전액"),
    ("542341937", "CADE", "P2-01 cash equivalent", 0.078300, "주식 전액"),
    ("2642029", "RPT", "P2-01 cash equivalent", 0.008000, "주식 전액"),
    ("1671892993", "IS", "P2-01 cash equivalent", -0.011200, "주식 전액"),
    ("606938070", "MNRL", "P2-01 cash equivalent", float("nan"), "주식 전액"),
    ("2650011", "SJR", "P2-02 CAN$", 0.341948, "CAD 대가 → NaN"),
    ("141549237", "KOR", "P2-02 CAN$", 0.277259, "CAD 대가 → NaN"),
    ("29975856", "PNI", "P2-02 CDN$", 0.089744, "CAD 대가 → NaN"),
    ("192288682", "SVLC", "P2-02 CDN$", 0.180810, "CAD 대가 → NaN"),
    ("24884901", "SILU", "P2-02 CDN$", 0.061840, "CAD 대가 → NaN"),
    ("23181585", "HTE", "P2-02 CDN$", 0.062699, "CAD 대가 → NaN"),
    ("128444415", "XG", "P2-02 CDN$", -0.001553, "CAD 대가 → NaN"),
    ("2607489", "DRE", "P2-18 티커+SHARES", float("nan"), "0.475 PLD"),
]

# symbol, expected cash, expected ratio, expected successor symbol, lead's stated terms
HAND_CHECKS = [
    ("34969516", "DFS", None, 1.0192, "COF", "1.0192 COF"),
    ("27143925", "HES", None, 1.025, "CVX", "1.025 CVX"),
    ("2590144", "ANSS", 199.91, 0.3399, "SNPS", "$199.91 + 0.3399 SNPS"),
    ("267935529", "CYBR", 45.0, 2.2005, "PANW", "$45 + 2.2005 PANW"),
    ("2610459", "EXAS", 105.0, None, None, "$105 (override)"),
    ("2608773", "EA", 210.0, None, None, "$210 (override)"),
    ("2591859", "AVB", None, 2.793, "VMRK", "2.793 VMRK (override)"),
    ("2610085", "EQR", None, 1.0, "VMRK", "N/C 1.0 VMRK (override)"),
]


def table(rows, header) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def close_enough(got, want) -> bool:
    if want is None:
        return got is None
    return got is not None and abs(got - want) <= 1e-6 * max(1.0, abs(want))


def write_report(path, args, out, events, targets, all_targets, match_stats, universes,
                 conflicts, db_only, master_only, override_note, override_rows, symbols_by_id,
                 cache_symbols=None, conflict_stats=None, never_settle=0):
    conflict_stats = conflict_stats or {}
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(out)
    matched = int((out["matchMethod"] != "none").sum())
    parsed_terms = events["cashPerShare"].notna() | events["successorRatio"].notna()
    r = out["settlementReturn"]
    finite = r[np.isfinite(r)]
    kind = events["parseKind"].fillna("no_match")

    lines = [f"# 정산 이벤트 파일 보고 ({now})", "",
             f"생성 명령: `build_events.py --master {args.master} --out {args.out} "
             f"--override {args.override} --start {args.start} --window {args.window}`", "",
             "## 1. 규칙", "",
             "- **구조(사실)와 평가 입력(캐시 단위)을 분리**함: `hasCashLeg`·`hasStockLeg`는 원 조건에서 "
             "읽은 다리 존재 여부이고, `cashPerShareCache`·`successorRatioCache`는 **선언된 다리를 전부 "
             "환산했을 때만** 채워짐(하나라도 실패하면 둘 다 NaN). 실패 사유는 `cacheUnitStatus`에 기록함.",
             "- **엔진 입력은 `*Cache` 컬럼**임: `cashPerShareCache`·`successorRatioCache`"
             "(+`successorTradingItemId`). 엔진이 곱하는 값은 로컬 캐시 종가라 대가 조건도 캐시 단위로 "
             "환산해 넣어야 함 — 이 컬럼들은 **캐시 빌드에 묶여 있으므로 캐시를 재구축하면 반드시 "
             "이 파일도 재생성해야 함**. `cashPerShare`·`successorRatio`는 딜 당시 거래 기준 원값이고 "
             "감사·재계산용임.",
             "- 1차 출력은 대가 조건임: `cashPerShare`·`successorRatio`·`successorTradingItemId`. "
             "엔진이 이 셋으로 정산 수익률을 직접 계산하므로 혼합 딜도 현금과 주식을 **각각** 채우고 "
             "하나의 R로 뭉치지 않음. `settlementReturn`은 참고값(승계사가 패널 밖이거나 엔진이 "
             "computed 경로를 못 쓰는 딜용)이며 아래 규칙으로만 계산함.",
             "- `endDate` = **`xfl_target.ciqsecurity.securityenddate` 우선**, 없으면 로컬 마스터 "
             "`securityEndDate`. 둘이 다르면 DB를 채택하고 `endDateSource`·`endDateConflict`에 기록, "
             "목록은 `enddate_conflicts.csv`. 로컬값이 후속 종목 종가가 구 id에 붙은 아티팩트인 경우가 "
             "있어 DB를 권위로 둠(WOLF `2603877`: 로컬 2026-08-11 → DB 2025-09-26 채택).",
             "- `settlementDate` = `co_acthist.datadate`(사건 기록일), 매칭 실패 시 `endDate`.",
             f"- co_acthist 매칭 창 = `|datadate − endDate| ≤ {args.window}`일. 여러 건이면 **가장 가까운 datadate**"
             " (동률은 ACQ/MGD/LBO/PVT → BK*/LIQ → 기타 순) 1건만 채택함.",
             "- `settlementReturn = (cash + ratio × acquirerClose) / lastClose − 1`. "
             "`lastClose`의 **날짜**는 로컬 캐시 `BaseData/close.parquet`에서 **정산 bar 직전** 마지막 유효 "
             "종가가 있는 bar(= 엔진이 정산 시점에 들고 있는 mark의 날짜)이고, **값**은 그 날짜의 as-traded 가격임"
             "(아래 가격 소스 참조). 캐시에 종가가 아예 없으면 `ciqpriceequity` 30일 소급.",
             "- **정산 bar** = 종료일 이후 **대상 종가가 처음 없는 bar**(엔진은 호가가 이어지면 정산을 "
             "미룸 — `simulator.cpp` `event_now = event_due && !observed`). mark는 그 bar **직전** "
             "마지막 유효 종가임. 예: DFS 마지막 종가 2025-05-16(200.05), 정산 bar 2025-05-19.",
             "- `acquirerClose`는 **정산 bar 당일 종가**, 그 bar에 없으면 **그 이전 마지막 종가**만 씀. "
             "정산일 이후를 뒤지는 폴백은 룩어헤드라 제거했고, `acquirerPriceDate`에 실제 사용한 날짜를 "
             "기록해 §4-2에서 정산일보다 늦은 행이 0건임을 보임. 승계사 첫 호가가 정산 bar보다 늦으면 "
             "R은 NaN이고 조건(ratio·successor)만 남김.",
             "- **가격 소스**: 두 종가 모두 `ciqpriceequity.priceclose × adjustmentfactor`(그날 실제 "
             "거래된 가격)를 씀. 캐시는 수집 당일의 raw `priceclose`를 그대로 담고 있는데, CIQ는 나중에 "
             "분할·교환이 생기면 `priceclose`를 현재 주식 기준으로 재작성하고 `adjustmentfactor`에 환산값을 "
             "남김(GOOG 2015-10-05: 32.0735 × 20 = 641.47 = 캐시값). 그래서 캐시와 DB가 어긋나는 종목이 "
             "정확히 정산에서 두 종목을 비교해야 하는 종목들임. R은 비율이라 스케일이 상쇄되므로 **두 다리를 "
             "모두 as-traded로 통일**하는 것이 옳고, 캐시는 *어느 bar*에서 mark·정산하는지 정하는 데만 씀. "
             "override 4건은 factor=1.0이라 캐시값을 그대로 씀.",
             "- BK11/BK7/BKR/LIQ는 `suggestedReturn=-1`, `settlementReturn`은 NaN(정책은 엔진 config가 결정).",
             f"- `--start {args.start}` 이전 종료일은 파일에서 제외함"
             f"(전체 종료일 {len(all_targets)}건 중 {len(all_targets) - len(targets)}건).", "",
             "## 2. 대상·매칭", "",
             table([
                 ["로컬 마스터 행", len(all_targets) and f"{28210}"],
                 ["종료일 있는 종목(로컬∪DB)", len(all_targets)],
                 [f"종료일 ≥ {args.start}", len(targets)],
                 ["로컬 null → DB에만 있음", db_only],
                 ["DB null → 로컬에만 있음", master_only],
                 ["로컬·DB 종료일 불일치", len(conflicts)],
                 ["출력 행수", total],
                 ["사건 매칭(co_acthist+override)", f"{matched} ({matched / max(total,1):.1%})"],
                 ["  · (gvkey,iid) 정확 매칭", match_stats.get("exact_matched", 0)],
                 ["  · oldtic1 보조 매칭", match_stats.get("oldtic1_matched", 0)],
                 ["  · override", override_rows],
                 ["  · 매칭 없음(종료일만)", total - matched],
                 ["override 적용", override_note],
             ], ["항목", "값"]), ""]

    counts = out.groupby(out["actionCode"].fillna("(매칭없음)")).agg(
        rows=("tradingItemId", "size"),
        with_return=("settlementReturn", lambda s: int(np.isfinite(s).sum())),
        with_successor=("successorTradingItemId", lambda s: int(s.notna().sum())))
    rows = [[code, int(row.rows), f"{row.with_return} ({row.with_return / row.rows:.0%})",
             int(row.with_successor)] for code, row in counts.sort_values("rows", ascending=False).iterrows()]
    lines += ["### actionCode별 매칭·산출", "", table(rows, ["actionCode", "행수", "settlementReturn 유한", "successor 있음"]), ""]

    kind_rows = [[k, int(v), f"{v / matched:.1%}" if matched else "-"]
                 for k, v in kind.value_counts().items() if k != "no_match"]
    lines += ["## 3. 조건 파싱", "",
              f"매칭된 {matched}건 중 조건(현금 또는 교환비) 확보 {int(parsed_terms.sum())}건 "
              f"({parsed_terms.sum() / max(matched,1):.1%}), `settlementReturn` 유한 {len(finite)}건 "
              f"({len(finite) / max(total,1):.1%} of 전체).", "",
              table(kind_rows, ["파싱 결과", "건수", "매칭 대비"]), ""]

    cash_only = out[(out["cashPerShare"].notna()) & (out["successorTradingItemId"].isna()) &
                    np.isfinite(out["settlementReturn"])]
    stock = out[(out["successorTradingItemId"].notna()) & np.isfinite(out["settlementReturn"])]
    def stats(series):
        if not len(series):
            return ["-"] * 7
        q = series.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        return [len(series), f"{series.mean():.4f}", f"{q[0.05]:.4f}", f"{q[0.25]:.4f}",
                f"{q[0.5]:.4f}", f"{q[0.75]:.4f}", f"{q[0.95]:.4f}"]
    lines += ["## 4. settlementReturn 분포", "",
              table([["전체"] + stats(finite), ["현금 전용 딜"] + stats(cash_only["settlementReturn"]),
                     ["주식·혼합 딜"] + stats(stock["settlementReturn"])],
                    ["구분", "n", "평균", "p05", "p25", "중앙", "p75", "p95"]), "",
              "현금 딜의 R은 마지막 거래일 종가와 인수대금 차이라 0 근처여야 함."]
    lines += ["", "남아 있는 이상치는 대부분 (a) CVR·조건부 대가가 붙어 현금 명시분이 실제 대가보다 작은 딜"
              "(TOBIRA $28.35+CVR, SGTX $14.92+CVR), (b) 공개매수가 불확실해 마지막 호가가 인수가보다 낮았던 딜"
              "(NTSU 마지막 종가 90.34 → $109), (c) 벤더 원문 오기임. 파싱으로 더 줄일 수 있는 건은 아님.", ""]
    outliers = cash_only[cash_only["settlementReturn"].abs() > 0.10].copy()
    lines += ["", f"**|R| > 10% 현금 딜 이상치: {len(outliers)}건 / {len(cash_only)}건 "
                  f"({len(outliers) / max(len(cash_only),1):.1%})**", ""]
    show = outliers.reindex(outliers["settlementReturn"].abs().sort_values(ascending=False).index).head(25)
    lines += [table([[row.tradingItemId, row.endDate.date(), row.actionCode, f"{row.cashPerShare:g}",
                      f"{events.at[int(row.tradingItemId), 'lastClose']:.4g}",
                      f"{row.settlementReturn:.3f}", (row.actionText or "")[:80]]
                     for row in show.itertuples()],
                    ["tradingItemId", "endDate", "code", "cash", "lastClose", "R", "actionText"]), ""]

    reasons = events["rejectReason"].fillna("").replace("", np.nan).dropna()
    lines += ["### R을 만들지 못한/버린 사유", "",
              table([[k, int(v)] for k, v in reasons.value_counts().items()], ["사유", "건수"]), "",
              "`cash_R_out_of_range`(현금 딜 |R| > "
              f"{args.max_cash_return:.0%})·`R_out_of_range`(|R| > {args.max_return:.0%})는 "
              "원문 오기·집계액 오인식이 대부분이라 `settlementReturn`을 NaN으로 두고 successor도 지움"
              "(엔진 기본 정산으로 떨어뜨림).", ""]
    dropped = events[events["rejectReason"].isin(["cash_R_out_of_range", "R_out_of_range"])].copy()
    if len(dropped):
        dropped["absR"] = pd.to_numeric(dropped["lastClose"], errors="coerce").fillna(0)
        top = dropped.sort_values("absR", ascending=False).head(15)
        lines += ["버려진 상위 15건(마지막 종가 기준):", "",
                  table([[idx, row.symbol, str(pd.Timestamp(row.endDate).date()), row.actionCode,
                          f"{row.cashPerShare:g}" if pd.notna(row.cashPerShare) else "-",
                          f"{row.lastClose:.4g}" if np.isfinite(row.lastClose) else "-",
                          (str(row.actionText) or "")[:80]] for idx, row in top.iterrows()],
                        ["tradingItemId", "symbol", "endDate", "code", "cash", "lastClose", "actionText"]), ""]

    every = out[np.isfinite(out["settlementReturn"])]
    wide = every[every["settlementReturn"].abs() > 0.20]
    lines += ["", f"**전체(현금·주식·혼합) |R| > 20%: {len(wide)}건 / {len(every)}건 "
                  f"({len(wide) / max(len(every),1):.1%})** — 상위 12건", "",
              table([[row.tradingItemId, row.endDate.date(), row.actionCode,
                      f"{row.cashPerShare:g}" if pd.notna(row.cashPerShare) else "-",
                      f"{row.successorRatio:g}" if pd.notna(row.successorRatio) else "-",
                      f"{row.settlementReturn:.3f}", (row.actionText or "")[:70]]
                     for row in wide.reindex(wide["settlementReturn"].abs()
                                             .sort_values(ascending=False).index).head(12).itertuples()],
                    ["tradingItemId", "endDate", "code", "cash", "ratio", "R", "actionText"]), ""]

    priced = out[out["acquirerPriceDate"].notna()].copy()
    gap = (events["conversionDate"].reindex(events.index) - events["acquirerCloseDate"]).dt.days
    gap = gap[events["acquirerCloseDate"].notna()]
    ahead = int((gap < 0).sum())
    lines += ["### 4-2. 룩어헤드 점검 (인수사 종가 날짜)", "",
              "`acquirerPriceDate`가 정산 bar보다 **늦은 행이 있으면 룩어헤드**임.", "",
              table([["인수사 종가가 붙은 행", len(priced)],
                     ["정산 bar **이후** 날짜 사용 (룩어헤드)", ahead],
                     ["정산 bar 당일 사용", int((gap == 0).sum())],
                     ["정산 bar 이전 사용 (1~5일 전)", int(((gap > 0) & (gap <= 5)).sum())],
                     ["정산 bar 이전 사용 (6일 초과)", int((gap > 5).sum())]],
                    ["항목", "행수"]), "",
              ("**룩어헤드 0건 확인.**" if ahead == 0 else f"**경고: 룩어헤드 {ahead}건**"), "",
              "수정 전 빌드는 정산일 이후 최대 10거래일까지 앞을 뒤졌고, 그 경로로 미래 가격을 쓴 행이 "
              "32건이었음(리뷰 지적). 지금은 해당 행의 인수사 종가가 정산 bar 이전 가격으로 바뀌거나 "
              "(승계사 상장 전이면) R이 NaN으로 빠짐 — 조건 컬럼은 그대로 남으므로 엔진 승계는 유지됨.", ""]

    source_counts = out["endDateSource"].value_counts()
    unit = out[out["successorTradingItemId"].notna()].copy()
    kt = unit["cacheUnitFactorTarget"]
    ks = unit["cacheUnitFactorSuccessor"]
    off_t = (kt.notna() & ((kt - 1.0).abs() > 1e-6))
    off_s = (ks.notna() & ((ks - 1.0).abs() > 1e-6))
    lines += ["### 4-2b. 단위 불일치 보정 (캐시 단위 환산)", "",
              "엔진은 `R=(cash+ratio·cacheClose_succ)/cacheMark−1`을 캐시 종가로 계산하는데 딜 조건은 "
              "당시 거래 주식 기준이라 단위가 섞임. 각 다리를 **as-traded 가격 ÷ 같은 날짜 캐시 종가**"
              "(`cacheUnitFactorTarget` k_t, `cacheUnitFactorSuccessor` k_s)로 앵커해 "
              "`cashPerShareCache = cash/k_t`, `successorRatioCache = ratio·k_s/k_t`를 만들었음. "
              "캐시가 DB `priceclose`와 비율이 안 맞는 종목이 있어 DB 배수를 그냥 곱하면 안 되므로 "
              "**캐시 값에 직접 앵커**함.", "",
              table([["successor 행", len(unit)],
                     ["k_t ≠ 1 (대상 기준 다름)", int(off_t.sum())],
                     ["k_s ≠ 1 (승계사 기준 다름)", int(off_s.sum())],
                     ["둘 중 하나라도 ≠ 1", int((off_t | off_s).sum())],
                     ["환산 컬럼 산출됨", int(unit["successorRatioCache"].notna().sum())],
                     ["환산 불가(캐시/DB 가격 결측) → 엔진은 파일 R 폴백",
                      int(unit["successorRatioCache"].isna().sum())]],
                    ["항목", "행수"]), ""]
    scale = pd.concat([kt[off_t], ks[off_s]])
    if len(scale):
        lines += ["배수 분포(1이 아닌 k): " +
                  ", ".join(f"{v:g}×{c}건" for v, c in
                            scale.round(4).value_counts().head(10).items()), ""]
    show = ["2634982", "312940617", "2635984"]
    rows = []
    for tid in show:
        hit = out[out["tradingItemId"] == tid]
        if not len(hit):
            rows.append([tid, "(파일에 없음)", "", "", "", "", ""])
            continue
        row = hit.iloc[0]
        naive = ((row["cashPerShare"] if pd.notna(row["cashPerShare"]) else 0.0) +
                 (row["successorRatio"] if pd.notna(row["successorRatio"]) else 0.0) *
                 (row["successorCacheClose"] if pd.notna(row["successorCacheClose"]) else np.nan)) / \
                row["targetCacheMark"] - 1.0 if pd.notna(row["targetCacheMark"]) else np.nan
        fixed = ((row["cashPerShareCache"] if pd.notna(row["cashPerShareCache"]) else 0.0) +
                 (row["successorRatioCache"] if pd.notna(row["successorRatioCache"]) else 0.0) *
                 (row["successorCacheClose"] if pd.notna(row["successorCacheClose"]) else np.nan)) / \
                row["targetCacheMark"] - 1.0 if pd.notna(row["targetCacheMark"]) else np.nan
        rows.append([tid, row["successorTradingItemId"] or "-",
                     f"{row['successorRatio']:g}" if pd.notna(row["successorRatio"]) else "-",
                     f"{row['successorRatioCache']:g}" if pd.notna(row["successorRatioCache"]) else "-",
                     f"{row['cacheUnitFactorTarget']:g}" if pd.notna(row["cacheUnitFactorTarget"]) else "-",
                     f"{row['cacheUnitFactorSuccessor']:g}" if pd.notna(row["cacheUnitFactorSuccessor"]) else "-",
                     f"{naive:+.4f}" if np.isfinite(naive) else "NaN",
                     f"{fixed:+.4f}" if np.isfinite(fixed) else "NaN",
                     f"{row['settlementReturn']:+.4f}" if np.isfinite(row["settlementReturn"]) else "NaN"])
    convertible = out[out["successorRatioCache"].notna() & out["targetCacheMark"].notna() &
                      out["successorCacheClose"].notna()]
    if len(convertible):
        naive_all = ((convertible["cashPerShare"].fillna(0.0) +
                      convertible["successorRatio"].fillna(0.0) * convertible["successorCacheClose"]) /
                     convertible["targetCacheMark"] - 1.0)
        fixed_all = ((convertible["cashPerShareCache"].fillna(0.0) +
                      convertible["successorRatioCache"] * convertible["successorCacheClose"]) /
                     convertible["targetCacheMark"] - 1.0)
        delta = (naive_all - fixed_all).abs()
        lines += [f"환산으로 바로잡힌 엔진 R 오차: |Δ| > 10% **{int((delta > 0.1).sum())}건**, "
                  f"> 1% {int((delta > 0.01).sum())}건 (대상 {len(convertible)}건).", ""]
    lines += ["리드가 지목한 3건(환산 전 = 원 조건을 캐시 종가에 그대로 곱한 값, 환산 후 = `*Cache` 사용):", "",
              table(rows, ["tradingItemId", "successor", "ratio", "ratioCache", "k_t", "k_s",
                           "환산 전 R", "환산 후 R", "파일 R(as-traded)"]), ""]

    # --- contract v3: structure vs evaluation -----------------------------------------
    stock_no_cache = out[out["hasStockLeg"] & out["successorRatio"].notna() &
                         out["successorRatioCache"].isna() & out["hasCashLeg"]]
    core_r1 = stock_no_cache[stock_no_cache["successorTradingItemId"].notna() &
                             ~stock_no_cache["cacheUnitStatus"].str.startswith("rejected")]
    naive_cash = (stock_no_cache["cashPerShare"] / stock_no_cache["cacheUnitFactorTarget"]).where(
        stock_no_cache["cacheUnitFactorTarget"].notna(), stock_no_cache["cashPerShare"])
    no_successor = out[out["successorRatio"].notna() & out["successorTradingItemId"].isna()]
    named = ["2665532", "11114106", "241723627", "282449174"]
    lines += ["### 4-2c. 구조 vs 평가 분리 (계약 v3, 리뷰 R1)", "",
              "`hasCashLeg`·`hasStockLeg`는 **원 조건에서 읽은 구조적 사실**이고, `*Cache`는 **선언된 다리를 "
              "전부 환산했을 때만** 값이 있음(전부-또는-전무). 승계사 환산이 실패하면 현금 다리도 같이 "
              "비워서 엔진이 불완전한 현금 다리로 전액 정산하는 일이 없게 함 — 리뷰가 든 IWA(12678069) "
              "사례가 여기 해당함.", "",
              table([["hasCashLeg", int(out["hasCashLeg"].sum())],
                     ["hasStockLeg", int(out["hasStockLeg"].sum())],
                     ["둘 다", int((out["hasCashLeg"] & out["hasStockLeg"]).sum())],
                     ["둘 다 아님(구조 불명)", int((~out["hasCashLeg"] & ~out["hasStockLeg"]).sum())],
                     ["평가 입력 있음(cacheUnitStatus=ok)", int((out["cacheUnitStatus"] == "ok").sum())]],
                    ["항목", "행수"]), "",
              "**cacheUnitStatus 분포**", "",
              table([[k, int(v)] for k, v in out["cacheUnitStatus"].value_counts().items()],
                    ["cacheUnitStatus", "행수"]), "",
              f"**R1이 지적한 집합**(주식 다리 선언 + 원 ratio 유한 + ratioCache NaN + 현금 다리 있음): "
              f"현재 **{len(stock_no_cache)}건**(그중 승계사 id가 있고 거절도 아닌 핵심 "
              f"**{len(core_r1)}건** = 리뷰의 32건에 해당). 이전 계약에서는 `cashPerShareCache`가 "
              "살아남아 엔진이 현금만으로 정산했으나 이제 전부 NaN임. 리뷰 사례 IWA(`12678069`)는 "
              "`no_cache_successor_at_settlement`로 분류되어 파일 R(−0.0108835542)로 폴백함.", "",
              "사유별: " + ", ".join(f"{k} {v}건" for k, v in
                                      stock_no_cache["cacheUnitStatus"].value_counts().items()), ""]
    show = stock_no_cache.reindex(naive_cash.sort_values(ascending=False).index).head(12)
    lines += [table([[row.tradingItemId, row.endDate.date(), row.actionCode,
                      f"{row.cashPerShare:g}", f"{row.successorRatio:g}",
                      row.successorTradingItemId or "-",
                      f"{row.cashPerShare / row.cacheUnitFactorTarget:g}"
                      if pd.notna(row.cacheUnitFactorTarget) else f"{row.cashPerShare:g}",
                      "NaN", row.cacheUnitStatus] for row in show.itertuples()],
                    ["tradingItemId", "endDate", "code", "cash", "ratio", "successor",
                     "이전 cashCache", "현재 cashCache", "status"]), "",
              f"**승계사 id 없이 교환비만 있는 행**(이전 `:1101`이 원 ratio까지 삭제하던 경로): "
              f"**{len(no_successor)}건**. 이제 원 `successorRatio`를 보존하고 `hasStockLeg=true`, "
              "평가 입력은 둘 다 NaN으로 둠. 리뷰가 원문으로 혼합 대가를 확인한 4건 포함 여부: " +
              ", ".join(f"{tid}={'포함' if tid in set(no_successor['tradingItemId']) else '미포함'}"
                        for tid in named) + ".", ""]
    if len(no_successor):
        lines += [table([[row.tradingItemId, row.endDate.date(), row.actionCode,
                          f"{row.cashPerShare:g}" if pd.notna(row.cashPerShare) else "-",
                          f"{row.successorRatio:g}" if pd.notna(row.successorRatio) else "-",
                          (row.actionText or "")[:70]]
                         for row in no_successor.head(12).itertuples()],
                        ["tradingItemId", "endDate", "code", "cash", "ratio", "actionText"]), ""]

    lines += ["### 4-3. 종료일 출처", "",
              "`ciqsecurity.securityenddate`는 **증권 단위**라 증권이 다른 트레이딩아이템으로 이어지면 "
              "은퇴한 아이템에 수천 일 뒤 날짜가 붙고, 최근 로드일(2026-09-11)은 16,087개 증권에 박힌 "
              "롤링 마커임. 그래서 DB 날짜는 **그 아이템이 실제로 거래되던 시점**일 때만 채택함: "
              f"`dbEndDate ≤ lastpx + {args.db_end_date_tolerance}일`(`lastpx = max(ciqpriceequity.pricingdate)`). "
              "아니면 로컬 마스터 날짜, 로컬도 없으면 `lastpx`. `lastpx` 자체가 없으면 DB. override 최우선. "
              "허용 범위는 **양방향**(`|dbEndDate − lastpx| ≤ 10일`)이며, 단방향에서 양방향으로 바꿔도 "
              "채택 건수는 변하지 않았음(DB 날짜가 마지막 체결일보다 10일 이상 *앞선* 행이 없음).", "",
              table([[k, int(v)] for k, v in source_counts.items()], ["endDateSource", "행수"]), "",
              table([["로컬·DB 충돌 행", len(conflicts)],
                     ["  · DB 채택", conflict_stats.get("db", 0)],
                     ["  · 로컬 채택(`security_level`)", conflict_stats.get("local", 0)],
                     ["  · lastpx 채택", conflict_stats.get("lastpx", 0)],
                     ["`dbEndDateReason=security_level` 전체", int((out["dbEndDateReason"] == "security_level").sum())]],
                    ["항목", "행수"]), "",
              "충돌 전체 목록은 `data/events/enddate_conflicts.csv`"
              "(id·ticker·localEndDate·dbEndDate·lastpx·dbMinusLastpxDays·adopted·reason).", "",
              "리드가 지적한 8건(NTST·USPI·PDCE·SLM·QRTE.A·TLRY·FI·GMGI)은 DB 날짜가 마지막 체결일보다 "
              "수백~7,000일 뒤라 전부 로컬로 되돌아감 — 정산 bar가 패널 밖으로 밀려 영원히 동결되는 회귀가 "
              "사라짐. WOLF(`2603877`)는 `lastpx`=2025-09-26 = dbEndDate라 DB가 유지됨"
              "(로컬 2026-08-11의 8 bar는 후속 종목 종가가 구 id에 붙은 아티팩트).", ""]

    lines += ["### 리뷰 지적 반영 (review_p2_report.md §1)", "",
              "P2-01(`CASH EQUIVALENT OF $x`를 현금 다리로 오파싱), P2-02(`CAN$`/`CDN$` 미필터), "
              "P2-18(`0.475 PLD SHARES FOR EVERY ...` 미파싱)을 고친 뒤 해당 행의 변화임. "
              "`before`는 수정 전 2026-09-11 빌드 값.", ""]
    rows = []
    for tid, symbol, issue, before, expected in REVIEW_FIXES:
        hit = out[out["tradingItemId"] == tid]
        if not len(hit):
            rows.append([symbol, tid, issue, f"{before:+.4f}" if np.isfinite(before) else "NaN",
                         "(파일에 없음)", "", "", expected])
            continue
        row = hit.iloc[0]
        after = row["settlementReturn"]
        rows.append([symbol, tid, issue,
                     f"{before:+.4f}" if np.isfinite(before) else "NaN",
                     f"{after:+.4f}" if np.isfinite(after) else "NaN",
                     f"{row['cashPerShare']:g}" if pd.notna(row["cashPerShare"]) else "-",
                     f"{row['successorRatio']:g}" if pd.notna(row["successorRatio"]) else "-",
                     expected])
    lines += [table(rows, ["종목", "tradingItemId", "지적", "before R", "after R", "cash", "ratio",
                           "리뷰 기대"]), "",
              f"`excluded_equivalent`(환산 문구 금액을 대가에서 제외) 표시 행: "
              f"{int((events['parseNote'] == 'excluded_equivalent').sum())}건.", ""]

    lines += ["## 5. 손계산 대조", "",
              "리드 확인값은 **조건(현금·교환비·인수사)** 이므로 그 세 값을 대조함. R은 조건에서 나오는 "
              "파생값이고, 분모가 엔진이 실제로 들고 있는 mark(종료일 이하 마지막 유효 종가)라 "
              "마지막 호가와 정산 bar 사이 인수사 주가 변동이 그대로 들어감.", ""]
    rows, agree = [], 0
    for tid, symbol, want_cash, want_ratio, want_succ, stated in HAND_CHECKS:
        hit = out[out["tradingItemId"] == tid]
        if not len(hit):
            rows.append([symbol, tid, "(파일에 없음)", "", "", "", "", stated, "X"])
            continue
        row = hit.iloc[0]
        key = int(tid)
        got_cash = None if pd.isna(row["cashPerShare"]) else float(row["cashPerShare"])
        got_ratio = None if pd.isna(row["successorRatio"]) else float(row["successorRatio"])
        succ = row["successorTradingItemId"]
        got_succ = symbols_by_id.get(int(succ)) if succ else None
        same = (close_enough(got_cash, want_cash) and close_enough(got_ratio, want_ratio) and
                (want_succ is None or got_succ == want_succ))
        agree += int(same)
        mark = events.at[key, "lastClose"]
        acq = events.at[key, "acquirerClose"]
        rows.append([symbol, tid, str(row["endDate"].date()), row["actionCode"],
                     f"cash={got_cash if got_cash is not None else '-'}, "
                     f"ratio={got_ratio if got_ratio is not None else '-'}, succ={got_succ or '-'}",
                     f"mark={mark:.4g}" + (f", acq={acq:.4g}" if np.isfinite(acq) else ""),
                     f"{row['settlementReturn']:.4f}" if np.isfinite(row["settlementReturn"]) else "NaN",
                     stated, "O" if same else "X"])
    lines += [table(rows, ["종목", "tradingItemId", "endDate", "code", "파싱 조건", "가격", "R",
                           "리드 확인값", "일치"]), "",
              f"조건 일치 {agree}/{len(HAND_CHECKS)}건.", ""]

    lines += ["## 6. 실패 사례 상위 20건", "",
              "매칭은 됐으나 `settlementReturn`을 만들지 못한 건(보유 마지막 종가 기준 내림차순)."]
    failed = events[(events["matchMethod"] != "none") & ~np.isfinite(pd.to_numeric(events["settlementReturn"],
                                                                                   errors="coerce"))].copy()
    failed["rank"] = pd.to_numeric(failed["lastClose"], errors="coerce").fillna(0)
    failed = failed.sort_values("rank", ascending=False).head(20)
    lines += ["", table([[idx, row.symbol, str(pd.Timestamp(row.endDate).date()), row.actionCode,
                          row.parseKind, "" if not np.isfinite(row.lastClose) else f"{row.lastClose:.4g}",
                          (str(row.actionText) or "")[:90]] for idx, row in failed.iterrows()],
                        ["tradingItemId", "symbol", "endDate", "code", "파싱", "lastClose", "actionText"]), "",
              "대형주 실패는 대부분 벤더 원문이 교환비를 빼먹은 주식 인수(KSU·XLNX·AGN·ALXN·VMW·DATA 등: "
              "`RECEIVED STOCK SHARES OF ...`)임. 이 경우 엔진은 `default_delisting_return`(기본 0)으로 "
              "정산하는데, 인수가 근처에서 마지막 호가가 형성되는 주식 합병에서는 0이 나쁘지 않은 근사임. "
              "정확히 하려면 8-K/S-4 기반 override가 필요함.", ""]

    succ = out[out["successorTradingItemId"].notna()]
    in_cache = succ["successorTradingItemId"].isin(cache_symbols).sum() if cache_symbols else None
    lines += ["### successor 요약", "",
              table([["successor 있는 행", len(succ)],
                     ["  · 주식·혼합 인수(acquirer)", int((succ["actionCode"] != "N/C").sum())],
                     ["  · 개명·재상장 연속(N/C)", int((succ["actionCode"] == "N/C").sum())],
                     ["successor가 가격 캐시에 있음", in_cache if in_cache is not None else "-"],
                     ["successor가 캐시 밖(엔진은 현금 정산으로 되돌림)",
                      (len(succ) - in_cache) if in_cache is not None else "-"]],
                    ["항목", "값"]), ""]

    if universes:
        lines += ["## 7. 유니버스 커버리지", "",
                  f"{args.universe_start} 이후 하루라도 멤버였던 종목 기준."]
        rows = []
        ids = set(out["tradingItemId"])
        with_return = set(out.loc[np.isfinite(out["settlementReturn"]), "tradingItemId"])
        with_match = set(out.loc[out["matchMethod"] != "none", "tradingItemId"])
        for name, members in universes.items():
            inside = members & ids
            rows.append([name, len(members), len(inside),
                         f"{len(members & with_match)} ({len(members & with_match) / max(len(inside),1):.0%})",
                         f"{len(members & with_return)} ({len(members & with_return) / max(len(inside),1):.0%})"])
        lines += ["", table(rows, ["유니버스", "멤버 수", "이벤트 있음", "co_acthist 매칭", "R 산출"]), ""]

    flagged = out[out["reviewFlag"].str.contains("enddate_gap", na=False)].copy()
    rec = int((out["endDateBasis"] == "record_date_match").sum())
    est = int((out["endDateBasis"] == "gap_estimate").sum())
    confirmed = int((out["endDateBasis"] == "confirmed").sum())
    early = out[out["reviewFlag"].str.contains("succ_valued_before_record", na=False)]
    lines += ["## 7-1. 종료일 근거 등급과 갭 처리", "",
              "**xfeed에는 기업행사 효력일 컬럼이 없음**(전 스키마 확인: `eff*` 컬럼은 `ciqestimate*`와 "
              "`sec_idhist`뿐). `co_acthist.datadate`는 **기록일**이므로 날짜가 맞아도 효력일 확인이 "
              "아니라 **추정**임. 그래서 등급을 `endDateBasis`로 분리해 기록함.", "",
              table([["`confirmed`", confirmed,
                      "override — 1차 출처(8-K/DEFM14A)로 확인하고 독립 감사를 통과한 행"],
                     ["`record_date_match`", rec,
                      f"기록일이 마지막 캐시 호가와 ±{args.end_date_tolerance}일 이내 — **추정**(효력일 확인 아님)"],
                     ["`gap_estimate`", est,
                      f"갭 ≤ {args.end_date_tolerance}일 — 통상 결제 지연으로 보고 당김, **추정**"],
                     ["`original`", len(out) - confirmed - rec - est,
                      "원 종료일 유지. 갭 없음 또는 갭 > 5일이면서 근거 없음(`reviewFlag=enddate_gap`)"]],
                    ["endDateBasis", "행수", "의미"]), "",
              f"이 중 미확정으로 남긴 행은 **{len(flagged)}건**임.", ""]
    if len(flagged):
        gaps = flagged["endDateGapDays"]
        lines += [f"flagged 갭 분포: 중앙 {int(gaps.median())}일 · p75 {int(gaps.quantile(.75))}일 · "
                  f"p95 {int(gaps.quantile(.95))}일 · 최대 {int(gaps.max())}일. "
                  "사유 추정은 갭 크기로 갈음함(≤60일 결제·상장폐지 절차 지연, 60~365일 수집 중단 의심, "
                  ">365일 아이템 변경/식별자 재사용 의심).", ""]
        top = flagged.sort_values("endDateGapDays", ascending=False).head(30)
        lines += [table([[row.tradingItemId, symbols_by_id.get(int(row.tradingItemId), ""),
                          str(row.endDate.date()), int(row.endDateGapDays), row.actionCode or "-",
                          ("아이템 변경/식별자 재사용 의심" if row.endDateGapDays > 365 else
                           "수집 중단 의심" if row.endDateGapDays > 60 else "절차 지연"),
                          row.cacheUnitStatus] for row in top.itertuples()],
                        ["tradingItemId", "symbol", "endDate", "갭(일)", "code", "사유 추정", "status"]), ""]
    lines += [f"**승계사 평가일이 기록일보다 앞선 행: {len(early)}건**(`reviewFlag=succ_valued_before_record`). "
              "정산 bar가 벤더 기록일보다 이를 때 생기며 오류는 아니지만, 그 행에서는 기록일이 평가일을 "
              "확인해 주지 못한다는 뜻임.", ""]
    if len(early):
        show = early.sort_values("endDate", ascending=False).head(20)
        lines += [table([[row.tradingItemId, symbols_by_id.get(int(row.tradingItemId), ""),
                          str(row.endDate.date()), str(row.settlementDate.date()),
                          str(row.acquirerPriceDate.date()) if pd.notna(row.acquirerPriceDate) else "-",
                          row.endDateBasis] for row in show.itertuples()],
                        ["tradingItemId", "symbol", "endDate", "기록일", "승계사 평가일", "basis"]), ""]

    lines += ["## 7-2. 캐시 한계 (이벤트 파일로 고칠 수 없는 건)", "",
              "아래는 조건·종료일이 맞아도 **가격 캐시 자체**가 총수익률을 왜곡하는 사례임. "
              "이벤트 파일의 정산 규칙으로는 복구할 수 없고 캐시 재구축(6단계) 과제임.", "",
              table([["12465726 (TFCF.A)", "2019-03-19", "−28.7%",
                      "Fox Corp 분배(스핀오프) 당일 캐시 종가가 69.71 → 49.69로 끊김. "
                      "BaseData에 분배 조정 시계열이 없어 보유자에게 가짜 손실이 계상됨"],
                     ["대상 라인 일반", "종료일 이후", "-",
                      "은퇴한 id에 후속 종목 종가가 이어 붙는 아티팩트(WOLF·TYC). "
                      "종료일 캡과 ktDate 앵커로 정산은 막았으나 그 사이 보유 구간의 가짜 손익은 남음"]],
                    ["종목", "날짜", "크기", "내용"]), ""]

    lines += ["## 8. 출력 스키마·엔진 설정", "",
              "```yaml", "events:", f"  path: {args.out}", "  id_col: tradingItemId",
              "  end_date_col: endDate", "  return_col: settlementReturn",
              "  successor_col: successorTradingItemId        # P2-3",
              "  successor_ratio_col: successorRatio          # P2-3", "```", "",
              "```", "\n".join(f"{n}: {t}" for n, t in
                               zip(EVENT_COLUMNS, ["string", "timestamp[ns]", "timestamp[ns]", "string", "double",
                                                   "double", "double", "string", "double", "string", "string",
                                                   "string", "string", "string"])), "```", "",
              "`source`는 `co_acthist`·`override`·`master`(종료일만 있고 사건 미매칭) 세 값임. "
              "엔진은 `tradingItemId`·`endDate`·`settlementReturn`(+ P2-3에서 successor 두 컬럼)만 읽으므로 "
              "나머지는 감사용임.", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


SELF_TEST_TERMS = [
    ("CHEVRON CORP ACQUIRED HESS CORP.  SHAREHOLDERS OF HESS CORP RECEIVED 1.025 SHARES OF "
     "CHEVRON CORP FOR EVERY SHARE HELD.", None, 1.025, "stock", True),
    ("SYNOPSYS INC ACQUIRED ANSYS INC IN A CASH AND STOCK MERGER.  HOLDERS OF ANSYS INC RECEIVED "
     "$199.91 AND 0.3399 SHARES OF SYNOPSYS INC FOR EVERY SHARE HELD, RESULTING IN A CASH "
     "EQUIVALENT OF $25.814499.", 199.91, 0.3399, "mixed", True),
    ("HOME DEPOT INC ACQUIRED HD SUPPLY HOLDINGS INC FOR $56 PER SHARE.", 56.0, None, "cash", False),
    ("ROGERS COMMUNICATIONS INC ACQUIRED SHAW COMMUNICATIONS INC FOR  CAN $40.5",
     None, None, "foreign_currency", False),
    # a stock leg the vendor names without an exchange ratio: structure is still a fact
    ("ADVANCED MICRO DEVICES INC AQUIRED XILINX INC AND RECEIVED STOCK SHARES OF XILINX INC",
     None, None, "unparsed", True),
    ("ABBVIE INC. ACQUIRED ALLERGAN PLC. RECEIVED CASH AND STOCK SHARES OF ABBV FOR EACH SHARE HELD.",
     None, None, "unparsed", True),
    ("DRE ACQUIRED BY PROGLOGIS INC FOR 0.475 PLD SHARES FOR EVERY DRE SHARE HELD.",
     None, 0.475, "stock", True),
    # a post-split share of the acquirer
    ("DUKE ENERGY CORP ACQUIRED PROGRESS ENERGY INC.  SHAREHOLDERS OF PROGRESS ENERGY INC "
     "RECEIVED 0.87083 OF A POST-SPLIT SHARE OF DUKE ENERGY CORP FOR EACH SHARE HELD.",
     None, 0.87083, "stock", True),
    # the unit sits behind the company name
    ("ENSCO PLC ACQUIRED PRIDE INTERNATIONAL INC.  SHAREHOLDERS OF PRIDE INTERNATIONAL INC "
     "RECEIVED $15.60 AND 0.4778 OF AN ENSCO PLC ADS FOR EACH SHARE HELD.",
     15.6, 0.4778, "mixed", True),
    # a denominator other than one share
    ("ALCOA INC ACQUIRED X.  SHAREHOLDERS OF X RECEIVED ONE SHARE OF ALCOA CORP FOR EVERY "
     "THREE SHARES HELD.", None, 0.33333333, "stock", True),
]


def self_test() -> int:
    """Exercise the parser and the cache-unit resolver without touching the DB or the cache."""
    failures = []
    for text, want_cash, want_ratio, want_kind, want_stock in SELF_TEST_TERMS:
        parsed = parse_terms(text)
        cash = None if not np.isfinite(parsed["cash"]) else round(parsed["cash"], 8)
        ratio = None if not np.isfinite(parsed["ratio"]) else round(parsed["ratio"], 8)
        if (cash, ratio, parsed["kind"], parsed["stockDeclared"]) != \
                (want_cash, want_ratio, want_kind, want_stock):
            failures.append(f"parse_terms: {text[:50]!r} -> {cash}/{ratio}/{parsed['kind']}/"
                            f"{parsed['stockDeclared']}")

    base = dict(has_cash=True, has_stock=True, cash=55.0, ratio=2.0, has_successor=True,
                as_traded_target=50.0, cache_at_kt=100.0, cache_mark=100.0,
                as_traded_successor=30.0, cache_successor=60.0)
    cases = [
        # happy path: both legs anchored, k_t = 0.5, k_s = 0.5
        ("ok", dict(base), dict(status="ok", cash=110.0, ratio=2.0)),
        # review R4: the DB print for the target is missing - k_t must NOT default to 1
        ("no_db_target", dict(base, as_traded_target=float("nan")),
         dict(status="no_db_target", cash=None, ratio=None)),
        # review R4: an override on a non-unit cache basis measures k, it is not forced to 1
        ("override_basis", dict(base, cash=float("nan"), has_cash=False, as_traded_target=25.0),
         dict(status="ok", cash=None, ratio=4.0)),
        # review R4: a rejected path leaves no evaluation input at all
        ("rejected", dict(base, reject_reason="non_usd_listing"),
         dict(status="rejected:non_usd_listing", cash=None, ratio=None)),
        # review R1: a stock leg with no successor id withdraws the cash leg too
        ("no_successor_id", dict(base, has_successor=False),
         dict(status="no_successor_id", cash=None, ratio=None)),
        # review R1: no cache close on the settlement bar - all-or-nothing again
        ("no_cache_successor", dict(base, cache_successor=float("nan")),
         dict(status="no_cache_successor_at_settlement", cash=None, ratio=None)),
        ("no_db_successor", dict(base, as_traded_successor=float("nan")),
         dict(status="no_db_successor", cash=None, ratio=None)),
        ("no_cache_target", dict(base, cache_mark=float("nan"), cache_at_kt=float("nan")),
         dict(status="no_cache_target", cash=None, ratio=None)),
        ("no_terms", dict(base, has_cash=False, has_stock=False),
         dict(status="no_terms", cash=None, ratio=None)),
        # a declared stock leg whose successor is known but whose ratio is not
        ("no_ratio", dict(base, ratio=float("nan")),
         dict(status="no_ratio", cash=None, ratio=None)),
        # a retired line that keeps quoting after the end date on the successor's basis:
        # k_t must come from the end-date bar, so ratioCache collapses to 1.0 (TYC 2016 shape)
        ("kt_measured_at_end_date",
         dict(base, has_cash=False, cash=float("nan"), ratio=0.955, as_traded_target=45.59,
              cache_at_kt=47.738, cache_mark=48.90, as_traded_successor=47.22, cache_successor=47.22),
         dict(status="ok", cash=None, ratio=0.99999539)),  # 0.955 / (45.59/47.738), i.e. 1.0 up to rounding
        # cash-only deal keeps working
        ("cash_only", dict(base, has_stock=False, ratio=float("nan"), has_successor=False),
         dict(status="ok", cash=110.0, ratio=None)),
    ]
    for name, inputs, want in cases:
        got = resolve_cache_units(**inputs)
        cash = None if not np.isfinite(got["cashPerShareCache"]) else round(got["cashPerShareCache"], 8)
        ratio = None if not np.isfinite(got["successorRatioCache"]) else round(got["successorRatioCache"], 8)
        if (got["cacheUnitStatus"], cash, ratio) != (want["status"], want["cash"], want["ratio"]):
            failures.append(f"resolve_cache_units[{name}]: {got['cacheUnitStatus']}/{cash}/{ratio} "
                            f"!= {want['status']}/{want['cash']}/{want['ratio']}")
    good = {"tradingItemId": 2608773, "endDate": "2026-08-05", "actionCode": "ACQ",
            "cashPerShare": 210.0, "successorTradingItemId": None, "successorRatio": None,
            "source": "sec_8k", "confidence": "high", "evidence_url": "https://example.invalid/8k",
            "note": "test", "entered_on": "2026-09-14"}
    override_cases = [
        ("accepted", [good], 0),
        # a required column is absent from the file
        ("missing_column", [{k: v for k, v in good.items() if k != "evidence_url"}], 1),
        # evidence is declared but empty, and the source is not a known kind
        ("blank_evidence", [dict(good, evidence_url="  ", source="")], 2),
        # neither leg of consideration: nothing to evaluate
        ("no_structure", [dict(good, cashPerShare=None, successorRatio=None)], 1),
        ("bad_confidence", [dict(good, confidence="probably")], 1),
        # an election deal may only be entered with the non-election default rule
        ("election_without_rule",
         [dict(good, note="holders could elect either $100 cash or 1.5 shares")], 1),
        ("election_bad_rule",
         [dict(good, election_rule="blended_average",
               note="non-election holders received the prorated mix")], 1),
        ("election_rule_without_citation",
         [dict(good, election_rule="non_election_default", note="stock deal")], 1),
        ("election_explicitly_absent",
         [dict(good, note="fixed cash+ADS mix, no election")], 0),
        # the contractual term "No Election Shares" is an election row, not a denial of one
        ("no_election_shares_term",
         [dict(good, note="no election shares were deemed to have elected cash")], 1),
        ("election_ok",
         [dict(good, election_rule="non_election_default",
               note="DEFM14A: shares for which no election was made were treated as stock "
                    "elections, prorated 61.66%")], 0),
        # a correction must carry both halves of its history
        ("supersedes_without_date", [dict(good, supersedes="cash=6.06 ratio=0.8263")], 1),
        ("supersedes_ok", [dict(good, supersedes="cash=6.06 ratio=0.8263",
                                superseded_on="2026-09-14")], 0),
    ]
    for name, rows, want in override_cases:
        errors = validate_override_frame(pd.DataFrame(rows))
        if len(errors) != want:
            failures.append(f"validate_override_frame[{name}]: {len(errors)} errors != {want} "
                            f"({errors})")

    stamp = pd.Timestamp
    cap_cases = [
        # effective date sits on the last quote: the cap is corroborated even over a long gap
        # the record date sits on the last quote: an estimate, never a confirmation
        ("record_date_match", (stamp("2018-05-22"), stamp("2018-03-06"), stamp("2018-03-06")),
         (stamp("2018-03-06"), "+capped_recorddate_match", 77, "", "record_date_match")),
        # small gap with no effective date: ordinary settlement lag, capped as an estimate
        ("estimate", (stamp("2010-03-01"), stamp("2010-02-26"), pd.NaT),
         (stamp("2010-02-26"), "+capped_estimate", 3, "", "gap_estimate")),
        # long gap with nothing to corroborate it: keep the end date and flag it
        ("flagged", (stamp("2026-07-17"), stamp("2026-04-02"), pd.NaT),
         (stamp("2026-07-17"), "", 106, "enddate_gap", "original")),
        # no gap at all
        ("no_gap", (stamp("2020-01-10"), stamp("2020-01-10"), pd.NaT),
         (stamp("2020-01-10"), "", 0, "", "original")),
    ]
    for name, (end, quote, corroborating), want in cap_cases:
        got = decide_end_date_cap(end, quote, corroborating)
        if got != want:
            failures.append(f"decide_end_date_cap[{name}]: {got} != {want}")

    # the happy path must reproduce the as-traded return it stands for
    got = resolve_cache_units(**base)
    implied = (got["cashPerShareCache"] + got["successorRatioCache"] * base["cache_successor"]) / \
        base["cache_mark"] - 1.0
    expected = (base["cash"] + base["ratio"] * base["as_traded_successor"]) / base["as_traded_target"] - 1.0
    if abs(implied - expected) > 1e-12:
        failures.append(f"identity: {implied} != {expected}")

    for line in failures:
        print("FAIL " + line, file=sys.stderr)
    print(f"self-test: {len(SELF_TEST_TERMS) + len(cases) + len(override_cases) + len(cap_cases) + 1 - len(failures)} passed, "
          f"{len(failures)} failed", file=sys.stderr)
    return 1 if failures else 0


def verify(path: Path, expected_rows: int, closes: "CloseCache" = None) -> None:
    table = pq.read_table(path)
    names = table.schema.names
    assert names == EVENT_COLUMNS, f"schema order mismatch: {names}"
    assert table.num_rows == expected_rows, f"row count mismatch {table.num_rows} != {expected_rows}"
    assert pa.types.is_string(table.schema.field("tradingItemId").type)
    assert pa.types.is_timestamp(table.schema.field("endDate").type)
    assert pa.types.is_string(table.schema.field("successorTradingItemId").type)
    ids = table.column("tradingItemId").to_pylist()
    assert len(set(ids)) == len(ids), "duplicate tradingItemId"
    assert all(i is not None and i != "" for i in ids), "null/empty tradingItemId"
    ends = table.column("endDate").to_pylist()
    assert all(e is not None for e in ends), "null endDate row present"
    assert all(e.hour == 0 and e.minute == 0 and e.second == 0 and e.microsecond == 0 for e in ends), \
        "endDate must be midnight"
    assert pa.types.is_boolean(table.schema.field("endDateConflict").type)
    assert pa.types.is_timestamp(table.schema.field("acquirerPriceDate").type)
    if closes is not None:
        # Settlement bar = first bar after the end date with no valid close (the engine's rule).
        # A quote dated after that bar would be look-ahead.
        frame = table.select(["tradingItemId", "endDate", "acquirerPriceDate"]).to_pandas()
        frame = frame[frame["acquirerPriceDate"].notna()]
        ahead = 0
        for row in frame.itertuples():
            bar, _ = closes.settlement_bar(row.tradingItemId, row.endDate)
            if bar is not pd.NaT and row.acquirerPriceDate > bar:
                ahead += 1
        if ahead:
            raise RuntimeError(f"{ahead} acquirer quotes are dated after their settlement bar")
        print(f"verified look-ahead: 0 of {len(frame)} acquirer quotes after the settlement bar",
              file=sys.stderr)
    frame = table.to_pandas()
    cash_cache = frame["cashPerShareCache"]
    ratio_cache = frame["successorRatioCache"]
    evaluable = cash_cache.notna() | ratio_cache.notna()
    failures = []
    # (1) a declared stock leg that could not be converted withdraws the cash leg too
    broken = frame["hasStockLeg"] & ratio_cache.isna() & cash_cache.notna()
    if broken.any():
        failures.append(f"(1) hasStockLeg with no ratioCache but a cashCache: {int(broken.sum())}")
    # (2) and the mirror image
    broken = frame["hasCashLeg"] & cash_cache.isna() & ratio_cache.notna()
    if broken.any():
        failures.append(f"(2) hasCashLeg with no cashCache but a ratioCache: {int(broken.sum())}")
    # (3) a converted stock leg must carry the settlement-bar guard price and date
    stock = frame[ratio_cache.notna()]
    broken = stock["successorCacheClose"].isna() | stock["successorCacheCloseDate"].isna()
    if broken.any():
        failures.append(f"(3) ratioCache without a successor guard price/date: {int(broken.sum())}")
    if closes is not None and len(stock):
        off_bar = 0
        for row in stock.itertuples():
            bar, _ = closes.settlement_bar(row.tradingItemId, row.endDate)
            if bar is pd.NaT or pd.Timestamp(row.successorCacheCloseDate) != bar:
                off_bar += 1
        if off_bar:
            failures.append(f"(3) successorCacheCloseDate is not the settlement bar: {off_bar}")
    # (4) any evaluation input requires the target guard
    broken = evaluable & (frame["targetCacheMark"].isna() | frame["targetCacheMarkDate"].isna())
    if broken.any():
        failures.append(f"(4) evaluation input without a target guard: {int(broken.sum())}")
    # (5) the converted terms must reproduce the as-traded settlement return
    check = frame[ratio_cache.notna() & frame["targetCacheMark"].notna() &
                  frame["successorCacheClose"].notna() & np.isfinite(frame["settlementReturn"])]
    worst = 0.0
    if len(check):
        implied = ((check["cashPerShareCache"].fillna(0.0) +
                    check["successorRatioCache"] * check["successorCacheClose"]) /
                   check["targetCacheMark"] - 1.0)
        error = (implied - check["settlementReturn"]).abs()
        worst = float(error.max())
        if int((error > 1e-6).sum()):
            failures.append(f"(5) {int((error > 1e-6).sum())} cache-unit terms do not reproduce "
                            f"settlementReturn (max error {worst:.3g})")
    cash_only = frame[cash_cache.notna() & ratio_cache.isna() & frame["targetCacheMark"].notna() &
                      np.isfinite(frame["settlementReturn"])]
    if len(cash_only):
        implied = cash_only["cashPerShareCache"] / cash_only["targetCacheMark"] - 1.0
        error = (implied - cash_only["settlementReturn"]).abs()
        worst = max(worst, float(error.max()))
        if int((error > 1e-6).sum()):
            failures.append(f"(5) {int((error > 1e-6).sum())} cash-only cache terms do not "
                            f"reproduce settlementReturn")
    # (6) the status says exactly whether evaluation inputs exist
    broken = (frame["cacheUnitStatus"] == "ok") != evaluable
    if broken.any():
        failures.append(f"(6) cacheUnitStatus disagrees with the evaluation inputs: {int(broken.sum())}")
    if failures:
        raise RuntimeError("event file invariants violated: " + "; ".join(failures))
    print(f"verified invariants (1)-(6): {int(evaluable.sum())} evaluable rows, "
          f"{len(check)} stock + {len(cash_only)} cash identities, max error {worst:.3g}",
          file=sys.stderr)
    print(f"verified {path}: {table.num_rows} rows, {len(names)} columns, ids unique", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
