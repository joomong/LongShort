# 모듈 자동 탐색 독립 리뷰·검증 — 2026-09-09

기준 HEAD는 `2b84b7b`다. 검토자는 생성기, registry hook, launcher/CMake의 구현자가 아니며 제품 코드를 수정하지 않았다. 검토 문서와 `/tmp`의 독립 fixture·실행 로그만 작성했다. 자동학습 스킬과 프론트 UI는 이번 검토에서 다루지 않았다.

**결론: 기본 모듈 작성 규약에서 파일 추가 → 자동 발견/빌드 → 실제 HTTP XML 실행을 확인했다. 독립 리뷰에서 발견한 Unicode 경로/선언 속성 누락, build 경로 중첩, 숨김 source 탐색 불일치는 수정·재검증됐다. 현재 검토 범위에서 미해결 구현 회귀는 발견하지 못했다.**

## 확인한 작성 규약

- 추가 모듈은 설정 옆 `modules/` 또는 `--modules-dir`의 `.h`/`.hpp`에 둔다. 파일당 기본 생성 가능한 concrete `Module` 직접 파생 타입 하나를 사용한다. `struct X : Module` 또는 `class X : public Module`처럼 public 상속이어야 한다.
- 전역 및 이름 있는 namespace, `final`, 일반 declaration attributes와 `alignas`를 지원한다. 파일 stem의 소문자 값이 XML 태그다. 같은 stem의 대소문자 변형, 중복 class 이름, 내장 tag/type 이름 충돌은 오류다.
- XML 숫자 속성은 public의 직접 `IntParameter`/`FloatParameter` 멤버다. C++ 초기값을 유지하며 XML에 명시한 필드만 덮어쓴다. 일반적인 여러 멤버 선언도 지원한다. private/static/const/pointer/reference/array parameter wrapper는 명시 오류다. 일반 내부 상태는 별도의 숫자/vector 등으로 선언할 수 있다.
- `init()` 및 `calculate(unsigned, SignalVector&)`를 구현한다. simulator가 numAssets를 설정하고 init을 부른다. 상태가 있는 모듈은 init에서 재실행 상태를 초기화해야 한다.
- 정확히 public `const DataPanel* panel`을 선언하면 현재 엔진 panel이 주입된다. 다른 의존성이 자동으로 주입된다고 가정해서는 안 된다.
- `input`은 XML의 공통 캐시 신호 입력 속성이므로 숫자 parameter 이름으로 사용할 수 없다. 일반 모듈에 `input="saved"`를 지정하는 기존 동작은 유지된다.
- `.cpp`/`.cc`/`.cxx` 구현 파일도 빌드에 포함된다. 상대경로의 어느 component라도 `.`으로 시작하면 header와 source가 함께 제외된다. source 자체는 C++ compiler가 최종 검증한다.
- exported interface의 조건부 전처리, class template, 다중/간접 상속, 익명 namespace나 중첩된 모듈만 있는 header 등은 지원 규약 밖이다. 이 생성기는 C++ 전체 문법을 반영하는 reflection 기능이 아니다.

## 기존 동작 보존 검토

기존 7개 builtin factory 본문을 유지하고 `register_discovered_modules(registry)`만 연결했다. 따라서 tsmean/tsrank/tszscore/decay의 XML days 기본값 20, delay의 1, rank/tsrank의 bool 형식 pct, event의 특수 속성 처리가 유지된다. 선언상 IntParameter의 기본 0을 기존 XML 기본값으로 바꾸는 오류는 피했다.

`data/signal/event` compiler 노드와 `choice/shuffle` expansion 노드도 자동 일반 factory로 대체하지 않는다. `Ops.h`의 abstract/복수 타입, `SignalCache.h`의 복수 구현, pointer 주입이 필요한 내장 복합 모듈을 일반 custom 모듈로 잘못 생성하지 않는다.

새 custom 숫자 변환은 전체 문자열 소비, int 범위, float finite 여부를 검사한다. 오류는 XML tag·속성·값을 포함한다. unknown attribute는 거절하되 공통 input은 허용한다. registry는 중복 태그를 덮어쓰지 않고 오류를 반환하며, 실패 후 기존 factory가 남는다.

## 독립 리뷰에서 발견하고 해결한 사항

