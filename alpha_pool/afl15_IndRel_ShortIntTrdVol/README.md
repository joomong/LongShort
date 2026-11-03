# afl15_IndRel_ShortIntTrdVol

- 작성자: jgo · 풀 등록: 2026-10-29 · 상태: combo15 운영 구성 알파
- 원 연구: `/home/jgo/longshort_workspace/research/notebooks/afl15_IndRel_ShortIntTrdVol.ipynb`(정의 카드), `/home/jgo/longshort_workspace/research/themes/afl_operating_book/exp/02_legacy_afl_improvement/notes.md`(원문 관찰 기록), 저장 정의 `/home/jgo/longshort_workspace/alphas/afl15_IndRel_ShortIntTrdVol/`

## 무엇을 거래하나

AFL 팩터 `IndRel_ShortIntTrdVol` (Ind Grp Rel Short Interest Trading Volume)를 변환 없이 그대로 쓴다. 롱 쪽은 산업군 내 상대 공매도 잔고/거래량이 낮은 종목, 숏 쪽은 그 반대이며 섹터 안에서의 상대 순위다.
XML의 음수 부호(`-AFL.IndRel_ShortIntTrdVol`)가 방향을 뒤집는다. 팩터의 정확한 정의는 AFL 사전을 따르고, 여기서 새 경제적 가설을 붙이지 않는다. 운영 전 관찰(분포·갱신 주기·팩터 간 상관·IC)은 원문 notes에 있다.

## XML

```xml
<data expr="-AFL.IndRel_ShortIntTrdVol" delay="1" />
<tsfill days="21" />
<rank />
<neutralize group="sector" />
```

| 단계 | 하는 일 |
|---|---|
| `<data expr="-AFL.IndRel_ShortIntTrdVol" delay="1"/>` | 전일 AFL 값에 −1을 곱한 것 |
| `<tsfill days="21"/>` | 21영업일 블록 sample-and-hold. 블록 첫날 값을 블록 내내 고정해 월 1회 리밸런스처럼 움직인다 |
| `<rank/>` | 유니버스 안 횡단면 순위 |
| `<neutralize group="sector"/>` | 섹터 평균 제거 → 섹터 중립 롱숏 |

## 데이터와 시점

- 캐시: `/home/base/cache/cache_v2_cash_1d` (읽기 전용). 데이터 끝 2026-10-15 기준 측정
- 유니버스: `TOP500_CAP` (캐시의 이름 있는 유니버스, 신호일 멤버십)
- 지연: 모든 `<data>`는 `delay="1"`. AFL D값은 D+1 장 전에 도착하므로 전일 값으로 신호를 만든다
- 체결: `ohlc4`(저장 설정), 편도 수수료 1.5bp, 목표 포지션 Σ|w| = 1, 연환산 252일
- 기간: simStart 2014-01-02, IS 2016-01-01 ~ OS 직전, OS 2024-01-01 ~
- 원천: `AflData/IndRel_ShortIntTrdVol` (`afl_catalog.json`의 `IndRel_ShortIntTrdVol`; 정의는 카탈로그 source `AFL_Dictionary.xlsx`)

## 성과

측정 2026-10-29, 공유 엔진(50007) `/api/simulate`, 저장 설정 그대로. 후보 컷 기준은 전체 기간 비용 후 Sharpe 0.8 하나이고 엔진 요약 Sharpe는 gross라 참고만 한다. ALL은 첫 포지션일부터다(그 전 워밍업 구간은 손익 0).

| 구간 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| ALL(2014-01-02~) | 0.52 | 1.27 | 3216 |
| IS(2016~2023) | 0.47 | 0.96 | 2012 |
| OS(2024~) | 0.82 | 3.12 | 700 |

| 구간 | gross Sharpe(엔진 요약) | 일별 turnover % | 평균 보유 종목 | 최대 단일 비중 % |
|---|---|---|---|---|
| IS | 0.53 | 3.09 | 455.12 | 0.69 |
| OS | 0.86 | 3.45 | 451.37 | 0.79 |

### 연도별 (비용 후)

| 연도 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| 2014 | -0.34 | -0.48 | 252 |
| 2015 | 0.28 | 0.38 | 252 |
| 2016 | 0.65 | 0.98 | 252 |
| 2017 | 1.03 | 1.28 | 251 |
| 2018 | -1.14 | -2.05 | 251 |
| 2019 | 0.18 | 0.40 | 252 |
| 2020 | 0.36 | 1.03 | 253 |
| 2021 | 1.68 | 3.82 | 252 |
| 2022 | -0.04 | -0.08 | 251 |
| 2023 | 1.29 | 2.35 | 250 |
| 2024 | 1.75 | 3.41 | 252 |
| 2025 | 1.01 | 2.82 | 250 |
| 2026 | 0.52 | 3.11 | 198 |

## 풀 안의 다른 알파와의 상관 (비용 후 일별 손익, 상위 3)

- `afl15_RevMagFY1C`: +0.25
- `afl15_IO_NAT`: +0.25
- `afl15_IndRel_OCFP`: +0.24

## 주의

- 정의 카드 상태는 `saved_definition_not_retested`였다. 위 성과가 이 저장 정의를 현재 캐시로 처음 재측정한 값이며, 원문 연구의 수치는 설정이 달라 그대로 비교하지 않는다.
- 섹터 코드는 2025-02 이전 구간이 현재 스냅샷이다(xfeed에 GICS 이력이 없음). 섹터 중립화의 과거 구간은 PIT가 아니다.
- AFL 월말 처리 결함과 수정안은 `afl_obs_fix_and_449.ipynb`(workspace) 기록을 따른다. 이 정의는 운영 처리 그대로다.
