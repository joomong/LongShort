# afl15_OCFIC

- 작성자: jgo · 풀 등록: 2026-10-29 · 상태: combo15 운영 구성 알파
- 원 연구: `/home/jgo/longshort_workspace/research/notebooks/afl15_OCFIC.ipynb`(정의 카드), `/home/jgo/longshort_workspace/research/themes/afl_operating_book/exp/02_legacy_afl_improvement/notes.md`(원문 관찰 기록), 저장 정의 `/home/jgo/longshort_workspace/alphas/afl15_OCFIC/`

## 무엇을 거래하나

AFL 팩터 `OCFIC` (Operating Cash Flow to Invested Capital)를 변환 없이 그대로 쓴다. 롱 쪽은 투하자본 대비 영업현금흐름이 높은 종목, 숏 쪽은 그 반대이며 섹터 안에서의 상대 순위다.
팩터의 정확한 정의는 AFL 사전을 따르고, 여기서 새 경제적 가설을 붙이지 않는다. 운영 전 관찰(분포·갱신 주기·팩터 간 상관·IC)은 원문 notes에 있다.

## XML

```xml
<data expr="AFL.OCFIC" delay="1" />
<tsfill days="21" />
<rank />
<neutralize group="sector" />
```

| 단계 | 하는 일 |
|---|---|
| `<data expr="AFL.OCFIC" delay="1"/>` | 전일 AFL 값 |
| `<tsfill days="21"/>` | 21영업일 블록 sample-and-hold. 블록 첫날 값을 블록 내내 고정해 월 1회 리밸런스처럼 움직인다 |
| `<rank/>` | 유니버스 안 횡단면 순위 |
| `<neutralize group="sector"/>` | 섹터 평균 제거 → 섹터 중립 롱숏 |

## 데이터와 시점

- 캐시: `/home/base/cache/cache_v2_cash_1d` (읽기 전용). 데이터 끝 2026-10-15 기준 측정
- 유니버스: `TOP500_CAP` (캐시의 이름 있는 유니버스, 신호일 멤버십)
- 지연: 모든 `<data>`는 `delay="1"`. AFL D값은 D+1 장 전에 도착하므로 전일 값으로 신호를 만든다
- 체결: `ohlc4`(저장 설정), 편도 수수료 1.5bp, 목표 포지션 Σ|w| = 1, 연환산 252일
- 기간: simStart 2014-01-02, IS 2016-01-01 ~ OS 직전, OS 2024-01-01 ~
- 원천: `AflData/OCFIC` (`afl_catalog.json`의 `OCFIC`; 정의는 카탈로그 source `AFL_Dictionary.xlsx`)

## 성과

측정 2026-10-29, 공유 엔진(50007) `/api/simulate`, 저장 설정 그대로. 후보 컷 기준은 전체 기간 비용 후 Sharpe 0.8 하나이고 엔진 요약 Sharpe는 gross라 참고만 한다. ALL은 첫 포지션일부터다(그 전 워밍업 구간은 손익 0).

| 구간 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| ALL(2014-01-02~) | 0.73 | 2.11 | 3216 |
| IS(2016~2023) | 0.69 | 2.20 | 2012 |
| OS(2024~) | 0.98 | 2.73 | 700 |

| 구간 | gross Sharpe(엔진 요약) | 일별 turnover % | 평균 보유 종목 | 최대 단일 비중 % |
|---|---|---|---|---|
| IS | 0.71 | 0.95 | 442.58 | 0.81 |
| OS | 0.99 | 0.87 | 438.62 | 0.71 |

### 연도별 (비용 후)

| 연도 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| 2014 | 0.22 | 0.32 | 252 |
| 2015 | 0.78 | 1.46 | 252 |
| 2016 | -0.29 | -0.66 | 252 |
| 2017 | 2.40 | 3.82 | 251 |
| 2018 | 2.18 | 4.76 | 251 |
| 2019 | -0.59 | -1.06 | 252 |
| 2020 | -0.51 | -2.14 | 253 |
| 2021 | 1.52 | 6.97 | 252 |
| 2022 | 1.29 | 5.69 | 251 |
| 2023 | 0.08 | 0.20 | 250 |
| 2024 | 1.68 | 4.22 | 252 |
| 2025 | 0.21 | 0.55 | 250 |
| 2026 | 1.09 | 3.58 | 198 |

## 풀 안의 다른 알파와의 상관 (비용 후 일별 손익, 상위 3)

- `afl15_CFROIC`: +1.00
- `afl15_FCFEqt`: +0.78
- `afl15_Chg3YFCF`: +0.56

## 주의

- 정의 카드 상태는 `saved_definition_not_retested`였다. 위 성과가 이 저장 정의를 현재 캐시로 처음 재측정한 값이며, 원문 연구의 수치는 설정이 달라 그대로 비교하지 않는다.
- 섹터 코드는 2025-02 이전 구간이 현재 스냅샷이다(xfeed에 GICS 이력이 없음). 섹터 중립화의 과거 구간은 PIT가 아니다.
- AFL 월말 처리 결함과 수정안은 `afl_obs_fix_and_449.ipynb`(workspace) 기록을 따른다. 이 정의는 운영 처리 그대로다.
- `afl15_CFROIC`와 비용 후 일별 손익 상관 0.997이다. 사실상 같은 신호이므로 북 안에서 둘을 합치면 그 신호의 예산이 2배가 된다.
