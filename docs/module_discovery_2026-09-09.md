# 사용자 모듈 자동 탐색·빌드 — 2026-09-09

추가 C++ 모듈마다 중앙 registry와 CMake 목록을 편집하던 절차를 없앴다. 설정 파일 옆의 `modules/`에서 모듈을 발견하고, 실행기가 필요한 빌드를 완료한 후 서버를 시작한다. 기존 내장 모듈과 손익 계산은 유지한다.

## 사용하는 흐름

```text
개인 작업 폴더/
  config.yaml
  modules/
    MySignal.h
    MySignal.cpp   # 구현을 분리한 경우에만
  alphas/
    strategy.xml
  .longshort/
    build/         # 자동 생성되는 개인 빌드 캐시
```

1. 설정 파일 옆 `modules/`에 모듈 헤더를 둔다.
2. 저장소 루트에서 `python3 cpp-engine/tools/run_engine.py --config /개인/작업/config.yaml`을 실행한다.
3. `MySignal.h`는 XML에서 `<mysignal .../>`로 사용한다.

파일의 추가·수정·삭제는 다음 프로그램 시작에 반영된다. 변경이 없으면 C++ 컴파일/링크를 생략하고 기존 빌드 결과를 사용한다. 발견·configure·compile 단계가 실패하면 과거 실행 파일을 새로 실행하지 않는다. 직접 CMake를 사용하는 개발자도 모듈 추가 후 중앙 목록을 편집할 필요 없이 `cmake --build`로 자동 갱신할 수 있다.

현재 자동 반영 시점은 **프로그램 시작**이다. 이미 실행 중인 엔진의 코드를 즉시 바꾸는 hot reload는 구현하지 않았다. 브라우저 화면만 새로고침하는 것은 엔진 재시작이 아니다.

## 모듈에 필요한 선언

다음 파일을 `modules/MySignal.h`로 두면 별도 등록 코드는 필요 없다.

```cpp
#pragma once

namespace my_research {
struct MMySignal : public Module {
    FloatParameter factor{1.0};
    FloatParameter offset{0.0};

    void init() {}
    void calculate(unsigned, SignalVector& output) {
        for (unsigned i = 0; i < numAssets; ++i) {
            if (std::isfinite(output[i]))
                output[i] = static_cast<float>(output[i] * static_cast<double>(factor)
                                                + static_cast<double>(offset));
        }
    }
};
}
```

```xml
<data expr="close"/>
<mysignal factor="-1" offset="2"/>
<zscore/>
```

- 파일마다 기본 생성 가능한 concrete `Module` 직접 public 파생 타입 하나를 둔다. 전역 및 이름 있는 namespace, `final`, 일반 declaration attribute/alignas를 지원한다.
- 파일 stem을 소문자로 바꾼 값이 XML 태그다. 클래스 이름을 등록 목록에 적지 않는다. 클래스/namespace/parameter 이름은 ASCII identifier 작성 규약을 따른다.
- public `IntParameter`와 `FloatParameter` 직접 멤버가 XML 속성이다. SDK의 실제 parameter 타입인지 생성된 C++에서도 검사한다.
- XML에서 생략한 속성은 C++ 생성/멤버 초기값 그대로다. 명시한 속성만 바인딩한다. 정수 전체 파싱·범위, 실수 전체 파싱·finite 여부를 검사한다. 알 수 없는 속성은 오류다.
- 범위의 업무 의미(예: days가 양수여야 하는지)는 모듈에서 검증한다. 자동 발견기가 필드 이름만 보고 임의의 수학 규칙을 추가하지 않는다.
- 상태가 있는 모듈은 `init()`에서 이전 실행의 상태를 초기화한다. simulator가 `numAssets`를 설정한 뒤 init을 호출한다.
- public `const DataPanel* panel = nullptr;` 선언이 있으면 현재 엔진 데이터를 주입한다. 다른 임의의 의존성을 자동으로 만들어 주지는 않는다.
- `input`은 기존 XML의 저장된 신호 입력 선택자이므로 parameter 이름으로 사용하지 않는다. `<mysignal input="saved"/>` 경로는 유지된다.
- 헤더와 별도 `.cpp`/`.cc`/`.cxx` 구현 파일 모두 해당 폴더 아래에서 자동 발견된다. 숨김 파일·폴더는 제외된다.
- 같은 태그/클래스, 내장 태그/타입 충돌은 명시 오류다. 한글 및 공백이 있는 상위 폴더를 지원한다. XML 태그가 되는 파일 stem은 ASCII 이름을 사용한다.

바로 복사해 볼 예제는 [Affine.h](../cpp-engine/examples/modules/Affine.h)에 있다. 기본 `modules/`에는 안내 파일만 있으며 예제 알파를 자동으로 활성화하지 않는다.

## 지원 문법 범위

이 도구는 정해진 모듈 선언 규약을 읽는 생성기이며 C++ 전체 reflection이 아니다.

