# 재시작 기준 최신 종료일 — 2026-09-11

사용자 결정에 따라 엔진 재시작으로 데이터를 다시 읽는 방식을 유지하고, 기본 종료일의 고정 날짜를 `latest`로 바꿨다. 자동 갱신·데이터 버전 저장·실행별 스냅샷 관리는 추가하지 않았다.

```yaml
backtest:
  end_date: latest
```

루트 `config.yaml`과 주식 예제에 적용했다. YAML에서 생략·null·빈 문자열도 latest로 해석한다. 명시적인 `YYYY-MM-DD` 날짜와 시작/종료일 검증은 유지한다.

latest는 선택된 종목들의 close가 **전부 NaN인 연속 끝행**만 제거한다. 중간 결측, 마지막 행의 일부 종목 결측, 0·음수·Inf 가격은 보존한다. 이것으로 데이터 수집 완료나 상폐 손익 처리의 정확성을 보증하지 않는다. 지정된 거래 시작 이후 사용 가능한 행이 없으면 warmup 구간으로 대체하지 않고 오류를 반환한다.

close를 먼저 읽고 최종 날짜 수를 정한 뒤 다른 eager 필드와 lazy 필드의 좌표를 만든다. close·timestamps·universe_mask·lazy 좌표의 길이를 일치시킨다. 이미 실행 중인 엔진에는 새 날짜가 추가되지 않으며, 재시작 시 현재 파일을 다시 읽는다. 웹 화면을 계속 열어 두었다면 새로고침해 갱신된 날짜 범위를 받는다.

## 검증

- 실제 CMake로 config/Parquet 테스트와 서버·data_load_probe 빌드 성공.
- 관련 CTest 2개 target, **16개 회귀 통과**. 추가 5개 검사는 최신/고정 종료일 대조, 끝행 절단 후 eager/lazy/mask 정렬, 중간·부분 결측 보존, 재적재 후 날짜 증가, 거래 구간 전체 결측 오류, 잘못된 비결측 가격 보존을 확인한다.
- 독립 엔진 담당자의 계약·코드·테스트 리뷰에서 차단 사항 없음.
- 실제 주식 예제와 별도 테스트 서버의 `/api/config`에서 `simStart=2010-01-04`, **`simEnd=2026-09-10`**, T=4,449(warmup 252 포함), N=1,056을 확인했다. 고정된 2026-09-08 이후 자료가 반영된다. 테스트 서버는 종료했다.
- 이는 날짜·로더 검증이며 전기간 시뮬레이션 성공을 의미하지 않는다. 기존 상폐/중간 결측 종가로 인한 시뮬레이션 중단은 이번 변경으로 해결하지 않았다.

```bash
cmake --build /tmp/longshort_module_server/build \
  --target test_config_loader test_parquet_loader longshort-server data_load_probe --parallel 4
ctest --test-dir /tmp/longshort_module_server/build \
  -R '^test_(config_loader|parquet_loader)$' --output-on-failure
```
