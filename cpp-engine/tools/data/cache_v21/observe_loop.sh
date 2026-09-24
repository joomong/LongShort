#!/bin/bash
# T3 agent-observe — observe_finalization.py 를 매시 05분에 1회 실행하는 루프.
# 시작(백그라운드):  nohup setsid bash observe_loop.sh > /dev/null 2>&1 &
# 종료 조건: 시작 시각의 T0 이후 N_DAYS(기본 10) 미국 거래일이 지나고 그 날 정규장 마감 +24h 가 지나면 스스로 종료.
# 수동 종료:  kill $(cat $STAGE/observe.pid)   ← 자기 PID만. 광역 pkill 금지.
# 로그: $STAGE/observe.log, PID: $STAGE/observe.pid, 시작 시각: $STAGE/observe.start
# 관측 1회 실패(DB 타임아웃 등)는 로그만 남기고 다음 정각+05분에 재시도한다(루프는 죽지 않음).
set -u
STAGE=${STAGE:-/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad/stage_v21/T3}
PY=/home/jgo/usr/venv-3.11/bin/python
SCRIPT=/home/jgo/longshort_platform/cpp-engine/tools/data/cache_v21/observe_finalization.py
N_DAYS=${N_DAYS:-10}
MINUTE=${MINUTE:-5}            # 매시 몇 분에 실행(로더 적재가 매시 00분~03분 KST → 05분)
OBS_TIMEOUT=${OBS_TIMEOUT:-1500}  # 관측 1회 상한(초); 내부 쿼리는 statement_timeout 5분
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2

mkdir -p "$STAGE/qa/finalization"
LOG="$STAGE/observe.log"; PIDF="$STAGE/observe.pid"; STARTF="$STAGE/observe.start"
echo $$ > "$PIDF"
if [ -s "$STARTF" ]; then START=$(cat "$STARTF"); else START=$(date +%Y-%m-%dT%H:%M:%S+09:00); echo "$START" > "$STARTF"; fi
SLEEP_PID=
cleanup() { [ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null; rm -f "$PIDF"; echo "[$(date '+%F %T')] loop exit (pid $$)" >> "$LOG"; }
trap 'exit 0' TERM INT     # exit → EXIT 트랩에서 cleanup 1회
trap 'cleanup' EXIT
echo "[$(date '+%F %T')] loop start pid=$$ start=$START n_days=$N_DAYS minute=$MINUTE" >> "$LOG"
$PY "$SCRIPT" --done-after "$START" "$N_DAYS" >> "$LOG" 2>&1 || true

while true; do
    now=$(date +%s)
    next=$(date -d "$(date +%Y-%m-%d\ %H):$(printf %02d $MINUTE):00" +%s)
    if [ "$next" -le "$now" ]; then next=$((next + 3600)); fi
    sleep $((next - now)) & SLEEP_PID=$!; wait "$SLEEP_PID"; SLEEP_PID=   # 백그라운드 sleep + wait: TERM/INT 트랩이 즉시 실행됨(포그라운드 sleep은 끝날 때까지 트랩 지연)
    echo "[$(date '+%F %T')] observe start" >> "$LOG"
    if timeout "$OBS_TIMEOUT" $PY "$SCRIPT" --once >> "$LOG" 2>&1; then
        echo "[$(date '+%F %T')] observe ok" >> "$LOG"
    else
        echo "[$(date '+%F %T')] observe FAILED rc=$? (retry next hour)" >> "$LOG"
    fi
    done=$($PY "$SCRIPT" --done-after "$START" "$N_DAYS" 2>>"$LOG" || echo 0)
    if [ "$done" = "1" ]; then
        echo "[$(date '+%F %T')] $N_DAYS trading days elapsed since $START — done" >> "$LOG"
        break
    fi
done
