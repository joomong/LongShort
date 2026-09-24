#!/bin/bash
# cache_v21/cron/update_daily.sh — cache_v2.1 일별 갱신 크론 래퍼 (D4 초안 2026-09-21; 크론 미등록, 리드/사용자 확인 후 등록)
#
# 한 번 호출 = 한 번의 "시도". 크론이 매시(14:10~19:10 KST, 화~토) 호출하고, 래퍼는
#   (1) 락(flock)으로 중복 실행을 막고
#   (2) readiness_probe.py 로 대상 세션 D(정규장 마감이 지난 최근 NYSE 거래일)와 원천 적재 완료를 확인한 뒤
#   (3) update_daily.py --asof D 를 실행하고 (이전 빌드 = $ROOT/current, 출력 = $ROOT/builds/<id>, 발행 = $ROOT/published/<id> + 61)
#   (4) 종료코드별로 current 갱신 / 재시도 / 경보 플래그를 남긴다.
# 같은 세션 D 가 이미 성공했으면(state/done_<D>) 아무것도 하지 않는다(멱등). 실패 빌드 디렉토리는 진단용으로 남긴다.
#
# 종료코드(래퍼): 0 성공 또는 할 일 없음(이미 완료·휴장·락 점유) / 3 미준비(다음 시도) / 그 외 = update_daily 종료코드 그대로
#   (1 예외, 4 임계·행 소멸 퓨즈, 5 raw 불변·구간·원장 stale, 6 events 없음, 7 게이트, 8 publish) / 10 61 마운트 없음 / 11 프로브 오류
# update_daily 가 D2 에서 추가하는 "보유 종목 미확정 경고(비차단)" 종료코드는 EXPOSURE_WARN_RC(기본 9) 로 받아 성공으로 처리하되 경보를 남긴다.
#
# 사용:  update_daily.sh [--early] [--full-lifecycle] [--dry-run] [update_daily.py 추가 인자...]
#   --early          가격·계수만 확정되면 실행(배당 파일 미대기; 프로브 --early)
#   --require-idc    식별자 파일(18:00 KST 적재)까지 기다림
#   --last-attempt   이 시도에서도 미준비면 경보(크론의 마지막 슬롯에서 사용; 자동: 시각 >= MAX_ATTEMPT_HOUR)
# 환경변수(기본값): PY CV21 ROOT REMOTE ORCH THREADS TIMEOUT MAX_ATTEMPT_HOUR EXPOSURE_WARN_RC ALERT_CMD KEEP_LOG_DAYS
set -u
PY=${PY:-/home/jgo/usr/venv-3.11/bin/python}
CV21=${CV21:-/home/jgo/longshort_platform/cpp-engine/tools/data/cache_v21}
ROOT=${ROOT:-/home/jgo/cache_v21_builds}                     # 로컬 빌드 체인(/home, 여유 큼; / 는 1.4GB 남음 — /tmp 사용 금지)
REMOTE=${REMOTE:-/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d}
MOUNT=${MOUNT:-/home/jgo/61/data2}
ORCH=${ORCH:-$ROOT/orch}                                     # publish 가 qa/ 로 복사하는 브리프·보고서(내구 경로; 세션 스크래치패드 아님)
THREADS=${THREADS:-8}                                        # 공유 머신: 스레드 <= 8
TIMEOUT=${TIMEOUT:-3h}                                       # update_daily 한 번의 상한(실측 12~15분 + 원장 증분)
MAX_ATTEMPT_HOUR=${MAX_ATTEMPT_HOUR:-19}                     # 이 시각(KST) 이상의 시도 = 마지막 시도(미준비도 경보)
EXPOSURE_WARN_RC=${EXPOSURE_WARN_RC:-9}                      # [D2 확정 필요] 보유 종목 미확정 경고(비차단) 종료코드
ALERT_CMD=${ALERT_CMD:-}                                     # 예: "mail -s" 래퍼·webhook 스크립트 (인자: 제목; 본문은 stdin). 비어 있으면 플래그 파일만
KEEP_LOG_DAYS=${KEEP_LOG_DAYS:-90}
export OMP_NUM_THREADS=$THREADS OPENBLAS_NUM_THREADS=$THREADS MKL_NUM_THREADS=$THREADS NUMEXPR_NUM_THREADS=$THREADS POLARS_MAX_THREADS=$THREADS

PROBE_ARGS=(); EXTRA=(); LAST_ATTEMPT=0; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --early|--require-idc) PROBE_ARGS+=("$1");;
        --last-attempt) LAST_ATTEMPT=1;;
        --dry-run) DRY=1; EXTRA+=("$1");;
        *) EXTRA+=("$1");;
    esac
    shift
done
mkdir -p "$ROOT"/{builds,published,logs,alerts,state,orch/reports}
LOG=$ROOT/logs/cron.log
STAMP() { date '+%F %T'; }
log() { echo "[$(STAMP)] $*" >> "$LOG"; }
hour=$(date +%H); [ "${hour#0}" -ge "$MAX_ATTEMPT_HOUR" ] && LAST_ATTEMPT=1

