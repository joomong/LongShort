# 프론트 논리/연결 오류 보완 — 2026-09-11

기준은 clean `main@3e663d6`다. 프론트와 `.gitignore`를 담당했으며 backend 제품 소스는 변경하지 않았다. 변경은 미커밋이며 root가 통합한다.

## 적용

- git 전체 history의 `frontend/lib`에는 `api.ts`만 있어 누락된 6개 원본을 복구할 수 없었다. 현재 컴포넌트의 실제 계약에 맞게 `uplotUtils`, `uplotAdapters`, `themeColors`, `metricColors`, `snapshotColors`, `colorScale`를 구현했다. production에서 stub/alias 대체를 사용하지 않는다.
- `.gitignore`의 일반 `lib/` 제외 규칙은 유지하되 `/frontend/lib/`와 그 내용을 명시적으로 추적한다. Tailwind가 lib의 metric class도 생성하도록 content 범위를 보완했다.
- 시그널 색상은 raw 값의 부호를 보존한다. 이전 heatmap의 전체 종목 순위 색상은 모든 값이 양수여도 하위 종목을 short 색으로 만들었다. 현재는 positive=long/positive color, negative=short/negative color이며 절대크기로 강도를 표현한다. tooltip도 동일하다.
- batch/GA의 signalPlot·signalRange·distribution을 각각의 alpha에 보관하고, Combined 또는 개별 alpha를 선택하면 세 패널이 같은 payload를 읽는다. 상세 화면에 들어갔다 돌아와도 선택과 실제 uPlot/canvas가 유지·복원된다.
- simulate는 collectSignals=true/topK=5를 요청한다. 미수집 alpha는 해당 alpha.xml을 단일 simulate로 요청해 signals만 캐시한다. 최초 run의 sim/is/os 날짜·mode·threads를 보존하고 AbortSignal/결과 revision으로 늦은 응답이 새 결과에 붙지 않게 한다. on-demand cache는 alpha 5개 LRU이며 combined는 별도다. metrics/return series는 evict하지 않는다.
- sampling의 표시 행/종목 수를 UI에 표시한다. top-level과 single/combined payload는 adapter에서 같은 참조를 재사용해 중복 보관을 줄인다.
- setResult가 선택 배열만 초기화하고 Set을 남기던 오류를 수정했다. 조건부 chart DOM의 attach/detach·resize·uPlot destroy를 실제 helper에서 처리한다.
- multi Save는 선택한 각 `alpha.xml`을 저장한다. 상세 화면에서는 이전 multi 체크가 남아 있어도 현재 상세 alpha.xml을 우선한다. 누락 XML이 있는 multi 선택은 쓰기 전에 거절한다. Load는 XML의 명시 delay를 보존하고 상세 화면을 벗어나 로드한 editor 내용을 보여준다.
- UI에서 호출되지 않던 기존 runGA 경로를 같은 hook의 Run GA/Stop에 연결했다. Stop 및 hook unmount는 transport를 abort하고 late GA complete/error를 무시한다.
- root 승인에 따라 새 editor 기본값만 `<data expr="close" delay="0"/>`로 변경했다. 실행 기본값은 D1(mode=1)이며 기존 저장·편집 XML의 명시 delay를 바꾸지 않는다.
- 기존 C++ D0/D1 계산을 실제 API mode=0/1에 연결하고, Jobs를 threads로 전달한다. backend config.mode가 0이면 최초 UI도 D0로 표시한다. Jobs 상한 32를 UI 상태에서도 지킨다. 같은 mode/threads가 GA와 on-demand 재요청에도 사용된다. 새 체결 모델은 추가하지 않았다.

## 실제 C++ fixture

삭제된 Python-engine fixture 의존성을 제거했다. `frontend/scripts/capture_engine_fixtures.mjs`가 실제 C++ API에서 single/multi/GA 응답을 캡처하며, `__tests__/fixtures/capture.json`에 데이터 config와 요청을 기록한다.

사용 서버는 root 소유 `http://127.0.0.1:39207`, config는 `/tmp/longshort_integration_20260911/config.json`이다. 합성 6종목, 연환산 252, 비용 5bp이며 마지막 120일만 요청해 fixture를 약 156KB로 제한했다. signalPlotMaxCells=60으로 실제 sampling metadata 및 끝 날짜 보존도 포함한다. C++가 출력한 요약·연도별·시계열 숫자를 프론트에서 다시 생성하거나 맞춰 쓰지 않았다.

## 교차 리뷰

- engine 담당이 wire `loadedFields`가 필드명 배열이 아니라 count number임을 지적해 타입을 수정했다.
- root가 상세 화면에서 과거 multi 체크가 저장 우선순위를 가리는 문제를 지적했다. multi 저장을 multi 화면에만 한정하고, 선택이 남은 상세 저장 회귀를 추가했다.
- engine 담당은 batch/combined 선택, on-demand의 원래 설정 보존, revision/Abort stale 차단, LRU와 Save detail 우선순위를 독립 검토했다. 최종 mode/threads/config.mode 연결과 원래 runSettings 재사용도 독립 확인했으며 새 문제 없이 sign-off했다.