| 발견 사항 | 재현·영향 | 수정 확인 |
| --- | --- | --- |
| Unicode include 경로 | JSON escape를 #include에 사용해 실제 한글 경로가 `\ud55c...` 경로로 생성됐다. g++ preprocessor가 실제 파일을 찾지 못했다. | include header-name은 UTF-8 원문을 유지한다. quote/backslash/newline처럼 지원할 수 없는 경로는 명시 거절한다. 한글 경로의 실제 .hpp/.cpp 모듈 빌드·HTTP 실행으로 재검증했다. |
| 선언 attribute 때문에 parameter/module 누락 | `[[maybe_unused]] FloatParameter gain`, `alignas(8) FloatParameter gain`은 parameters=[]로, `struct [[nodiscard]]`와 `struct alignas(16)` 모듈은 helper로 조용히 건너뛰었다. | declaration attributes를 인식한다. 실제 class/field attribute 모듈의 기본값 및 XML override가 모두 기대값으로 실행됐다. |
| build-dir가 modules-dir 내부 | recursive .cpp 탐색이 generated registry 및 CMake compiler-ID 소스까지 사용자 source로 재수집할 수 있었다. | launcher와 직접 CMake 모두 build가 modules 또는 그 하위인 구성을 거절한다. 빌드 담당이 양쪽 회귀 검사를 추가했고, reviewer는 최종 guard 코드를 확인했다. |
| 숨김 header/source 탐색 불일치 | generator는 `.archive/`를 제외하지만 CMake/run_logic_tests는 그 아래 .cpp를 컴파일했다. 실제 CMake glob으로 재현했다. | 동일한 상대경로 component 필터를 적용한다. 일부러 문법이 깨진 `.archive/{Broken.h,Broken.cpp}` 및 `.Disabled.cpp`를 둔 실제 서버 빌드가 통과했다. |

생성기 구현자가 함께 수정한 익명/중첩-only 모듈의 명시 오류, pointer/reference 반환 메소드의 field 오인 방지, 16진 digit separator 처리도 최종 소스와 생성기 회귀 테스트에서 확인했다.

## 실제 end-to-end 실행

독립 fixture 경로:

```text
/tmp/longshort_peer_e2e_20260909_cj83r_db/
  personal config.yaml
  panel.parquet
  user modules/
  peer_e2e.py
  peer_additional_e2e.py
  results.json
  additional_results.json
  *_build.log
  *_server.log
```

synthetic 데이터는 40개 날짜·3개 종목의 float32 OHLCV와 timestamp[ms]다. 실제 `run_engine.py`가 `/tmp/longshort_module_server/build`에서 configure/build를 하고, 준비된 executable을 별도 포트에 띄운 뒤 `/api/health`와 `/api/simulate`를 호출했다. 각 단계가 끝나면 reviewer가 시작한 서버 process group을 종료했다. 중앙 registry source의 임시 수정이나 수동 empty factory fallback은 사용하지 않았다.

모듈 예시는 기본 `shift{2}`/`gain{0.5}`로 입력 신호를 affine 변환하는 named-namespace의 `class ... final : public Module`이다. 비교는 source XML 문자열만 제외하고 raw signalPlot, 분포·범위, 모든 요약·연도별·시계열 JSON에 대해 정확한 equality로 수행했다. 양수 상수 배수는 정규화에서 사라질 수 있으므로 손익만 비교하지 않고 raw 신호도 확인했다.

| 실제 검사 | 결과 |
| --- | --- |
| `<reviewaffine/>`와 `<data expr="close*0.5+2"/>`의 기본값 일치 | 통과 |
| gain=2, shift=3 명시와 기존 `close*2+3` expression 일치 | 통과 |
| 음수 FloatParameter gain=-1.5와 기존 expression 일치 | 통과 |
| saved signal의 `input` 경로를 custom 모듈에 전달 | 통과 |
| public const DataPanel* panel 자동 주입 후 데이터 읽기 | 통과 |
| 정수 소수/부분문자열, float NaN/Inf/overflow, unknown 속성 6개 요청 | 모두 HTTP 400 및 모듈 문맥 오류 |
| unchanged 재실행 시 generated.cpp와 binary mtime 유지 | 통과 |
| header 기본값 수정 후 재빌드 및 새 기본값 반영 | 통과 |
| header를 하위 디렉터리로 이동 후 태그·동작 유지 | 통과 |
| header 삭제 후 재빌드·새 서버에서 Unknown module | 통과 |
| 한글 경로의 .hpp + 별도 .cpp + class/member attributes + 깨진 숨김 백업 동시 존재 | 실제 자동 빌드·HTTP 기본값 실행 통과 |
| attributed Int/Float field의 XML override | 통과 |

