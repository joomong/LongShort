#!/bin/bash
# cache_v21/rc2f_build.sh - rc2f X0 재생성 드라이버 (orch_rc2f/BRIEF.md §4 X0)
#   apply(교정 CSV → T2/R1/R3 스테이지 + ledger_build --overrides) → publish 드라이런(returns v2 + loader.export_ca_ledger; --remote 없음, 발행 아님)
#   → ledger_qa.py → returns_v2_qa.py → rc2f_recount.py(재집계 + 귀속 게이트)
# 사용: rc2f_build.sh [--out <stage_rc2f/build>] [--overrides-glob '<glob>'] [--skip-apply] [--skip-qa] [--rc2-rules]
#   --rc2-rules: rc2 규칙 그대로(ledger_build --rc2-rules, publish/QA --legacy-links, X3 (d) events 패치 생략) → 빈 교정 = rc2 비트 동일 게이트 (a)
#   기본 out = SP/stage_rc2f/build, 기본 glob = SP/stage_rc2f/X*/overrides/*.csv. 로그 <out>/logs/*.log. 어느 단계든 실패하면 exit 1.
set -u
PY=/home/jgo/usr/venv-3.11/bin/python
D=$(cd "$(dirname "$0")" && pwd)
SP=${RC2F_SP:-/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad}
export RC2F_SP=$SP
RC2=$SP/build_rc2_final
STAGE_RC2=$SP/stage_rc2
STAGE_V21=$SP/stage_v21
OUT=$SP/stage_rc2f/build
GLOB="$D/overrides/rc2f/X*/*.csv"          # repo copy of the rc2f overrides (scratchpad originals: $SP/stage_rc2f/X*/overrides/*.csv)
SKIP_APPLY=0; SKIP_QA=0; BUILD_ID=rc2f-stage; RC2_RULES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT=$2; shift 2;;
    --overrides-glob) GLOB=$2; shift 2;;
    --rc2) RC2=$2; shift 2;;
    --stage-rc2) STAGE_RC2=$2; shift 2;;
    --stage-v21) STAGE_V21=$2; shift 2;;
    --build-id) BUILD_ID=$2; shift 2;;
    --skip-apply) SKIP_APPLY=1; shift;;
    --skip-qa) SKIP_QA=1; shift;;
    --rc2-rules) RC2_RULES=1; shift;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
mkdir -p "$OUT/logs"
LOG=$OUT/logs/rc2f_build.log
step() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
run() { # run <name> <cmd...>
  local name=$1; shift
  step "START $name: $*"
  "$@" > "$OUT/logs/$name.log" 2>&1
  local rc=$?
  if [ $rc -ne 0 ]; then step "FAIL $name (exit $rc) — see $OUT/logs/$name.log"; tail -20 "$OUT/logs/$name.log"; exit 1; fi
  step "OK $name"
}

RULE_APPLY=""; RULE_PUB=""
if [ $RC2_RULES -eq 1 ]; then RULE_APPLY="--rc2-rules"; RULE_PUB="--legacy-links"; fi
if [ $SKIP_APPLY -eq 0 ]; then
  run apply $PY "$D/rc2f_apply.py" apply --rc2 "$RC2" --stage-rc2 "$STAGE_RC2" --stage-v21 "$STAGE_V21" --overrides-glob "$GLOB" --out "$OUT" --force $RULE_APPLY
else
  # R-X0 P2-5: --skip-apply reuses an existing apply stage — it must be complete (rc2f_apply_info.json is written last, after the apply log gate)
  # and built under the same rules (rc2_rules) as this run
  run check_apply $PY - "$OUT" "$RC2_RULES" <<'PYEOF'
import json, sys
from pathlib import Path
out, rules = Path(sys.argv[1]), sys.argv[2] == "1"
p = out / "rc2f_apply_info.json"
if not p.exists():
    sys.exit(f"--skip-apply: {p} missing (apply did not finish); run without --skip-apply")
