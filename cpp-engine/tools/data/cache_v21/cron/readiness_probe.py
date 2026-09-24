#!/home/jgo/usr/venv-3.11/bin/python
"""cache_v21/cron/readiness_probe.py — 일별 갱신 실행 전 준비 상태 프로브 (D4 초안, 읽기 전용).

update_daily.py 의 `--asof auto` 임계(ciq 종가 수 >= 0.9 x 직전 거래일)는 ciq 순번 03 파일 적재(07:00 KST, EDT) 시점에 이미
통과하지만 그 시점의 종가는 잠정치(순번 04 에서 표본의 0.6~11% 정정, 순번 05 에서 거래량 최종)다. 이 프로브는 대상 세션 D
(정규장 마감이 지난 최근 NYSE 거래일, observe_finalization 달력)에 대해 xfl_control(로더 제어, 작은 테이블)만 읽어
"모든 원천이 적재됐는가"를 판정한다. 큰 테이블(ciqpriceequity 등)은 읽지 않는다.

검사(기본 = 전 원천):
  P05  ciqPriceEquity(RecentHistory 로드) 마지막 zip >= PriceEquityRecentHistoryIntradayChange<D>-05.zip (순번 05 적재 = 거래량 최종)
  MI   MIPrice(RecentHist 로드) 적재 종료 >= P05 적재 종료 - 10분 (MI 는 15분 간격 파일, 종가·거래량 최종은 순번 05 이전)
  SPL  ciqSplitCache 마지막 zip SplitsV2Change<label> 의 label >= D 의 다음 달력일 (00:10 ET 생성 -> 13:10 KST(EDT)/14:10(EST) 도착, 14:00/15:00 적재)
  DIV  ciqDividend 마지막 zip DividendsV2Change<label> 의 label >= D 의 다음 달력일 (02:10 ET -> 15:10/16:10 KST 도착, 16:00/17:00 적재)
  IDC  (--require-idc 일 때만) ciqTradingItem 마지막 zip IDCSecurityChange<label> 의 label >= D 의 다음 달력일 (17:40 KST 도착, 18:00 적재)
  --early: P05 + MI + SPL 만(가격·계수 확정 우선, 배당은 전날 파일).

출력: KEY=VALUE 행(SESSION, PREV_ASOF, ACTION, READY, 각 검사 CHECK_<name>=ok|wait|error 와 상세) — 셸 래퍼가 파싱.
종료코드: 0 준비 완료(실행) / 3 미준비(재시도) / 4 할 일 없음(세션 <= 이전 빌드 asOf) / 1 오류(DB 접속 등).
"""
from __future__ import annotations
import os
os.environ.setdefault("OMP_NUM_THREADS", "1")
import argparse, datetime as dt, json, re, sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import observe_finalization as obsf  # noqa: E402  (NYSE 달력·마감 시각, 하드코딩 휴장일)

KST = ZoneInfo("Asia/Seoul"); ET = ZoneInfo("America/New_York"); UTC = dt.timezone.utc
RC_READY, RC_ERROR, RC_WAIT, RC_NOTHING = 0, 1, 3, 4


def prev_asof(prev_build: Path | None) -> dt.date | None:
    """이전 빌드 asOf: BUILD.json(stage) → manifest.json(published) → _work/asof.json → T2_work/asof.json."""
    if prev_build is None:
        return None
    for rel, key in (("BUILD.json", "asOf"), ("manifest.json", "asOfDate"), ("_work/asof.json", "asOf"), ("T2_work/asof.json", "asOf")):
        p = prev_build / rel
        if p.exists():
            try:
                v = json.loads(p.read_text()).get(key)
            except Exception:
                v = None
            if v:
                return dt.date.fromisoformat(str(v)[:10])
    return None


def load_states(conn):
    """xfl_control 스냅샷: 관심 테이블의 로드 상태(마지막 zip 이름·다운로드 KST·적재 종료 UTC·결과 코드)."""
    sql = """select t.tablename, s.loadsubid, s.enddatetime, s.lastresulttypeid, z.actualzipfilename as lastzip, z.lastdownloadosdatetime as lastzip_dl
             from xfl_control.xfu_loadsubtablestate s
             join xfl_control.xfc_tableinst ti using(tableinstid) join xfl_control.xfc_table t using(tableid)
             left join xfl_control.xfu_actualzipfile z on z.actualzipfileid = s.lastactualzipfileid
             where t.tablename in ('ciqPriceEquity','MIPrice','ciqSplitCache','ciqDividend','ciqTradingItem') and s.enddatetime is not null"""
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    return rows


def pick(rows, table, zip_prefix):
    """테이블의 로드 행 중 마지막 zip 이 zip_prefix 로 시작하는 것(RecentHistory 등) 가운데 적재 종료가 가장 늦은 행."""
    c = [r for r in rows if r["tablename"] == table and (r["lastzip"] or "").startswith(zip_prefix)]
    return max(c, key=lambda r: r["enddatetime"]) if c else None


def label_date(name: str, pat: str) -> dt.date | None:
    m = re.search(pat, name or "")
    return dt.date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8])) if m else None


