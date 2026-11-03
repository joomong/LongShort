# afl15_Chg3YFCF

- 작성자: jgo · 풀 등록: 2026-10-29 · 상태: combo15 운영 구성 알파
- 원 연구: `/home/jgo/longshort_workspace/research/notebooks/afl15_Chg3YFCF.ipynb`(정의 카드), `/home/jgo/longshort_workspace/research/themes/afl_operating_book/exp/02_legacy_afl_improvement/notes.md`(원문 관찰 기록), 저장 정의 `/home/jgo/longshort_workspace/alphas/afl15_Chg3YFCF/`

## 무엇을 거래하나

AFL 팩터 `Chg3YFCF` (3Y Chg in Free Cash Flow per Share)를 변환 없이 그대로 쓴다. 롱 쪽은 주당 잉여현금흐름의 3년 변화가 큰 종목, 숏 쪽은 그 반대이며 섹터 안에서의 상대 순위다.
팩터의 정확한 정의는 AFL 사전을 따르고, 여기서 새 경제적 가설을 붙이지 않는다. 운영 전 관찰(분포·갱신 주기·팩터 간 상관·IC)은 원문 notes에 있다.

## XML

```xml
<data expr="AFL.Chg3YFCF" delay="1" />
<tsfill days="21" />
<rank />
<neutralize group="sector" />
```

| 단계 | 하는 일 |
|---|---|
| `<data expr="AFL.Chg3YFCF" delay="1"/>` | 전일 AFL 값 |
| `<tsfill days="21"/>` | 21영업일 블록 sample-and-hold. 블록 첫날 값을 블록 내내 고정해 월 1회 리밸런스처럼 움직인다 |
| `<rank/>` | 유니버스 안 횡단면 순위 |
| `<neutralize group="sector"/>` | 섹터 평균 제거 → 섹터 중립 롱숏 |

## 데이터와 시점

- 캐시: `/home/base/cache/cache_v2_cash_1d` (읽기 전용). 데이터 끝 2026-10-15 기준 측정
- 유니버스: `TOP500_CAP` (캐시의 이름 있는 유니버스, 신호일 멤버십)
- 지연: 모든 `<data>`는 `delay="1"`. AFL D값은 D+1 장 전에 도착하므로 전일 값으로 신호를 만든다
- 체결: `ohlc4`(저장 설정), 편도 수수료 1.5bp, 목표 포지션 Σ|w| = 1, 연환산 252일
- 기간: simStart 2014-01-02, IS 2016-01-01 ~ OS 직전, OS 2024-01-01 ~
- 원천: `AflData/Chg3YFCF` (`afl_catalog.json`의 `Chg3YFCF`; 정의는 카탈로그 source `AFL_Dictionary.xlsx`)

## 성과

측정 2026-10-29, 공유 엔진(50007) `/api/simulate`, 저장 설정 그대로. 후보 컷 기준은 전체 기간 비용 후 Sharpe 0.8 하나이고 엔진 요약 Sharpe는 gross라 참고만 한다. ALL은 첫 포지션일부터다(그 전 워밍업 구간은 손익 0).

| 구간 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| ALL(2014-01-02~) | 0.54 | 1.21 | 3216 |
| IS(2016~2023) | 0.72 | 1.54 | 2012 |
| OS(2024~) | -0.02 | -0.06 | 700 |

| 구간 | gross Sharpe(엔진 요약) | 일별 turnover % | 평균 보유 종목 | 최대 단일 비중 % |
|---|---|---|---|---|
| IS | 0.74 | 1.42 | 430.46 | 0.78 |
| OS | -0.00 | 1.44 | 431.76 | 0.71 |

### 연도별 (비용 후)

| 연도 | 비용 후 Sharpe | 비용 후 연수익 % | 거래일 |
|---|---|---|---|
| 2014 | 0.57 | 0.72 | 252 |
| 2015 | 1.85 | 2.56 | 252 |
| 2016 | -1.80 | -2.85 | 252 |
| 2017 | 1.53 | 2.01 | 251 |
| 2018 | 0.56 | 0.97 | 251 |
| 2019 | 0.32 | 0.56 | 252 |
| 2020 | 1.42 | 4.59 | 253 |
| 2021 | 1.39 | 3.79 | 252 |
| 2022 | 0.03 | 0.07 | 251 |
| 2023 | 1.58 | 3.14 | 250 |
| 2024 | 1.27 | 2.75 | 252 |
| 2025 | -0.67 | -1.75 | 250 |
| 2026 | -0.39 | -1.50 | 198 |

## 풀 안의 다른 알파와의 상관 (비용 후 일별 손익, 상위 3)

- `afl15_FCFEqt`: +0.57
- `afl15_OCFIC`: +0.56
- `afl15_CFROIC`: +0.56

## 주의

- 정의 카드 상태는 `saved_definition_not_retested`였다. 위 성과가 이 저장 정의를 현재 캐시로 처음 재측정한 값이며, 원문 연구의 수치는 설정이 달라 그대로 비교하지 않는다.
- 섹터 코드는 2025-02 이전 구간이 현재 스냅샷이다(xfeed에 GICS 이력이 없음). 섹터 중립화의 과거 구간은 PIT가 아니다.
- AFL 월말 처리 결함과 수정안은 `afl_obs_fix_and_449.ipynb`(workspace) 기록을 따른다. 이 정의는 운영 처리 그대로다.
- 원문 notes O1: 3년 전 FCF가 0 근처이거나 음수면 변화율이 폭발하거나 부호가 뒤집힌다(1~99분위 −16.65~16.28). 순위 변환이 크기는 지우지만 기준값의 부호 문제는 남는다.
