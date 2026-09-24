# 이벤트 override 백필 절차

`build_events.py`가 벤더 원문만으로 정산 조건을 세우지 못한 종목을 손으로 채우는 경로임. 이 도구는 `cache_v21/events_build.py`가 직접 import·호출하는 빌더 입력이다. thin-v1 실행 설정은 이 도구의 구식 이벤트 파일을 직접 읽지 않는다. 빌더가 정제한 대가를 `returns_total`과 간단한 position 이동 행으로 발행한다. 현재 엔진 설정은 최상위 `events:`를 거부한다.
override 행은 파싱 행과 **똑같은** 앵커 측정(k_t·k_s)·가드·불변식 경로를 통과하므로, 조건만 정확히
넣으면 나머지는 자동으로 맞춰짐.

## 1. 무엇을 먼저 채우나

워크리스트에서 **실제 백테스트에서 보유 상태로 정산된 노출**이 큰 행부터. 판단 기준:

1. `cacheUnitStatus != ok` — 엔진이 조건으로 계산하지 못하는 행(기본 R로 떨어짐).
2. 유니버스 멤버십이 길고 정산 직전 비중이 큰 행(노출 = 보유 gross × 정산 bar).
3. 같은 노출이면 `no_ratio` > `no_successor_id` > `no_cache_successor_at_settlement` > `no_terms` 순
   (앞쪽일수록 조건 한 줄로 해결됨). `no_ratio`는 승계사까지 풀렸고 **교환비 한 개만** 넣으면 되는 행이고,
   `no_terms`는 현금·교환비·승계사가 전부 없는 행임 — 둘을 섞어 세지 말 것.
4. `rejected:*`는 원문이 틀린 것이라 override로만 고칠 수 있음 — 근거가 확실할 때만.

## 2. 근거 우선순위

| 순위 | 근거 | `source` 값 |
|---|---|---|
| 1 | DB `co_acthist.actiontxt` 원문 재해석(파서가 못 읽은 문형) | `co_acthist_text` |
| 2 | DB `filingdata`/EDGAR 8-K 본문·별첨 | `sec_8k` 또는 `filingdata` |
| 3 | 회사 보도자료(합병 완료 공시) | `press_release` |
| 4 | 뉴스 기사 | `web_news` |

낮은 순위로 갈수록 `confidence`를 낮춤(1~2 = `high`, 3 = `high`/`medium`, 4 = `medium`/`low`).
**확인되지 않으면 기입하지 말고 `unresolved`로 보고함.** 추정치 금지.

## 3. 기입 규칙

- 단위는 **딜 당시 거래 기준**(as-traded). 캐시 단위 환산은 생성기가 `k_t`·`k_s`로 처리함.
- 통화는 **USD만**. 캐나다달러 등은 기입 금지(파일에서 `foreign_currency`로 남김).
- **선택형 대가(cash-or-stock election)는 원칙적으로 기입 금지.** 예외는 단 하나 —
  합병계약·DEFM14A가 **미선택 주주(non-election shares)에게 배정된 대가**를 정하고 프로레이션 결과가
  확인되는 경우에 한해 `election_rule=non_election_default`로 그 값을 기입함. 일괄 현금 옵션,
  옵션 단순평균, 배분율 추정치는 **금지**(어느 주주도 그 대가를 받지 않았음). 확인 불가면
  `unresolved:election_default_unknown`으로 남김.
- 혼합 딜은 `cashPerShare`와 `successorRatio`를 **각각** 채움. 하나로 합치지 않음.
- `successorTradingItemId`는 승계사의 **트레이딩아이템 id**(티커 아님). 로컬 마스터에서 확인.
  `successorRatio`만 있고 id를 모르면 그대로 둬도 되지만 그 행은 평가 불가(`no_successor_id`)로 남음.
- `endDate`는 **보유 마지막 날**. 정산은 그 다음 무호가 bar에서 일어남.

## 4. 파일 스키마 (`data/events/events_override.csv`)

필수 열(하나라도 없거나 비면 생성기가 **exit 1**):

`tradingItemId, endDate, actionCode, cashPerShare, successorTradingItemId, successorRatio,
source, confidence, evidence_url, note, entered_on`

선택 열: `ticker`, `settlementDate`, `successorTicker`, `election_rule`, `supersedes`, `superseded_on`.

- `source` ∈ `sec_8k | filingdata | press_release | co_acthist_text | web_news`
- `confidence` ∈ `high | medium | low`
- `evidence_url` 공백 금지, `entered_on`은 기입일(YYYY-MM-DD)
- `cashPerShare`·`successorRatio`가 **둘 다** 비면 거부(구조가 없음)
- `election_rule`은 **`non_election_default`만** 허용. note에 EITHER/ELECT 문구가 있는데 비어 있으면 거부.
  `election_rule`을 쓰면 note에 **non-election 조항 또는 프로레이션 결과**를 인용해야 함(`non-election`/
  `proration`/`default` 중 하나가 note에 있어야 통과)
- `supersedes`(이전 값 요약)와 `superseded_on`(정정일)은 **함께** 채워야 함

출력 parquet에는 `source='override'`와 `overrideSource`(원 근거 종류)로 남음.

## 4-1. 정정 절차

기존 행을 고칠 때는 (a) 원 행을 `data/events/events_override_history.csv`에 `retired_on`·
`retired_reason`과 함께 append하고, (b) 새 행의 `supersedes`에 이전 값 요약(예:
`cash=6.06 ratio=0.8263 succ=372277281`), `superseded_on`에 정정일을 적음. 근거가 철회돼 행을
통째로 빼는 경우도 같은 방식으로 이력에 남기고 override에서는 삭제함.

## 5. 재생성·검증

```bash
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/build_events.py \
  --master data/master/uss_cash_master_refreshed.parquet \
  --out data/events/events.parquet \
  --override data/events/events_override.csv --start 2009-01-01
```

- 스키마·근거 검증 실패 → exit 1(파일 미생성).
- 성공 시 stderr에 `verified invariants (1)-(6)`·`verified look-ahead: 0 of N`이 찍혀야 함.
- 규칙만 따로 시험: `build_events.py --self-test`(DB·캐시 불필요).
- 보고서 `data/events/events_report.md` §4-2c에서 `cacheUnitStatus` 분포가 줄었는지 확인.

DB는 읽기 전용(`default_transaction_read_only=on`)으로만 접근하고 `/home/base`는 수정하지 않음.