def fmt_kst(t):
    if t is None:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prev-build", type=Path, default=None, help="이전 빌드 디렉토리(stage 또는 published): asOf 비교로 '할 일 없음' 판정")
    ap.add_argument("--session", default=None, help="대상 세션 YYYY-MM-DD (기본: 정규장 마감이 지난 최근 NYSE 거래일)")
    ap.add_argument("--now", default=None, help="현재 시각 강제(ISO, tz 없으면 KST; 테스트용)")
    ap.add_argument("--early", action="store_true", help="가격·계수만(P05+MI+SPL); 배당 파일은 기다리지 않음")
    ap.add_argument("--require-idc", action="store_true", help="IDCSecurityChange(식별자·상태, 18:00 KST 적재)까지 요구")
    ap.add_argument("--no-db", action="store_true", help="달력·asOf 판정만(테스트)")
    ap.add_argument("--timeout-ms", type=int, default=20000)
    a = ap.parse_args(argv)

    now = dt.datetime.fromisoformat(a.now) if a.now else dt.datetime.now(tz=KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    t0, t_1, _, t_next = obsf.target_days(now.astimezone(ET))
    session = dt.date.fromisoformat(a.session) if a.session else t0
    out = {"NOW_KST": now.astimezone(KST).strftime("%Y-%m-%d %H:%M"), "SESSION": str(session), "LATEST_CLOSED": str(t0), "NEXT_SESSION": str(t_next),
           "SESSION_CLOSE_KST": obsf.close_dt(session).astimezone(KST).strftime("%Y-%m-%d %H:%M"), "EARLY": int(a.early), "REQUIRE_IDC": int(a.require_idc)}
    pa = prev_asof(a.prev_build)
    out["PREV_ASOF"] = str(pa) if pa else ""
    if not obsf.is_trading_day(session):
        out["ACTION"], out["READY"], out["REASON"] = "nothing", 0, f"{session} is not a NYSE trading day"
        emit(out); return RC_NOTHING
    if pa is not None and session <= pa:
        out["ACTION"], out["READY"], out["REASON"] = "nothing", 0, f"session {session} <= previous build asOf {pa}"
        emit(out); return RC_NOTHING
    if a.no_db:
        out["ACTION"], out["READY"], out["REASON"] = "run", 1, "no-db mode: calendar/asOf only"
        emit(out); return RC_READY

    try:
        from db import connect
        conn = connect(statement_timeout_ms=a.timeout_ms, application_name="cache_v21_readiness_probe")
        rows = load_states(conn); conn.close()
    except Exception as exc:  # noqa: BLE001
        out["ACTION"], out["READY"], out["REASON"] = "error", 0, f"db: {type(exc).__name__}: {exc}"
        emit(out); return RC_ERROR

    day_after = session + dt.timedelta(days=1)          # 벤더 일간 파일 라벨(ET 날짜) 은 세션 다음 달력일
    checks = {}
    # P05: ciq RecentHistory 순번 05
    p = pick(rows, "ciqPriceEquity", "PriceEquityRecentHistoryIntradayChange")
    need = f"PriceEquityRecentHistoryIntradayChange{session:%Y%m%d}-05.zip"
    if p is None:
        checks["P05"] = ("error", "no ciqPriceEquity RecentHistory load state")
    else:
        lab = label_date(p["lastzip"], r"Change(\d{8})-"); seq = int(re.search(r"-(\d+)\.zip$", p["lastzip"]).group(1))
        ok = (lab, seq) >= (session, 5)
        checks["P05"] = ("ok" if ok else "wait", f"lastzip={p['lastzip']} loaded={fmt_kst(p['enddatetime'])} result={p['lastresulttypeid']} need>={need}")
    # MI: RecentHist 적재가 P05 적재보다 늦거나 같음(10분 허용)
    m = pick(rows, "MIPrice", "MIPriceRecentHistChange")
    if m is None:
        checks["MI"] = ("error", "no MIPrice RecentHist load state")
    elif p is None or checks["P05"][0] != "ok":
        checks["MI"] = ("wait", f"lastzip={m['lastzip']} loaded={fmt_kst(m['enddatetime'])} (P05 first)")
    else:
        ok = m["enddatetime"] >= p["enddatetime"] - dt.timedelta(minutes=10)
        checks["MI"] = ("ok" if ok else "wait", f"lastzip={m['lastzip']} loaded={fmt_kst(m['enddatetime'])} vs P05 loaded={fmt_kst(p['enddatetime'])}")
    # SPL / DIV / IDC: 일간 파일 라벨 >= 세션 다음 달력일
    for name, table, prefix, required in (("SPL", "ciqSplitCache", "SplitsV2Change", True), ("DIV", "ciqDividend", "DividendsV2Change", not a.early),
                                          ("IDC", "ciqTradingItem", "IDCSecurityChange", a.require_idc)):
        r = pick(rows, table, prefix)
        if r is None:
            checks[name] = ("error" if required else "skip", f"no {table} load state")
            continue
        lab = label_date(r["lastzip"], r"Change(\d{8})")
        ok = lab is not None and lab >= day_after
        checks[name] = (("ok" if ok else "wait") if required else "skip", f"lastzip={r['lastzip']} loaded={fmt_kst(r['enddatetime'])} result={r['lastresulttypeid']} need_label>={day_after}")
    for k, (st, detail) in checks.items():
        out[f"CHECK_{k}"] = st; out[f"DETAIL_{k}"] = detail
    states = [st for st, _ in checks.values() if st != "skip"]
    if "error" in states:
        out["ACTION"], out["READY"], out["REASON"] = "error", 0, "load state missing: " + ",".join(k for k, (st, _) in checks.items() if st == "error")
        emit(out); return RC_ERROR
    if "wait" in states:
        out["ACTION"], out["READY"], out["REASON"] = "wait", 0, "not loaded yet: " + ",".join(k for k, (st, _) in checks.items() if st == "wait")
        emit(out); return RC_WAIT
    out["ACTION"], out["READY"], out["REASON"] = "run", 1, "all required sources loaded"
    emit(out); return RC_READY


def emit(out: dict):
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    sys.exit(main())