info = json.loads(p.read_text())
if info.get("apply_log", {}).get("override_ids_missing_from_log"):
    sys.exit(f"--skip-apply: apply log gate failed: {info['apply_log']['override_ids_missing_from_log']}")
if bool(info.get("rc2_rules", False)) != rules:
    sys.exit(f"--skip-apply: stage was applied with rc2_rules={info.get('rc2_rules')} but this run has --rc2-rules={rules}")
for rel in ("T2/base_data.parquet", "R1/events/ca_ledger.parquet", "rc2f_apply_log.csv"):
    if not (out / rel).exists():
        sys.exit(f"--skip-apply: {out / rel} missing")
print(f"apply stage ok: {info.get('at')} rc2_rules={info.get('rc2_rules')} overrides={info.get('apply_log', {}).get('rows')}")
PYEOF
fi
# publish 드라이런 = prices_returns v2 + mask + loader.export_ca_ledger (rc2 와 같은 코드 경로; 61 발행은 리드가 --remote 로)
rm -rf "$OUT/cache"
run publish $PY "$D/publish.py" --stage "$STAGE_V21" --t2 "$OUT/T2" --rc2-stage "$OUT" --orch "$SP/orch_v21" --orch-rc2 "$SP/orch_rc2" --out "$OUT/cache" --build-id "$BUILD_ID" --no-proto-tables --keep-unmasked-returns "$OUT/R2/derived/returns_total.parquet" $RULE_PUB
# 드라이런 cache 에는 proto 표(dividends/, distributions/)가 없음(--no-proto-tables) → returns_v2_qa 의 gap 목록용으로 rc1 사본을 링크(발행본은 리드가 proto 표 포함 발행)
[ -e "$OUT/cache/dividends" ] || ln -s "$STAGE_RC2/R1/rc1_copy/dividends" "$OUT/cache/dividends"
[ -e "$OUT/cache/distributions" ] || ln -s "$STAGE_RC2/R1/rc1_copy/distributions" "$OUT/cache/distributions"
# ledger_qa Q11~Q13 은 <ledger dir>/../engine/ 의 export 를 읽음 → publish 가 만든 export 를 R1/engine 에 복사(rc2 R1 레이아웃과 동일)
mkdir -p "$OUT/R1/engine" && cp "$OUT/cache/engine/ca_ledger_cache.parquet" "$OUT/cache/engine/ca_ledger_cache_manifest.json" "$OUT/R1/engine/"
if [ $SKIP_QA -eq 0 ]; then
  run ledger_qa $PY "$D/ledger_qa.py" --ledger "$OUT/R1/events/ca_ledger.parquet" --root "$OUT/R1/root" --out "$OUT/R1/qa" --base-data "$OUT/T2/base_data.parquet"
  # returns_v2_qa: 교정된 base_data/factors(cache) + 마스킹 전 v2(R2/derived) + rc1 벤더식 수익률(G-VENDOR 기준) — rc2 V2 와 같은 게이트
  # vendor reference for G-SPLIT/G-VENDOR = rc2 original factors (before split overrides); applied split overrides classify their boundary cells (R-X0 2차 P1-7)
  run returns_v2_qa $PY "$D/returns_v2_qa.py" --build "$OUT/cache" --base-data "$OUT/cache/base_data.parquet" --rc1 "$STAGE_RC2/R2/rc1/derived/returns_total.parquet" --v2 "$OUT/R2/derived/returns_total.parquet" --ledger "$OUT/cache/events/ca_ledger.parquet" --lineage "$OUT/cache/master/lineage.parquet" --out "$OUT/R2/qa/returns_v2" --vendor-factors "$RC2/factors/split.parquet" --rc2f-stage "$OUT" $RULE_PUB
fi
run recount $PY "$D/rc2f_recount.py" --rc2 "$RC2" --build "$OUT/cache" --stage "$OUT" --out "$OUT/recount" $( [ $RC2_RULES -eq 1 ] && echo --rc2-rules )
step "DONE: stage $OUT (publish dry-run $OUT/cache, recount $OUT/recount/summary.md)"
