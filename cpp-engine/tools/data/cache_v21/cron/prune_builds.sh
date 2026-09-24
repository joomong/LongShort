#!/bin/bash
# cache_v21/cron/prune_builds.sh — 보존 정책 적용 (D4 초안). 기본은 dry-run(계획만 출력).
#
#   로컬 $ROOT/builds/     : current 가 가리키는 빌드 + 최신 성공 빌드 KEEP_BUILDS(3)개 유지, 나머지 성공 빌드 삭제;
#                            실패 빌드(BUILD.json status != ok)는 KEEP_FAILED_DAYS(7)일 지나면 삭제. archive/ 는 매 빌드에 carry-forward 되므로
#                            최신 빌드만 있으면 revisions 이력은 보존된다(publish 는 archive 를 61 로 내보내지 않음 → 로컬 체인이 원본).
#   로컬 $ROOT/published/  : 최신 KEEP_PUBLISHED(2)개 유지(61 발행본과 sha 동일한 사본).
#   61 $REMOTE.prev-*      : 최신 KEEP_PREV(2)개 유지. 그보다 오래된 것은 asOfDate 의 달에 스냅샷($REMOTE.snap-YYYY-MM)이 없으면 스냅샷으로 rename,
#                            있으면 삭제. $REMOTE.incoming-* 가 1일 이상 남아 있으면(중단된 publish) 삭제. 스냅샷은 삭제하지 않음(수동, 12개월 권고).
#   --apply-local  로컬만 실제 적용(래퍼가 성공 후 호출)      --apply-remote  61 까지 적용(사용자 확인 후 수동/월 1회)
set -u
ROOT=${ROOT:-/home/jgo/cache_v21_builds}
REMOTE=${REMOTE:-/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d}
KEEP_BUILDS=${KEEP_BUILDS:-3}; KEEP_FAILED_DAYS=${KEEP_FAILED_DAYS:-7}; KEEP_PUBLISHED=${KEEP_PUBLISHED:-2}; KEEP_PREV=${KEEP_PREV:-2}
PY=${PY:-/home/jgo/usr/venv-3.11/bin/python}
APPLY_LOCAL=0; APPLY_REMOTE=0
for a in "$@"; do case "$a" in --apply-local) APPLY_LOCAL=1;; --apply-remote) APPLY_REMOTE=1; APPLY_LOCAL=1;; --dry-run) ;; *) echo "unknown arg $a" >&2; exit 2;; esac; done
say() { echo "[prune $(date '+%F %T')] $*"; }
jget() { "$PY" -c "import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2],''))
except Exception: print('')" "$1" "$2"; }
du_h() { du -sh "$1" 2>/dev/null | cut -f1; }

# ---------------- 로컬 builds
if [ -d "$ROOT/builds" ]; then
    CUR=$(readlink -f "$ROOT/current" 2>/dev/null || echo "")
    ok_list=(); fail_list=()
    for d in "$ROOT"/builds/*/; do
        d=${d%/}; [ -d "$d" ] || continue
        st=$(jget "$d/BUILD.json" status)
        if [ "$st" = "ok" ]; then ok_list+=("$d"); else fail_list+=("$d"); fi
    done
    # 성공 빌드: mtime 내림차순, current 제외 상위 KEEP_BUILDS 유지
    n=0
    while IFS= read -r d; do
        [ -z "$d" ] && continue
        if [ "$d" = "$CUR" ]; then continue; fi
        n=$((n + 1))
        if [ $n -gt "$KEEP_BUILDS" ]; then
            say "local build (ok) delete: $d ($(du_h "$d"))"; [ $APPLY_LOCAL = 1 ] && rm -rf "$d"
        fi
    done < <(for d in "${ok_list[@]}"; do [ -n "$d" ] && printf '%s\t%s\n' "$(stat -c %Y "$d")" "$d"; done | sort -rn | cut -f2)
    for d in "${fail_list[@]}"; do
        [ -z "$d" ] && continue; [ "$d" = "$CUR" ] && continue
        age_days=$(( ( $(date +%s) - $(stat -c %Y "$d") ) / 86400 ))
        if [ $age_days -ge "$KEEP_FAILED_DAYS" ]; then say "local build (failed, ${age_days}d) delete: $d ($(du_h "$d"))"; [ $APPLY_LOCAL = 1 ] && rm -rf "$d"; fi
    done
fi
# ---------------- 로컬 published
if [ -d "$ROOT/published" ]; then
    n=0
    while IFS= read -r d; do
        [ -z "$d" ] && continue; n=$((n + 1))
        if [ $n -gt "$KEEP_PUBLISHED" ]; then say "local published delete: $d ($(du_h "$d"))"; [ $APPLY_LOCAL = 1 ] && rm -rf "$d"; fi
    done < <(for d in "$ROOT"/published/*/; do d=${d%/}; [ -d "$d" ] && printf '%s\t%s\n' "$(stat -c %Y "$d")" "$d"; done | sort -rn | cut -f2)