alert() {   # alert <code> <subject> [body-file]
    local code=$1 subject=$2 body=${3:-}
    local f="$ROOT/alerts/$(date +%Y%m%d_%H%M)_rc${code}.flag"
    { echo "subject: $subject"; echo "time: $(STAMP)"; echo "code: $code"; [ -n "$body" ] && [ -f "$body" ] && { echo "---"; tail -n 60 "$body"; }; } > "$f"
    ln -sfn "$f" "$ROOT/alerts/LATEST"
    log "ALERT rc=$code $subject -> $f"
    if [ -n "$ALERT_CMD" ]; then $ALERT_CMD "[cache_v21] $subject" < "$f" >> "$LOG" 2>&1 || log "alert command failed"; fi
}

# ---- (1) 락: 다른 시도가 돌고 있으면 조용히 종료(크론 다음 슬롯이 이어받음)
exec 9> "$ROOT/.lock"
if ! flock -n 9; then log "skip: another attempt holds $ROOT/.lock (pid $(cat "$ROOT/.lock.pid" 2>/dev/null))"; exit 0; fi
echo $$ > "$ROOT/.lock.pid"
trap 'rm -f "$ROOT/.lock.pid"' EXIT

# ---- (2) 전제: 61 마운트, 이전 빌드
if ! mountpoint -q "$MOUNT"; then
    log "61 mount missing: $MOUNT"; [ $LAST_ATTEMPT = 1 ] && alert 10 "61 mount missing ($MOUNT) — no build today"; exit 10
fi
if [ ! -e "$ROOT/current" ]; then
    log "no $ROOT/current (bootstrap: ln -s <stage build dir or published copy> $ROOT/current)"; alert 1 "bootstrap missing: $ROOT/current"; exit 1
fi
PREV=$(readlink -f "$ROOT/current")

# ---- (3) 준비 프로브(읽기 전용 xfl_control) → SESSION / ACTION
PROBE_OUT=$("$PY" "$CV21/cron/readiness_probe.py" --prev-build "$PREV" "${PROBE_ARGS[@]}" 2>> "$LOG"); prc=$?
SESSION=$(echo "$PROBE_OUT" | sed -n 's/^SESSION=//p'); REASON=$(echo "$PROBE_OUT" | sed -n 's/^REASON=//p')
case $prc in
    0) log "probe ready: session=$SESSION ($REASON)";;
    3) log "probe wait: session=$SESSION ($REASON)"; echo "$PROBE_OUT" | grep '^DETAIL_' >> "$LOG"
       [ $LAST_ATTEMPT = 1 ] && { echo "$PROBE_OUT" > "$ROOT/state/probe_last.txt"; alert 3 "session $SESSION not ready by last attempt ($REASON)" "$ROOT/state/probe_last.txt"; }
       exit 3;;
    4) log "nothing to do: $REASON"; exit 0;;
    *) log "probe error rc=$prc: $REASON"; echo "$PROBE_OUT" > "$ROOT/state/probe_last.txt"; alert 11 "readiness probe error ($REASON)" "$ROOT/state/probe_last.txt"; exit 11;;
esac
if [ -f "$ROOT/state/done_$SESSION" ]; then log "already built session $SESSION ($(cat "$ROOT/state/done_$SESSION"))"; exit 0; fi
# 같은 세션의 이전 시도가 재시도 불가 코드로 실패했으면 반복하지 않는다(경보는 이미 남음; 사람이 state/failed_<D> 를 지우면 재시도)
if [ -f "$ROOT/state/failed_$SESSION" ]; then log "session $SESSION failed earlier with a non-retryable code ($(cat "$ROOT/state/failed_$SESSION")); rm state/failed_$SESSION to retry"; exit 0; fi
n_try=$(( $(cat "$ROOT/state/tries_$SESSION" 2>/dev/null || echo 0) + 1 )); echo $n_try > "$ROOT/state/tries_$SESSION"
if [ "$n_try" -gt 3 ]; then log "session $SESSION: 3 full attempts used — stop (rm state/tries_$SESSION to retry)"; alert 1 "session $SESSION: 3 attempts failed"; exit 1; fi

# ---- (4) 실행
ID=build_${SESSION}_$(date +%H%M)
OUT=$ROOT/builds/$ID; PUB=$ROOT/published/$ID; RLOG=$ROOT/logs/update_$ID.log
FULL=(); [ "$(date +%u)" = 6 ] && FULL=(--full-lifecycle)          # 토요일(KST) = 금요일 세션: 전 종목 lifecycle 재집계 + 음성 후보 재검사
log "run $ID: prev=$PREV asof=$SESSION try=$n_try threads=$THREADS extra=(${EXTRA[*]:-}) ${FULL[*]:-}"
timeout --signal=TERM --kill-after=120 "$TIMEOUT" nice -n 10 ionice -c2 -n7 \
    "$PY" "$CV21/update_daily.py" --prev-build "$PREV" --out-stage "$OUT" --asof "$SESSION" \
        --publish-out "$PUB" --remote "$REMOTE" --threads "$THREADS" --orch "$ORCH" "${FULL[@]}" "${EXTRA[@]}" > "$RLOG" 2>&1
