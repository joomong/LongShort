# cache_v21/overrides/rc2f — rc2f 고정 스냅샷 교정 CSV (원본은 scratchpad stage_rc2f/X*/overrides, 2026-09-21 복사)

- `X1..X5/*.csv`: 태스크별 교정(split/price/ledger/lifecycle/lineage/aux_additions). 스키마·의미는 `BRIEF_rc2f.md` §3, 검증기 규칙은 `rc2f_apply.py validate`.
- `registry_final.csv`: 사건 레지스트리(4,711 사건, finalStatus 4종 + 사유).
- 적용: `rc2f_apply.py apply --overrides-glob 'overrides/rc2f/X*/*.csv' ...` (rc2f_build.sh 기본 glob 은 scratchpad; 일별 갱신은 이 디렉토리를 기준으로 함 — 새 교정은 새 하위 디렉토리(예 D1/)에 append, overrideId 접두어 유지).
- 발행본 `qa/rc2f/overrides/` 와 동일해야 함(sha 대조).