실제 E2E 결과는 **12개 검사 통과**다. 이 중 잘못된 입력 검사 한 항목에는 서로 다른 HTTP 요청 6개가 포함된다.

## parser 테스트 및 한계

- `python3 cpp-engine/tests/test_generate_modules.py`: 최초 독립 검증에서 18개가 통과했고, 아래 최종 보완 후 **19개 테스트 통과**를 다시 확인했다.
- 별도 소스 fixture로 namespace/final, 메소드의 argument·local parameter, declaration attributes, Unicode include, hex digit separator, 조건부 body, template initializer의 comma 등을 검사했다.
- arbitrary C++ macro/alias 기반 field reflection을 지원하지 않는다. 전처리 분기별로 여는 brace 수가 달라 raw source의 brace가 균형을 이루지 않는 함수 body는 합법적인 C++이어도 scanner가 거절할 수 있다. 함수 내부의 모든 전처리 패턴을 지원한다고 해석하면 안 된다.
- 괄호/중괄호 밖 template argument의 comma가 들어간 일부 initializer는 현재 간단한 declarator splitter가 거절한다. 단순 member initializer 규약을 사용하거나 식을 괄호/중괄호로 감싸야 한다. 조용히 잘못된 숫자 값을 사용하는 결과는 확인되지 않았다.
- Make/Ninja의 build lifecycle, overlap/cache/lock 및 실패 시 stale binary 미실행 단위 검사는 빌드 담당 결과와 별도로 문서화돼 있다. reviewer의 실제 HTTP E2E는 준비된 Make build를 사용했다.
- 다른 계정 로그인 또는 repository 전체를 다른 위치로 옮긴 fresh full build는 이 리뷰에서 실행하지 않았다. 공백·한글 module 경로와 외부 cwd 실행은 실제 확인했고, 오래된 source를 가리키는 CMakeCache는 launcher가 명시 오류로 새 build-dir를 요구하는 것을 코드에서 확인했다.
- 실행 중인 서버에 source 변경이 hot reload되는 기능은 아니다. launcher로 빌드 후 새 process를 시작하는 경로를 검증했다.

최종 독립 리뷰와 테스트는 지정한 module authoring 규약 안에서 자동 발견/빌드/XML 실행이 연결된다는 근거다. DB/YAML 데이터 모델 확장이나 임의 C++ 문법 지원을 검증한 결과로 확대하지 않는다.


## 최종 factory 보완에 대한 추가 독립 확인

전체 12개 E2E를 반복하지 않고 마지막 변경에 해당하는 범위만 확인했다.

- 생성된 클래스 이름이 `::`로 시작하므로 `module`/`attrs`/`panel` 같은 factory 지역명이나 매개변수에 타입 이름이 가려지지 않는다. 이름 있는 namespace의 `::peer::Decorated`도 동일하게 생성된다. 이 변경은 field 값·생성자 기본값을 바꾸지 않는다.
- `decltype(module->field)`와 `::IntParameter`/`::FloatParameter`의 정확한 타입 일치를 static_assert로 확인한다. unparenthesized member-access의 decltype이므로 field의 선언 타입을 비교한다.
- 같은 이름을 가진 다른 namespace의 wrapper를 만들고 SDK FloatParameter를 암시적으로 받을 수 있게 한 독립 fixture를 실제 g++로 컴파일했다. 새 static_assert의 `XML parameters must use the SDK parameter types` 메시지로 거절되는 것을 확인했다. 재현 파일은 `/tmp/longshort_sdk_type_peer_mbimm54e/`에 있다.
- `volatile` 및 `thread_local` parameter wrapper가 조용히 생략되지 않고 scanner의 명시 오류로 거절되는 것도 독립 확인했다.
- 생성기 최종 테스트 19개가 통과했다.
- 마지막 생성기 상태로 기존 한글 경로·namespace·declaration attributes·별도 cpp가 있는 Decorated 모듈을 실제 launcher로 다시 빌드하고 HTTP smoke 한 건을 실행했다. `<decorated gain="0.5" shift="-2"/>`와 기존 `close*0.5-2` expression의 raw 신호 및 전체 지표 JSON이 정확히 일치했다. 테스트 서버는 종료했다.

추가 HTTP 산출물은 기존 E2E 디렉터리의 `peer_final_smoke.py`, `final_factory_smoke.log`, `final_smoke_result.json`이다. 최종 작은 변경에서 새 회귀는 발견하지 못했다.