fi
# ---------------- 61: .prev-* / .incoming-* / snapshots  (읽기는 항상, 적용은 --apply-remote)
if [ -d "$(dirname "$REMOTE")" ]; then
    base=$(basename "$REMOTE"); parent=$(dirname "$REMOTE")
    say "remote current: $REMOTE build_id=$(jget "$REMOTE/manifest.json" build_id) asOf=$(jget "$REMOTE/manifest.json" asOfDate)"
    n=0; planned_snaps=" "
    while IFS= read -r d; do
        [ -z "$d" ] && continue; n=$((n + 1))
        asof=$(jget "$d/manifest.json" asOfDate); bid=$(jget "$d/manifest.json" build_id); month=${asof:0:7}
        if [ $n -le "$KEEP_PREV" ]; then say "remote prev keep ($n/$KEEP_PREV): $d build=$bid asOf=$asof"; continue; fi
        if [ -n "$month" ] && [ ! -d "$parent/$base.snap-$month" ] && [[ "$planned_snaps" != *" $month "* ]]; then
            say "remote prev -> monthly snapshot: $d -> $parent/$base.snap-$month (build=$bid asOf=$asof)"; planned_snaps="$planned_snaps$month "
            [ $APPLY_REMOTE = 1 ] && mv "$d" "$parent/$base.snap-$month"
        else
            say "remote prev delete: $d build=$bid asOf=$asof ($(du_h "$d"))"; [ $APPLY_REMOTE = 1 ] && rm -rf "$d"
        fi
    done < <(for d in "$parent/$base".prev-*/; do d=${d%/}; [ -d "$d" ] && printf '%s\t%s\n' "$(stat -c %Y "$d")" "$d"; done | sort -rn | cut -f2)
    for d in "$parent/$base".incoming-*/; do
        d=${d%/}; [ -d "$d" ] || continue
        age_h=$(( ( $(date +%s) - $(stat -c %Y "$d") ) / 3600 ))
        if [ $age_h -ge 24 ]; then say "remote stale incoming (${age_h}h) delete: $d"; [ $APPLY_REMOTE = 1 ] && rm -rf "$d"; else say "remote incoming (${age_h}h, recent — publish in progress?) keep: $d"; fi
    done
    for d in "$parent/$base".snap-*/; do d=${d%/}; [ -d "$d" ] && say "remote snapshot keep: $d asOf=$(jget "$d/manifest.json" asOfDate) ($(du_h "$d"))"; done
    if [ ! -d "$REMOTE" ]; then say "WARNING: $REMOTE does not exist — publish interrupted between the two renames? check .incoming-*/.prev-* and restore by mv"; fi
fi
[ $APPLY_LOCAL = 1 ] || say "dry-run (no changes). --apply-local / --apply-remote to apply."
