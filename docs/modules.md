# 개인 C++ 모듈

개인 모듈은 `~/longshort_workspace/modules/`에 둔다. 공용 내장 모듈은 `cpp-engine/src/modules/`에 둔다. 루트 `longshort`가 CMake를 직접 호출해 증분 빌드한다.

```bash
/home/base/longshort/longshort_platform/longshort start --build-only  # 빌드만
/home/base/longshort/longshort_platform/longshort restart            # 빌드 → 중지 → 기동
```

사용자 모듈은 계산 클래스와 XML 등록 선언을 같은 헤더에 작성한다. `ModulePrelude.h`가 기본 타입, Eigen, 등록 API를 제공하므로 별도 include는 필요하지 않다. 예를 들어 `MySignal.h`는 다음과 같다.

```cpp
#pragma once

struct MMySignal : Module {
    FloatParameter factor{1.0f};
    IntParameter days{20};

    void init() override {} // 매 실행마다 내부 상태 초기화
    void calculate(unsigned, SignalVector& output) override {
        for (unsigned i = 0; i < numAssets; ++i)
            output[i] *= static_cast<float>(factor);
    }
};

REGISTER_MODULE(MySignal, MMySignal, "mysignal",
    MODULE_PARAMETER(MMySignal, factor),
    MODULE_PARAMETER(MMySignal, days))
```

XML에서 `<mysignal factor="2" days="10"/>`으로 사용한다. `REGISTER_MODULE`은 정적 객체의 초기화를 사용하지 않는다. 전처리 매크로가 C++ 등록 함수를 만들고, 엔진의 registry 초기화 시 그 함수를 호출한다.

- 매크로의 첫 인수 `MySignal`은 헤더의 확장자를 뺀 파일명과 같아야 한다. CMake는 모든 `.h/.hpp`를 include하고 `register_MySignal(registry)`를 호출한다. 파일명은 C++ 식별자여야 하며 하위 폴더가 달라도 동일한 파일명은 중복할 수 없다.
- 두 번째 인수는 실제 C++ 타입이며 namespace를 포함할 수 있다. 세 번째 인수는 XML 태그다. 파일명과 태그가 같을 필요는 없다.
- XML 속성은 `MODULE_PARAMETER(타입, 멤버)`로 지정한다. public `IntParameter`, `FloatParameter`, `GroupParameter` 멤버를 지원한다. 일반 내부 상태는 등록하지 않는다. 속성을 생략하면 C++ 초기값을 유지한다.
- 파라미터가 없으면 `REGISTER_MODULE(MySignal, MMySignal, "mysignal")`로 작성한다.
- 헤더마다 등록 함수 하나를 제공한다. 보조 헤더는 자동 수집 경로 밖에 두거나 `.inc`로 두고 명시적으로 include한다. `.cpp/.cc/.cxx` 구현 파일은 자동으로 빌드에 포함된다.
- 점으로 시작하는 파일/폴더는 제외된다. `.archive/`로 옮기면 비활성화된다.
- 모듈 추가·수정·삭제는 다음 빌드에 반영된다. 중앙 registry나 CMake 소스 목록을 수정할 필요가 없다. 이름 충돌, 알려지지 않은 XML 속성, 잘못된 파라미터 값은 오류다.

`FloatParameter`는 float로 저장하며 XML 속성을 float로 바로 읽는다(평문 소수만, 범위 밖이나 비유한 값은 오류). 기본값은 `1e-12f`처럼 float 리터럴로 쓴다. 모듈 출력의 ±inf는 다음 모듈에 전달하기 전에 NaN으로 바뀐다.

`DataPanel`과 공통 실행 도구는 `include/simulation_data.h`, `include/parallel_executor.h`에 정의된다. 데이터와 실행 유니버스가 필요하면 public `const DataPanel* panel = nullptr;`, `const std::vector<uint8_t>* universe = nullptr;`을 선언한다. 등록 API가 타입을 검사하고 연결한다. `universe`는 내장 rank/neutralize와 같은 실행 선택 마스크(T*N, 이전 거래일 기준 멤버십)이며 nullptr이면 `panel->universe_mask`를 사용한다. 빈 vector는 전 종목이다. `sel && !sel->empty()`로 확인한다.