- private/static/const/volatile 등의 parameter wrapper, 포인터·참조·배열 parameter는 명시 오류다. 내부 상태는 일반 숫자/vector 등의 멤버로 둔다.
- class template, 여러 Module 타입을 한 헤더에 export, 다중/간접 Module 상속, 익명 namespace나 중첩 타입만 있는 모듈은 지원 규약 밖이다.
- macro/별칭으로 모듈 인터페이스나 parameter 타입을 생성하는 방식은 사용하지 않는다. 전처리로 클래스/속성 선언 자체를 조건부 선택하는 형태는 거절한다. 일반적인 헤더 가드는 지원한다.
- 함수 본문은 C++가 컴파일하지만 선언 스캐너가 원문 괄호 구조도 읽으므로, `#if/#else` 각 분기에 여는 중괄호만 두는 패턴처럼 원문 전체의 괄호 균형이 맞지 않는 형태는 사용할 수 없다.
- 초기화 식의 괄호 밖 template comma는 간단한 declarator와 구분하지 못할 수 있다. `FloatParameter gain = (make_gain<1, 2>());`처럼 식을 괄호로 감싸면 된다. C++ 초기값을 Python이 계산하는 것은 아니다.

지원되지 않는 인터페이스를 고치거나 C++ 컴파일 오류가 나면 실행기는 실패를 표시하고 중단한다. 변경한 모듈이 컴파일되지 않았는데 예전 서버 바이너리를 대신 실행하는 fallback은 없다.

## 구현

- [generate_modules.py](../cpp-engine/tools/generate_modules.py): `.h/.hpp`를 재귀 검색하고 태그·qualified class·직접 parameter·선택적 panel 주입 정보를 읽는다. 생성 파일은 변경된 경우에만 원자적으로 교체한다.
- [DiscoverModules.cmake](../cpp-engine/cmake/DiscoverModules.cmake): 파일 추가/삭제와 companion 소스까지 빌드 그래프에 반영한다. 생성 registry는 build/generated에만 쓴다.
- [module_attributes.h](../cpp-engine/src/compiler/module_attributes.h): custom XML 속성을 검증한다.
- [dag_builder.cpp](../cpp-engine/src/compiler/dag_builder.cpp): 기존 내장 factory 다음에 생성된 registration 함수를 호출한다. 중복 등록은 덮어쓰지 않는다.
- [run_engine.py](../cpp-engine/tools/run_engine.py): 개인 config 기준 탐색 경로와 빌드 캐시를 선택하고 configure/build/start를 이어서 실행한다. 다른 source의 캐시 재사용, 모듈 폴더 안의 build 폴더, 동시 빌드를 거절한다.

내장 `tsmean/rank/decay/...`의 기존 factory는 유지했다. 따라서 기존 XML의 days 기본값, pct의 bool 표기 및 signal/event 처리 규칙은 바뀌지 않는다. 사용자 모듈은 엔진 내부 `cpp-engine/src/modules`를 수정하는 대신 개인 `modules/`에 추가한다.

## 최초 환경과 PM 전달

C++ compiler, CMake, Python 및 엔진 의존성은 처음에 준비돼 있어야 한다. 이 작업은 SDK 환경을 없애는 것이 아니라 모듈을 추가할 때마다 반복하던 등록·빌드 조작을 자동화한 것이다. PyArrow 번들/의존성 prefix를 이용하는 최초 설정과 실제 검증 환경은 [빌드 문서](module_discovery_build_2026-09-09.md)에 있다. 해당 CMake 설정은 개인 build cache에 보관되므로 이후에는 반복하지 않는다.

PM에게는 모듈 헤더·필요 구현/의존 코드와 최종 XML, 엔진 버전 및 실행 조건을 전달한다. PM도 자신의 config 옆 modules에 넣고 같은 실행기를 사용하면 된다. generated registry와 `.longshort/build`를 손으로 복사하거나 중앙 코드에 붙여 넣을 필요는 없다. 데이터 기준과 실행 조건은 결과 재현을 위해 따로 일치시켜야 한다.

## 검증과 리뷰

- 기존 계산을 포함한 C++ core **139/139 통과**: 기존 130개, 새 속성 검증 7개, registry 충돌 2개.
- 생성기 **19개**, launcher **11개**, Make/Ninja 증분 빌드 통합 **1개** 통과.
- 실제 서버 전체 configure·컴파일·링크 성공. Arrow 18/newer reader 생성 API 차이는 FileReaderBuilder로 최소 호환 처리했으며 데이터 선택·수익률 산식은 변경하지 않았다.
- 독립 담당자가 실제 launcher→CMake→HTTP **12개 검사**를 수행했다. 기본값/override/음수/input route/panel binding의 raw signal과 전체 metrics가 기존 expression과 동일했다. 수정·이동·삭제, no-op build, 한글 폴더의 attributed .hpp와 별도 cpp도 확인했다.
- root 추가 검사에서 생성 factory 지역 변수와 같은 이름인 module/attrs/panel 클래스 3종도 실제 C++ 컴파일을 통과했다. 최종 생성기 보완 후 독립 담당자가 namespace/attributes/별도 cpp의 HTTP smoke 1건을 추가로 재확인했다.
- 리뷰에서 Unicode header-name escape, declaration attribute 누락, build 폴더 재수집, 숨김 source 필터 불일치를 발견하고 수정했다.
- 실제 테스트 서버는 종료했다. 일반 GUI/브라우저 패키징 또는 실행 중 코드 교체를 검증했다는 뜻은 아니다.

[엔진 검증](module_discovery_engine_2026-09-09.md) · [빌드·기동 검증](module_discovery_build_2026-09-09.md) · [독립 리뷰](module_discovery_review_2026-09-09.md)