## 검증

- 저장소 lock을 유지한 `npm ci`. package.json/package-lock.json의 dependency 버전을 바꾸지 않았다.
- `npm run lint`: 경고·오류 없음.
- `./node_modules/.bin/tsc --noEmit`: 통과.
- `npm test`: **15개 파일, 65개 테스트 통과**, 제외·skip 없음.
- `npm run build`: 실제 Next production compile, lint/type 검증, static page 생성 완료. 누락 helper stub이나 external module alias 우회를 사용하지 않았다.
- 실제 시스템 Chrome에서 production frontend private port 39407로 **Playwright 5개 통과**:
  - 실제 C++ 39207과 연결해 D0/D1 body/response mode와 Jobs를 확인했다. GA 완료 후 세 signal 패널과 실제 uPlot canvas가 표시됐다.
  - 실제 C++ alpha 저장소에 multi의 expanded XML과 detail XML을 저장하고 GET/Load로 비교했다. 테스트 고유 prefix의 파일만 생성했으며 finally에서 삭제했다.
  - 실제 C++ fixture를 응답으로 사용하는 브라우저 검사에서 canvas pixel의 long/short 색, sampling 표시, 미수집 alpha의 on-demand 요청, 캐시 재사용, 상세 화면 왕복 후 chart 복원을 확인했다. 지표뿐 아니라 실제 canvas의 색을 읽어 검증했다.
  - 기본 page/control/layout smoke 3개도 통과했다. 브라우저 pageerror는 없었다.

브라우저 실행 명령:

```bash
cd frontend
PLAYWRIGHT_PORT=39407 \
PLAYWRIGHT_CHROME_PATH=/usr/bin/google-chrome \
PLAYWRIGHT_USE_BUILD=1 \
PLAYWRIGHT_ENGINE_URL=http://127.0.0.1:39207 \
npm run test:e2e -- --workers=1 --reporter=line
```

Playwright가 생성한 private frontend 서버는 종료됐다. root 소유 C++ 서버는 임의로 종료하지 않았다. live integration test는 `PLAYWRIGHT_ENGINE_URL`이 있을 때만 실행되며, 이번 검증에서는 실제 URL을 제공해 실행했다.

## 남는 조건

일반 simulate 요청은 frontend abort가 즉시 backend 계산 취소까지 보장하지 않는다. 늦은 응답이 화면·캐시를 덮어쓰는 것은 차단했다. GA backend 취소는 engine 담당이 별도 구현·검증한다. 이 문서는 frontend transport/상태 회귀와 live GA 완료 경로의 검증 결과다.

실제 개인 DB/AFL 데이터 연결과 backend의 유니버스·퇴출 정책 검증은 root/engine/data 담당 범위다. 이번 프론트 검증에서는 합성 C++ 데이터를 사용했다.


## 최종 mode/threads 독립 리뷰

C++ API 담당이 프론트 동결 직전 `api.ts`, `useBacktest.ts`, `SignalPanelSelector`, `alphaStore`, `types`의 최종 연결을 별도로 검토했다. 수정 차단 사항은 발견하지 못했다.

- D0→0, D1→1이 새 서버의 정수 mode 계약과 일치하고, 일반 실행 및 GA에 동일하게 전달된다. config.mode=0도 truthiness가 아닌 명시적 비교로 받아 UI에 반영한다.
- Jobs는 threads로 전달되며 helper 인자 순서와 두 hook 호출이 일치한다.
- 완료 결과는 요청 당시의 세 날짜·mode·threads를 보관한다. on-demand는 현재 UI 설정보다 이 원 실행 설정을 우선하며, `??`를 사용하므로 원 mode=0을 D1로 잘못 대체하지 않는다.
- effect 의존성에 mode/threads가 포함되고 기존 revision/Abort guard를 유지한다. 서버·response·GA complete 타입의 mode도 0|1로 맞췄다.

이미 보고된 전체 65개 unit/5개 Chrome 검증 이후의 최종 코드 연결 리뷰이며, 새 문제가 없어 테스트를 반복하지 않았다. 리뷰 중 제품 코드는 변경하지 않았다.

## Root clean clone 검증

프론트 커밋 `2cd5b67`을 `/tmp/longshort_clean_clone_20260911_qumkt2kn`으로 실제 `git clone --no-hardlinks`했다. 이 clone의 frontend에서 `npm ci --no-audit --no-fund && npm run build`가 exit 0으로 완료됐다. 원래 작업 트리의 node_modules·.next·미추적 lib를 사용하지 않았다. 누락 파일 문제가 clean clone에서도 해결됐음을 확인했다. [기록](logic_completion_2026-09-11/clean_clone_build.json)