**주의:** 실행은 해당 run이 건드릴 수 있는 종목만 남긴 run panel에서 돌므로 `numAssets`와 열 인덱스는 전체 패널이 아니라 run panel 기준이고, 그 밖의 종목은 아예 없다. `numAssets` 전체를 cross-section으로 쓰는 모듈은 반드시 유니버스 마스크를 적용해야 한다. 마스크가 없으면 범위 밖 멤버였거나 successor로만 남은 열이 cross-section에 섞인다.

그룹 속성은 `GroupParameter group{""};`과 `MODULE_PARAMETER(타입, group)`으로 등록한다. XML 값은 내장 `group=`과 같이 `sector|industry`만 허용하고 공백을 제거하며 패널에 필드가 있어야 한다. 생략하면 C++ 기본값을 유지한다. 그룹 이름은 `group.name()`으로 읽는다.

매크로 대신 `inline void register_MySignal(ModuleRegistry& registry)` 안에서 `register_type<MMySignal>(registry, "mysignal").parameter("factor", &MMySignal::factor);`로 직접 선언할 수도 있다.

개인 설정과 모듈·알파는 보관하고, 자동 생성되는 `.longshort/`는 Git 및 workspace 복사 대상에서 제외한다. 빌드 의존성과 실행 방법은 [빌드 안내](build_and_validation.md)를 따른다.

## lookback

`virtual unsigned lookback(unsigned first_bar) const`는 입력이 처음 유효한 패널 bar `first_bar` 이후 출력이 처음 유효해질 때까지의 bar 수다. 기본값은 0이다. 과거 bar를 쓰는 모듈은 반드시 override한다. 예: 길이 `days` rolling window는 `days - 1`. 기본값을 두면 엔진은 필요한 과거를 모른 채 `Insufficient history` 검사를 통과시킨다.

```cpp
unsigned lookback(unsigned) const override { return (int)days > 1 ? (int)days - 1 : 0; }
```

내장 값 유지 모듈 두 개의 lookback 예(정의는 `math_contract.md`):

| 태그 | 동작 | lookback(first_bar) |
|---|---|---|
| `<tsfill days="n"/>` (`TsFill.h`, `MTsFill`) | 유효 입력은 즉시 출력, NaN이면 마지막 유효 입력 이후 n bar까지 그 값, 이후·최초 유효 이전은 NaN. 상태는 종목별 마지막 값·bar | `days`. 첫 bar부터 유한값을 내지만 긴 이력 실행과 같아지는 시점이 `first_bar + days` |
| `<tshold days="n"/>` (`TsHold.h`, `MTsHold`) | 패널 bar 0 기준 n bar 블록의 첫 입력을 NaN 포함 그대로 유지(옛 tsfill 코드) | 다음 블록 시작까지 `(n − first_bar % n) % n` |

## 저장 신호 입력 (signal_N)

모듈이 앞에서 `<signal id>`로 저장한 신호 여러 개를 받으려면 `SignalInputs`를 함께 상속한다. 컴파일러가 `signal_1`, `signal_2`, … 속성을 해석해 같은 bar의 저장 행을 넘겨준다. 출력은 저장 행만으로 정하며 현재 chain 값은 덮어쓴다.

```cpp
struct MSpread : Module, SignalInputs {
    unsigned min_signal_inputs() const override { return 2; }
    unsigned max_signal_inputs() const override { return 2; }
    void init() override {}
    void calculate(unsigned, SignalVector& output) override {
        for (unsigned i = 0; i < numAssets; ++i) output[i] = signal_row(0)[i] - signal_row(1)[i];
    }
};
REGISTER_MODULE(Spread, MSpread, "spread")
```

XML: `<spread signal_1="a" signal_2="b"/>`. `signal_N`은 `MODULE_PARAMETER`로 등록하지 않는다. 번호 누락, 없는 id, 개수 범위 밖은 컴파일 오류다. 내장 `add/sub/mul/div/min/max/mean/nanmean`, `gate`, `csresid`, `bucketdemean`이 같은 방식이다(`math_contract.md`).
