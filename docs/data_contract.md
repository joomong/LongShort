# 데이터 계약

`loader`는 발행된 캐시를 읽어 공통 날짜·종목 축의 `DataPanel`을 만든다. 캐시 생성 단계가 입력 정합성을 확인하며, 엔진은 계산에 필요한 필드와 기간을 읽는다. 계산 순서는 [엔진 계산 계약](math_contract.md), 경로 지정은 [설정 안내](config.md)를 따른다.

## 캐시 입력

| 입력 | 역할 |
|---|---|
| `base_data.parquet` | raw OHLC·VWAP·volume·cap |
| `returns.parquet` | 총수익률, 신호 `returns`와 보유 손익 입력 |
| `adj_returns.parquet` | 분할조정 가격수익률, 신호 `adj_returns` |
| `adj_factor.parquet` | 분할 기준 계수 |
| `events.parquet` | 종목 종료와 position move |
| `universe.parquet` | named universe의 구간 멤버십 |
| 그룹 패널의 `sector.parquet`, `industry.parquet` | 종목별 그룹 코드 |
| `master.parquet` | 종목 표시 이름 |
| AFL 카탈로그와 필드 파일 | `AFL.<이름>` 신호 입력 |

가격과 수익률은 날짜·종목 ID로 배열 위치를 연결한다. AFL·그룹 파일은 종목별 컬럼에서 필요한 종목을 읽으며, 그 시점 이하의 마지막 관측을 사용한다. 해당 관측의 null은 유지하고 최초 관측 이전은 NaN이다. AFL 필드는 처음 참조할 때 읽고 같은 파일의 값은 프로세스 안에서 공유한다.

실행 패널의 AFL 입력은 필드 캐시(`afl.cache_budget_gib`, 기본 32GiB)를 거친다. 키는 읽는 시점 파일의 실제 경로(realpath)·크기·mtime·ctime·장치·inode와 날짜·종목 축이며, 바뀐 파일은 다음 요청에서 새 키로 읽는다. 읽는 도중 파일이 바뀌면 한 번 다시 읽고 또 바뀌면 오류다. 요청 자신의 AFL 입력 합계가 예산을 넘으면 실행 전에 거부한다(503). 다만 alpha pool 등 기본 패널에서 부모 컬럼을 직접 읽은 필드는 처음 읽은 시점의 스냅샷으로 프로세스가 끝날 때까지 고정되며, 그 뒤 바뀐 파일은 엔진을 재시작해야 반영된다. 부모 컬럼이 로드된 필드는 실행 패널도 그 스냅샷을 투영해 쓴다.

숫자 필드의 ±inf는 로딩 시 NaN으로 읽는다. 가격은 유니버스 밖에서도 원래 값을 보존한다.

## 유니버스와 그룹

유니버스는 가격과 별도의 마스크다. 캐시의 `fromDate`·`toDate` 구간을 날짜별 멤버십으로 펼치고, 시작·종료일을 포함하며 종료일이 없는 구간은 계속 유지한다. 선택한 named universe마다 마스크를 보관한다.

sector·industry·모든 universe 마스크는 입력 준비 시 **고정 1 bar** 늦춘다. bar `t`에서는 이전 거래일 `t-1` 기준의 분류와 멤버십을 사용한다. 첫 로드 bar의 그룹 코드는 NaN, 멤버십은 비포함이다. 이 규칙은 XML `delay`와 독립적이다.

가격·수익률·기업행동 이벤트는 원래 날짜로 유지한다. XML `delay`는 알파가 관측할 입력을 지연시키고, 시뮬레이터는 해당 날짜의 실제 체결가·보유 수익률·이벤트를 적용한다.

`neutralize`는 현재 실행 bar의 마스크를 적용한 뒤 유한 신호와 유한·0 이상 그룹 코드만 그룹 평균에 포함한다. 그 외는 NaN이고, singleton은 0이다.

## 기업행동과 손익

종목 종료와 승계·분사의 포지션 이동은 `events.parquet`의 발생일에 처리한다. 이동 대상 종목은 패널에 포함하며, 해당 종목이 유니버스 구성원인지는 별도 마스크가 결정한다. 총수익률은 캐시에서 읽고 기업행동 손익을 엔진에서 재산출하지 않는다. position 이동 자체에는 시장 거래비나 execution leg를 부과하지 않는다.

고정 명목금액 근사의 보유 손익에 실제 시장 매매의 execution leg를 더하고 편도 turnover 비용을 뺀다. 체결은 Close/OHLC4/VWAP를 지원한다. OHLC4/VWAP는 해당 날짜의 관측을 사용하며 종가나 전일 값으로 대체하지 않는다.

`missing_close_policy: carry`는 필요한 관측이 없는 보유의 매매를 동결하고 노출 예산을 예약한다. `error`는 필요한 거래를 수행할 수 없으면 실패한다. 계산은 simStart 하루 전 bar부터 시작하고 첫 매매는 첫 IS bar(`max(isStart, simStart)`)에서 일어난다. 그 전 bar는 의도와 평가가격만 준비하고 보유·손익·비용을 만들지 않는다.

API 성과 지표와 GA 적합도는 gross 기준이고 시계열에는 gross/net이 함께 있다. 누적손익과 drawdown은 단리이며 초기 0 고점을 포함한다. `avgPositionCount`는 평균, `minPositionCount`는 최소 보유 종목 수다.

## 엔진과 화면의 결과 계약

응답은 `SimulationResponse {context, alphas, combined}`이고 각 알파 결과는 `AlphaResult`다. `summary.is`·`summary.os`, `yearly`, `performance`, `signals`, `poolCorrelation`을 각 결과 안에 둔다. 결과의 `context`는 실행 당시 설정이며 저장·조회 후에도 보존한다.

수익률·비중·turnover·drawdown·승률·노출 비율은 소수 비율이다. 예를 들어 `annualReturn: 0.2`는 20%다. 시계열의 `timestamps`와 분포 예시의 `exampleTimestamp`는 epoch milliseconds 숫자이고, context의 기간은 `YYYY-MM-DD` 문자열이다.

성과 곡선은 엔진이 `max(simStart, isStart)`에서 기준을 새로 잡는다. OS 진입 시 누적손익과 고점을 다시 시작하지 않는다. `performance.osStartIndex`는 이 보고 시계열 안의 OS 시작 위치다. 달러 표시에는 `context.reportNotional`($20M)을 곱하며 이는 `initial_capital`과 별개다.

신호 값은 `signals.plot.values`에 원래 값으로 전달한다. 히트맵의 날짜별 중심·스케일은 엔진이 함께 보내며, 표본으로 반환한 유한 셀 전체를 사용한다. 유니버스 밖의 유한 값도 통계에 포함하고 멤버십 표시는 별도로 전달한다. 브라우저는 금융 지표나 누적손익·drawdown을 재계산하지 않고 결과를 표시한다.