rc=$?
log "update_daily rc=$rc ($ID) log=$RLOG"
if [ $DRY = 1 ]; then log "dry-run: current/state untouched (out=$OUT)"; rm -f "$ROOT/state/tries_$SESSION"; exit $rc; fi

# ---- (5) 종료코드별 조치
finish_ok() {
    ln -sfn "$OUT" "$ROOT/current"
    echo "$ID rc=$rc $(STAMP)" > "$ROOT/state/done_$SESSION"; rm -f "$ROOT/state/tries_$SESSION"
    printf '{"build": "%s", "asOf": "%s", "out": "%s", "published": "%s", "rc": %d, "finished": "%s"}\n' "$ID" "$SESSION" "$OUT" "$PUB" "$rc" "$(STAMP)" > "$ROOT/state/last_success.json"
    # 발행 확인: 61 manifest 의 asOfDate == 세션
    remote_asof=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('asOfDate',''))" "$REMOTE/manifest.json" 2>/dev/null || echo "")
    if [ "$remote_asof" != "$SESSION" ]; then alert 8 "published manifest asOfDate '$remote_asof' != session $SESSION (check $REMOTE, .incoming-*, .prev-*)"; fi
    expo=$(find "$OUT" -maxdepth 3 -name exposure_daily.md 2>/dev/null | head -1); [ -n "$expo" ] && cp "$expo" "$ROOT/state/exposure_$SESSION.md"
    "$CV21/cron/prune_builds.sh" --apply-local >> "$LOG" 2>&1 || log "prune failed (non-fatal)"
    find "$ROOT/logs" -name 'update_*.log' -mtime +"$KEEP_LOG_DAYS" -delete 2>/dev/null
}
case $rc in
    0)  log "OK $ID -> current"; finish_ok; exit 0;;
    "$EXPOSURE_WARN_RC")
        log "OK with exposure warning $ID -> current"; finish_ok
        alert "$rc" "session $SESSION published; held names with unconfirmed P&L (see qa/exposure_daily.md)" "$ROOT/state/exposure_$SESSION.md"; exit 0;;
    3)  log "not complete yet (vendor late) — retry next slot"; [ $LAST_ATTEMPT = 1 ] && alert 3 "session $SESSION: data not complete by last attempt" "$RLOG"; exit 3;;
    4)  log "close-count threshold / removed-rows fuse — retry next slot once, then human"; 
        if [ $LAST_ATTEMPT = 1 ] || [ "$n_try" -ge 2 ]; then echo "rc=4 $ID" > "$ROOT/state/failed_$SESSION"; alert 4 "session $SESSION: asOf threshold or removed-rows fuse (BUILD.json.failed, asof_pick.checks)" "$RLOG"; fi; exit 4;;
    5)  echo "rc=5 $ID" > "$ROOT/state/failed_$SESSION"; alert 5 "session $SESSION: raw not invariant / interval check / ledger stale — check D1 ledger increment (ca_ledger_build_info.json asOf), T2/qa/raw_invariance_violations.csv" "$RLOG"; exit 5;;
    6)  echo "rc=6 $ID" > "$ROOT/state/failed_$SESSION"; alert 6 "session $SESSION: events file missing" "$RLOG"; exit 6;;
    7)  echo "rc=7 $ID" > "$ROOT/state/failed_$SESSION"; alert 7 "session $SESSION: blocking gate failed (T2/qa/update_gates.md) — human review" "$RLOG"; exit 7;;
    8)  log "publish failed — local build stays at $OUT; check $REMOTE.incoming-* and $REMOTE (two renames), rerun publish only"
        if [ $LAST_ATTEMPT = 1 ] || [ "$n_try" -ge 2 ]; then echo "rc=8 $ID" > "$ROOT/state/failed_$SESSION"; fi
        alert 8 "session $SESSION: publish failed (rsync/sha256/rename) — inspect $REMOTE.incoming-*" "$OUT/subprocess.log"; exit 8;;
    124|137) echo "rc=$rc $ID" > "$ROOT/state/failed_$SESSION"; alert "$rc" "session $SESSION: update_daily timed out ($TIMEOUT) — check $REMOTE.incoming-* / .prev-* consistency" "$RLOG"; exit "$rc";;
    *)  echo "rc=$rc $ID" > "$ROOT/state/failed_$SESSION"; alert "$rc" "session $SESSION: update_daily exception rc=$rc" "$RLOG"; exit "$rc";;
esac
