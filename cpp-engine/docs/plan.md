# C++ 백테스트 엔진 상세 구현 계획

## Summary

LongShort 백테스트 엔진을 STAR 플랫폼 스타일의 C++ 엔진으로 구현한다. 핵심 목표는 (1) STAR 메인 화면(star_main.jpg)에서 확인 가능한 핵심 지표(summary table, yearly table, timeseries chart)의 정합 구현, (2) MDecay 등 STAR 모듈 ABI와의 호환, (3) AVX2 SIMD 및 멀티스레드 GA를 통한 성능 극대화이다.

우선순위는 **STAR C++ 규약 > 수학 스펙** 순서로 고정한다. BARS_PER_YEAR=365 (crypto 365일 기준)는 전역 상수로 고정한다.

---

## Public Interfaces and Naming

### 모듈 ABI
```cpp
// module.h는 <iosfwd>를 include해야 함 (std::ostream/std::istream 참조에 필요)
struct Module {
    unsigned numAssets = 0;
    virtual void init() = 0;
    virtual void calculate(unsigned di, SignalVector& output) = 0;
    virtual ~Module() = default;

    void setting_output_to_nan_is_not_necessary() { skip_nan_init_ = true; }
    bool skips_nan_init() const { return skip_nan_init_; }

    // Checkpoint: stateful 모듈만 override. 기본 no-op이므로 override 누락 시 조용히 빈 상태 저장.
    // → stateful 모듈 전수 round-trip 테스트로 누락 검출 (Phase 3 테스트 참조).
    // CheckpointManager는 top-level 모듈에 대해 호출. composite는 내부에서 sub-모듈 재귀 호출.
    virtual void save_state(std::ostream& /*os*/) const {}
    virtual void load_state(std::istream& /*is*/) {}

private:
    bool skip_nan_init_ = false;
};
```

### 모듈 파일 규약 (STAR 스타일)
| 규칙 | 설명 | 예시 |
|------|------|------|
| **헤더 온리** | 모듈은 `.h` 파일 하나에 전부 구현. `.cpp` 없음 | `Decay.h` |
| **파일명** | PascalCase, `m_` prefix 없음. 복합어는 각 단어 첫 글자 대문자: `Ts` + `Mean` = `TsMean` | `Decay.h`, `TsMean.h`, `TsRank.h`, `TsZscore.h` |
| **구조** | `struct` 상속, 생성자 없음, public 멤버 | `struct MDecay : public Module { ... };` |
| **include 금지** | 모듈 헤더에 `#include` 없음 (`#pragma once` 만 허용). **절대 규칙**: 어떤 조건에서도 모듈 `.h`에 include를 추가하지 않음. 컴파일러별 강제 선행 플래그로 해결 (아래 정책 참고) | `ModulePrelude.h`가 강제 선행 |
| **init() 무파라미터** | `numAssets`는 프레임워크가 `init()` 호출 전 설정 | `void init() override { ... }` |
| **override 필수** | `init()`, `calculate()` 에 반드시 `override` 명시 | `void calculate(...) override` |
| **setting_output_to_nan_is_not_necessary()** | 출력 NaN 초기화 불필요한 모듈은 `init()`에서 호출 | MData, MDecay, MConstant 등 |

### 네이밍 규칙
| 구분 | 규칙 | 예시 |
|------|------|------|
| 모듈 클래스 | `M` prefix + 파일명과 동일한 PascalCase | `MDecay`, `MTsMean`, `MTsRank`, `MTsZscore`, `MEvent` |
| 파라미터 | `IntParameter`, `FloatParameter` (bool은 `IntParameter` 0/1로 처리) | `IntParameter days;` |
| 입력 | `ModuleInput input` | `input.maxLookBack`, `input(back)`, `input().data` |
| 벡터 | `StarVector<T>` (shim: `std::vector<T>`). `Vector<T>` 별칭 사용하지 않음 — `std::vector` 직접 사용 | `StarVector<double> sum;` |
| 시그널 | `SignalVector` (float* + length) | `output[ii] = value;` |

### Cross-sectional 모듈 (stateless)
| 클래스 | XML 태그 | 파일 | 설명 |
|--------|----------|------|------|
| `MRank` | `<rank/>` | `Rank.h` | Cross-sectional percentile rank |
| `MZscore` | `<zscore/>` | `Zscore.h` | Cross-sectional z-score (ddof=1) |

### Time-series 모듈 (stateful, checkpoint 필수)
| 클래스 | XML 태그 | 파일 | 상태 | 설명 |
|--------|----------|------|------|------|
| `MDecay` | `<decay days="N"/>` | `Decay.h` | weights, num, sum, diff | Linear decay weighted average |
| `MTsMean` | `<tsmean days="N"/>` | `TsMean.h` | running_sum, running_count | Rolling mean (O(1) incremental) |
| `MTsRank` | `<tsrank days="N"/>` | `TsRank.h` | circular buffer | Rolling percentile rank |
| `MTsZscore` | `<tszscore days="N"/>` | `TsZscore.h` | running_sum, running_sum_sq, running_count | Rolling z-score (O(1) incremental) |
| `MEvent` | `<event .../>` | `Event.h` | hold array (N,) | Event-driven holding mask |
| `MDelay` | `<delay days="N"/>` | `Delay.h` | ModuleInput ring buffer | Time delay |

### Utility 모듈 (stateless)
| 클래스 | 파일 | 설명 |
|--------|------|------|
| `MData` | `Data.h` | OHLCV data source |
| `MConstant` | `Constant.h` | Scalar broadcast |
| `MAdd, MSub, MMul, MDiv, MLe, MGe` | `Ops.h` | Binary operators |

---

## ModulePrelude.h (강제 선행 include)

모든 모듈 헤더가 의존하는 공통 타입/헤더를 한 곳에 집약. CMake `-include` 플래그로 모든 translation unit에 강제 선행 포함.

```cpp
// include/ModulePrelude.h
#pragma once

#include <vector>
#include <string>
#include <cmath>
#include <cstring>
#include <limits>
#include <algorithm>

#include "signal_vector.h"
#include "star_vector.h"
#include "module.h"
#include "data/data_panel.h"
```

**빌드 설정** (CMakeLists.txt):
```cmake
target_compile_options(engine PUBLIC
    SHELL:-include "${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h"
)
```

> **이식성**: `-include`는 GCC/Clang 전용. MSVC는 `/FI` 사용. `SHELL:` prefix로 플래그+경로를 단일 셸 토큰으로 전달하여 공백 인자 처리 문제 방지. `PUBLIC`으로 테스트 타깃에도 전파.
>
> **정책**: 모듈 `.h` 파일에 `#include`를 추가하는 것은 어떤 조건에서도 허용하지 않는다. 컴파일러가 `-include`를 지원하지 않으면 해당 컴파일러의 동등 플래그(`/FI` 등)로 대체한다. CMake generator expression:
> ```cmake
> target_compile_options(engine PUBLIC
>     $<$<CXX_COMPILER_ID:MSVC>:SHELL:/FI "${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h">
>     $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:SHELL:-include "${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h">
> )
> ```

---

## XML DSL Rules

1. 파이프라인 시작은 `<data expr="..." delay="..."/>` 필수.
2. `<data expr="return"/>` 허용. expr_parser가 "return"을 모듈 체인으로 확장: `close / close[-1] - 1.0` → MData→MDelay→MDiv→MSub→MConstant DAG 조립. 모든 모듈은 기본 생성 후 필드 주입.
3. `<data>` 태그에 `delay` 속성이 있으면 MData 뒤에 MDelay 자동 삽입.
4. `<decay days="N"/>` 는 `MDecay` 모듈로 컴파일.
5. `input="id"` 참조 지원: `<tsmean days="20" input="sig1"/>`. 참조는 저장된 출력값(SignalVector)의 복사이며, 모듈 인스턴스를 공유하지 않는다. 동일 `id`를 여러 곳에서 참조해도 각 소비자는 독립적으로 값을 읽는다.
6. `<signal id="x"/>` 는 현재 출력을 이름 `x`로 저장 후 chain-break.
7. `input` 미지정 모듈은 직전 출력 체이닝.
8. 확장 규칙 (Phase 2): pipe `"A|B|C"`, range `"20...10...60"`, `<choice>`, `<shuffle>`.
9. 오류 처리: unknown signal id, missing attr, invalid range, alpha overflow(`config.max_alpha_count` 초과, 기본값 1000) → 컴파일 에러.

---

## Phase 0: Data Loader

### 목표
`Data/ohlcv/um_1d.parquet` -> `[T, N]` panel 로딩.

### 파일 구조
```
src/data/
  parquet_reader.h / .cpp
  data_panel.h / .cpp
  config_loader.h / .cpp
```

### 핵심 인터페이스
```cpp
struct DataPanel {
    unsigned T;                          // timestamp 수
    unsigned N;                          // symbol 수
    std::vector<int64_t> timestamps;     // epoch_ms, UTC 정렬
    std::vector<std::string> symbols;    // 알파벳 정렬 고정
    std::unordered_map<std::string, std::vector<float>> arrays;  // "close" -> [T*N] row-major

    float& at(const std::string& col, unsigned t, unsigned n);
    const float* row(const std::string& col, unsigned t) const;  // N개 float 포인터

    static DataPanel load(const std::string& parquet_path,
                          const std::vector<std::string>& columns,
                          const std::string& time_col,
                          const std::string& symbol_col);
};
```

### 스펙
- **정렬**: timestamp UTC 오름차순, symbol 알파벳 오름차순.
- **결측치**: 없는 (timestamp, symbol) 조합은 `NaN`으로 채움. `bfill` 절대 금지, `ffill`만 허용하되 기본은 NaN 유지.
- **필수 컬럼**: `open`, `high`, `low`, `close`, `volume`. 누락 시 에러.
- **메모리 레이아웃**: row-major `[T][N]`. SIMD 친화적 연속 접근.
- **정밀도**: DataPanel은 float32. 누적 계산(equity curve, metrics)은 double 사용.
- **의존성**: `arrow-cpp` (Apache Arrow C++ library).

### 테스트
| 케이스 | 검증 |
|--------|------|
| 파일 없음 | 예외 발생 |
| 컬럼 누락 | 예외 발생 + 누락 컬럼명 메시지 |
| 빈 데이터 | T=0, N=0 정상 반환 |
| 정렬 검증 | timestamps 단조증가, symbols 알파벳순 |
| NaN 처리 | 결측 행 NaN 확인 |

---

## Phase 1: Contract Lock

### 목표
모듈 ABI, XML 바인딩, 실행 규칙, checkpoint 스키마, 단위 규약을 확정하고 코드에 반영.

### 파일 구조
```
include/
  module.h              // Module base class, ModuleInput, Parameter types
  signal_vector.h       // SignalVector (float*, unsigned size)
  star_vector.h         // StarVector<T> shim
  ModulePrelude.h       // 강제 선행 include 집약 헤더
```

### 핵심 인터페이스
```cpp
// signal_vector.h
struct SignalVector {
    float* data;
    unsigned size;
    float& operator[](unsigned i);
    const float& operator[](unsigned i) const;
    void fill_nan();
};

// star_vector.h
template<typename T>
using StarVector = std::vector<T>;

struct ModuleInput {
    unsigned maxLookBack = 0;
    const SignalVector& operator()(unsigned back) const;
    const SignalVector& operator()() const;  // back=0 convenience
    void push(const SignalVector& sv);
    void init(unsigned lookback, unsigned num_assets);
};

struct IntParameter {
    int value = 0;
    operator int() const { return value; }
};
struct FloatParameter {
    double value = 0.0;
    operator double() const { return value; }
};
```

### 실행 시점 규칙
| 모드 | 시그널 계산 시점 | 체결 시점 | 수익 실현 시점 |
|------|------------------|-----------|---------------|
| D0 | bar t close | bar t close | bar t+1 |
| D1 | bar t close | bar t+1 close | bar t+2 |

### Checkpoint 스키마

**인터페이스**: Module 베이스에 virtual `save_state(ostream&)` / `load_state(istream&)` 메서드를 두고, 기본 구현은 no-op. Stateful 모듈만 override. 컴파일 타임 강제 없음 — override 누락 시 checkpoint 후 해당 모듈 상태가 초기화되어 출력이 틀어진다. 코드 리뷰로 보장.

```cpp
// Module 베이스 (module.h)
virtual void save_state(std::ostream& /*os*/) const {}
virtual void load_state(std::istream& /*is*/) {}
```

**CheckpointManager 동작**: `pipeline[]`의 top-level 모듈에 대해서만 `save_state()`/`load_state()` 호출. Stateless 모듈은 no-op이므로 자동 skip. Composite 모듈(MEvent, MBinaryOp, MChain)은 자신의 `save_state()`/`load_state()` 내부에서 소유 sub-모듈의 `save_state()`/`load_state()`를 재귀적으로 호출하여 sub-모듈 상태까지 직렬화한다. 기존 `ICheckpointable` + `dynamic_cast` 패턴을 Module 직접 virtual로 교체하여 단일 상속 유지.

**Binary format**: raw binary stream (boost::archive 아님).
```
[4B uint32] di
[4B uint32] N
[N×8B double] prev_weights
[N×8B double] prev_target
각 top-level 모듈 순서대로 save_state(os) 호출 (stateless는 0바이트, composite는 내부 sub-모듈까지 재귀 기록)
```

**Stateful 모듈** (save_state/load_state override 필수): MDecay, MTsMean, MTsRank, MTsZscore, MEvent, MDelay.
**Stateless 모듈** (override 불필요, no-op): MRank, MZscore, MData, MConstant, Ops.

MDelay는 ModuleInput ring buffer를 save/load한다. 복구 시 di+1부터 즉시 재개하므로, ring buffer가 비어있으면 delay 출력이 틀어진다. 따라서 MDelay도 stateful로 분류.

**복구 방식**: checkpoint load 후 저장된 di+1부터 즉시 재개. 재실행(warmup) 없음. 모든 stateful 모듈은 save_state/load_state로 상태를 완전히 복원해야 한다.

### 단위 규칙
| 값 | 단위 | 범위 |
|----|------|------|
| weight | fraction | [-1, 1] (gross=1 기준 정규화) |
| turnover | fraction | [0, 2] (gross=1 기준) |
| return | fraction | 일일 수익률 |
| sharpe | annualized | sqrt(365) 스케일링 |
| fee | fraction per trade | config bps -> 내부 fraction 변환 |

---

## Phase 2: XML Compiler

### 목표
XML alpha DSL을 C++ Module DAG로 컴파일.

### 파일 구조
```
src/compiler/
  xml_parser.h / .cpp       // XML 파싱 (pugixml)
  expansion.h / .cpp         // Phase 2 확장 (pipe, range, choice, shuffle)
  dag_builder.h / .cpp       // Module DAG 조립 + 태그 레지스트리
  expr_parser.h / .cpp       // safe expression parser
```

### 컴파일 3단계
1. **Phase 1 Parse**: XML -> pipeline (순차적 태그 리스트).
2. **Phase 2 Expand**: pipe/range/choice/shuffle 확장 -> 최대 1000개 alpha variant 생성.
3. **Phase 3 Build**: 태그 레지스트리 참조로 Module DAG 조립.

### 태그 레지스트리 (STAR 스타일 팩토리)
```cpp
// 모듈 생성: 기본 생성 + 필드 직접 설정 (생성자 없음)
r.register_module("decay", [](const auto& a, const auto*) {
    auto m = std::make_unique<MDecay>();
    m->days = IntParameter(get_int(a, "days", 20));
    return m;
});

r.register_module("tsmean", [](const auto& a, const auto*) {
    auto m = std::make_unique<MTsMean>();
    m->days = IntParameter(get_int(a, "days", 20));
    return m;
});
```

### 오류 처리
| 조건 | 동작 |
|------|------|
| unknown tag | `CompileError("Unknown module tag: xxx")` |
| missing required attr | `CompileError("Missing attribute: days")` |
| invalid range | `CompileError("Invalid range: ...")` |
| alpha count > `config.max_alpha_count` | `CompileError("Alpha overflow: N > max_alpha_count")` |

---

## Phase 3: Module Runtime (STAR 스타일)

### 목표
11개 모듈 타입을 STAR ABI 호환 헤더 온리 struct로 구현. O(1) incremental 알고리즘 우선.

### 파일 구조
```
src/modules/
  Data.h           // MData — OHLCV data source
  Delay.h          // MDelay — Time delay
  Decay.h          // MDecay — Linear decay (O(1) incremental)
  TsMean.h         // MTsMean — Rolling mean (O(1) incremental)
  TsRank.h         // MTsRank — Rolling percentile rank
  TsZscore.h       // MTsZscore — Rolling z-score (O(1) incremental)
  Event.h          // MEvent — Event-driven mask
  Rank.h           // MRank — Cross-sectional rank
  Zscore.h         // MZscore — Cross-sectional z-score
  Ops.h            // MAdd, MSub, MMul, MDiv, MLe, MGe
  Constant.h       // MConstant — Scalar broadcast
```

> **모든 파일**: `#pragma once` + 구현 inline. `#include` 없음 (ModulePrelude.h가 강제 선행).

### MDecay 상세 스펙 (star_module.jpg 기준 — 핵심 참조 모듈)

```
Linear decay (recency-weighted):
  out[ii] = (in[di]*days + in[di-1]*(days-1) + ... + in[di-days+1]*1) / weighted_sum

구현: O(1) incremental update per instrument per bar
상태: sum[ii], diff[ii], weights[ii], num[ii]
보조: div[] = 1/i 역수 테이블 (나누기 -> 곱셈 대체)
```

**Update rule per bar per instrument:**
```
1. Age:    sum -= diff (각 값 weight -1), weights -= num
2. Remove: if past value finite: diff -= past, num -= 1
3. Add:    if new value finite: sum += new * days, diff += new, weights += days, num += 1
4. Output: sum * div[weights]  (bounds check: w > 0 && w < div.size())
```

**검증 (days=3, values=[10, 20, 30, 40]):**
- Bar 0: sum=30, weights=3 → 30/3=10.0 ✓
- Bar 1: age→20, add→80/5=16.0 ✓ ((20×3+10×2)/5)
- Bar 2: age→50, add→140/6=23.333 ✓ ((30×3+20×2+10×1)/6)
- Bar 3: age→80, remove 10, add→200/6=33.333 ✓ ((40×3+30×2+20×1)/6)

**init()**:
```cpp
input.maxLookBack = days;
weights.resize(numAssets, 0);
num.resize(numAssets, 0);
sum.resize(numAssets, 0.0);
diff.resize(numAssets, 0.0);
int n = days * (days + 1) / 2 + 1;
div.resize(n, 0.0);
for (int i = 1; i < n; ++i)
    div[i] = 1.0 / (double)i;
setting_output_to_nan_is_not_necessary();
```

**checkpoint**: `save_state(os)` / `load_state(is)` — weights, num, sum, diff를 순서대로 write/read

### MTsMean 스펙 (O(1) incremental)
```
out[ii] = mean(input[di-window+1 .. di][ii])  (NaN 제외)

구현: running_sum, running_count. ModuleInput ring buffer로 outgoing value 접근.
  Add new: if finite, running_sum += new, running_count++
  Remove old: if finite, running_sum -= old, running_count--
  Output: running_sum / running_count (running_count > 0)
warmup: bar_count < days → NaN 출력
```

### MTsRank 스펙 (O(days) — incremental 불가)
```
out[ii] = percentile_rank(input[di-window+1..di][ii], current=input[di][ii])
         = (n_less + 1 + (n_equal - 1) / 2) / n_valid

pct=true (기본값), ascending 하드코딩 (큰 값 → 높은 percentile)
구현: circular buffer [N][window], comparison counting
```

### MTsZscore 스펙 (O(1) incremental)
```
out[ii] = (current - mean) / (std + eps)

구현: running_sum, running_sum_sq, running_count.
  variance = (sum_sq - sum^2/n) / (n - ddof)  (one-pass formula)
  Guard: variance < 0 → 0 (floating point artifact)
기본값: ddof=1, min_count=-1 (→ days로 resolve), eps=1e-12
에지 케이스: std < eps → output 0.0
```

> **주의**: one-pass variance는 값이 크고 밀집된 경우 catastrophic cancellation 위험. crypto 데이터 범위에서는 실용적으로 무해. 정밀도 문제 발생 시 Welford's algorithm으로 교체 가능.

### MEvent 스펙
```
매 di:
  hold[ii] = max(hold[ii] - 1, 0)
  if trigger[ii] > threshold: hold[ii] = maxHolddays
  if betAll: out[ii] = base[ii] if hold[ii] > 0 else 0.0
  else:      out[ii] = base[ii]
```

**필드 주입 규칙 (MEvent)**:
| 필드 | 타입 | 주입 주체 | null 허용 | 설명 |
|------|------|-----------|-----------|------|
| `signal_module` | `Module*` | dag_builder | X | base signal 소스 |
| `trigger_module` | `Module*` | dag_builder | X | trigger 조건 소스 |
| `threshold` | `FloatParameter` | dag_builder | — | trigger 임계값 |
| `maxHolddays` | `IntParameter` | dag_builder | — | 최대 hold 기간 |
| `betAll` | `IntParameter` | dag_builder | — | 1: hold 중에만 signal 출력, 0: 항상 출력 |

init()에서 `signal_module`과 `trigger_module`의 `numAssets` 설정 + `init()` 호출 필수.

### MDelay 스펙
```
out[ii] = input(days)[ii]  // days 바 전의 값 복사

구현: ModuleInput ring buffer.
  init(): input.maxLookBack = days;
  calculate(): push(output), memcpy(output, input(days))
checkpoint: ModuleInput ring buffer를 save_state/load_state로 저장/복원.
```

### MRank 스펙 (cross-sectional, stateless)
```
finite = {ii : isfinite(input[ii])}
ranks = average_rank(input[finite])  // 1-based, tie=average
out[ii] = ranks[ii] / |finite|       // percentile [0,1]
out[non-finite] = NaN
```

### MZscore 스펙 (cross-sectional, stateless)
```
vals = input[finite]
center = mean(vals)
scale = std(vals, ddof=1)
out[ii] = (input[ii] - center) / (scale + eps)
eps = 1e-12, min_count = 2
에지 케이스: scale < eps → 전체 output NaN
```

### 산술 연산 모듈 스펙 (Ops.h)
```
MAdd:      out[ii] = lhs[ii] + rhs[ii]
MSub:      out[ii] = lhs[ii] - rhs[ii]
MMul:      out[ii] = lhs[ii] * rhs[ii]
MDiv:      out[ii] = lhs[ii] / rhs[ii]  (rhs=0 → NaN)
MLe:       out[ii] = (lhs[ii] <= rhs[ii]) ? 1.0 : 0.0  (NaN 입력 → NaN)
MGe:       out[ii] = (lhs[ii] >= rhs[ii]) ? 1.0 : 0.0  (NaN 입력 → NaN)
MConstant: out[ii] = value  (모든 ii에 동일 상수)
```

**필드 주입 규칙 (MBinaryOp — MAdd/MSub/MMul/MDiv/MLe/MGe 공통 베이스)**:
| 필드 | 타입 | 주입 주체 | null 허용 | 설명 |
|------|------|-----------|-----------|------|
| `lhs` | `Module*` | expr_parser / dag_builder | X | 좌측 피연산자 |
| `rhs` | `Module*` | expr_parser / dag_builder | X | 우측 피연산자 |

init()에서 `lhs`와 `rhs`의 `numAssets` 설정 + `init()` 호출 필수.

**필드 주입 규칙 (MConstant)**:
| 필드 | 타입 | 주입 주체 | null 허용 | 설명 |
|------|------|-----------|-----------|------|
| `value` | `FloatParameter` | expr_parser / dag_builder | — | broadcast 상수값 |

### setting_output_to_nan_is_not_necessary() 적용 대상
| 모듈 | 호출 여부 | 이유 |
|------|-----------|------|
| MData | O | DataPanel에서 직접 복사 |
| MConstant | O | 상수 broadcast, NaN 불가 |
| MDecay | O | incremental update, 자체 NaN 핸들링 |
| MDelay | X | ring buffer warmup 전 NaN 필요 |
| MTsMean | X | warmup 기간 NaN 출력 필요 |
| MTsRank | X | warmup 기간 NaN 출력 필요 |
| MTsZscore | X | warmup 기간 NaN 출력 필요 |
| MRank | X | cross-sectional, NaN 가능 |
| MZscore | X | cross-sectional, NaN 가능 |
| MEvent | X | 복합 모듈, NaN 가능 |
| Ops | X | 입력 NaN 전파 필요 |

### 테스트
| 케이스 | 검증 |
|--------|------|
| lookback/warmup | window 채워지기 전 NaN 출력 |
| NaN 입력 | NaN 심볼 건너뛰기, 나머지 정상 |
| checkpoint round-trip (전수) | stateful 모듈 전체(MDecay, MTsMean, MTsRank, MTsZscore, MEvent, MDelay) 각각에 대해: N바 실행 → save → load → 이후 바 실행 → save 없이 연속 실행한 결과와 bit-exact 일치 |
| MDecay incremental | brute-force O(N×days) 결과와 O(1) incremental 결과 일치 |
| MTsMean incremental | brute-force 결과와 running sum 결과 일치 |
| MTsZscore incremental | brute-force 결과와 one-pass 결과 일치 |
| MEvent hold count | 트리거 후 정확히 maxHolddays 바 동안 active |
| NaN-interspersed decay | [10, NaN, 30, NaN, 50] 입력 → 올바른 가중 평균 |

### 수용 기준
- 모든 모듈이 STAR struct 패턴 준수 (헤더 온리, override, 무-include).
- O(1) incremental 모듈이 brute-force 대조와 수치 동일.
- checkpoint round-trip 후 계산 연속성 보장.

---

## Phase 4: Simulator Kernel

### 목표
`for di` 루프 기반 시뮬레이터. 시그널 계산 → 정규화 → 수익률/턴오버/수수료 계산. No-lookahead 보장.

### 파일 구조
```
src/engine/
  simulator.h / .cpp
```

### 핵심 인터페이스
```cpp
struct SimulatorConfig {
    unsigned T;
    unsigned N;
    double fee_rate;           // config bps → fraction
    int mode;                  // 0=D0, 1=D1
    unsigned bars_per_year;    // 365
};

struct SimulatorOutput {
    std::vector<double> ret;
    std::vector<double> turnover;
    std::vector<double> min_bet;
    std::vector<double> max_weight;
    std::vector<double> long_weight;
    std::vector<double> short_weight;
    std::vector<int> nonzero_count;
    std::vector<int64_t> timestamps;
};
```

### 모듈 초기화 (STAR 스타일)

**파이프라인 구성**: `pipeline[]`에는 top-level 모듈만 포함한다. Composite 모듈(MEvent, MBinaryOp, MChain)이 소유하는 sub-모듈(signal_module, trigger_module, lhs, rhs, first, second)은 파이프라인에 들어가지 않는다.

**소유권**: Sub-모듈은 소유 composite가 `unique_ptr`로 독점 소유한다. 하나의 sub-모듈이 복수의 composite에 공유되지 않는다. 따라서 각 모듈 인스턴스의 소유자는 정확히 1개(시뮬레이터 또는 composite)이고, init()은 소유자가 1회만 호출한다.

**초기화 책임**: 시뮬레이터는 `pipeline[]`의 top-level 모듈에만 `numAssets` 설정 + `init()` 호출. Sub-모듈의 `numAssets` 설정과 `init()` 호출은 소유 composite 모듈의 `init()` 내부에서 수행한다.

```cpp
// 시뮬레이터: top-level만 초기화
for (Module* mod : pipeline) {
    mod->numAssets = N;
    mod->init();           // composite는 내부에서 sub-모듈 init() 호출
}
```

### NaN 초기화 규칙
파이프라인은 단일 `output` 버퍼를 체이닝하는 모델이다. 각 모듈은 `output`을 입력으로 읽고, 결과를 같은 `output`에 쓴다. 따라서 **매 모듈 전 NaN 초기화는 하지 않는다** — upstream 출력이 지워지기 때문이다.

NaN 초기화는 **파이프라인 첫 모듈(`pipeline[0]`) 직전 1회만** 수행한다. 단, 첫 모듈이 `skips_nan_init() == true`이면 초기화를 건너뛴다 (MData는 DataPanel에서 전 슬롯을 직접 복사하므로 항상 skip). 이 플래그는 모듈이 `init()`에서 `setting_output_to_nan_is_not_necessary()`를 호출하여 설정한다. 적용 대상은 Phase 3의 적용 대상 표 참조.

### 메인 루프
```
for di = 0 to T-1:
    // 1. Module DAG 실행 → raw signal[N]
    if (!pipeline[0].skips_nan_init())
        output.fill_nan()              // 파이프라인 시작 전 1회만
    for each module in pipeline:
        module.calculate(di, output)

    // 2. Signal → Target weight 정규화
    norm_tradable[ii] = isfinite(close[di][ii]) AND isfinite(signal[ii])
    abs_sum = sum(|signal[norm_tradable]|)
    target[ii] = (abs_sum > 1e-12) ? signal[ii] / abs_sum : 0

    // 3. Execution weight (mode 의존)
    exec_w = (mode == D0) ? target : prev_target

    // 4. Portfolio return (이전 가중치 기반 → no-lookahead)
    if di > 0:
        for ii: if prev_close > 0 and cur_close > 0:
            ret[di] += prev_weights[ii] * (cur_close / prev_close - 1)

    // 5. Turnover + Fee
    turnover[di] = sum(|exec_w - prev_weights|)
    ret[di] -= turnover * fee_rate

    // 6. Per-bar statistics
    // 7. State update
```

### Return Series Convention
- `ret` 배열 길이 T. 첫 바(di=0)는 항상 ret=0.
- MetricsEngine에서 `ret[1:]` (길이 T-1) 사용.

### 특수 케이스
| 케이스 | 처리 |
|--------|------|
| close = NaN | normalization untradable (weight = 0) |
| close = 0 (finite) | normalization tradable, return 계산 시 해당 구간 수익률 0 |
| signal = NaN | normalization untradable (weight = 0) |
| abs_sum < 1e-12 | 전체 weight = 0 |

---

## Phase 5: Metrics Engine

### 목표
STAR 메인 화면(star_main.jpg) 기준 핵심 지표 구현: Summary table, Yearly table, Timeseries chart.

### 파일 구조
```
src/metrics/
  summary_metrics.h / .cpp
  yearly_metrics.h / .cpp
  timeseries_output.h / .cpp
  metrics_engine.h / .cpp
```

### 5-A. Summary-level 메트릭

**BARS_PER_YEAR = 365**

| 메트릭 | 공식 | 에지 케이스 |
|--------|------|------------|
| pnl | `equity_last - 1` where `equity = cumprod(1 + r)` | r 전부 NaN → 0.0 |
| cagr | `equity_last^(365/n_days) - 1` | n_days=0 → 0.0 |
| sharpe | `(mean(r) / (std(r, ddof=1) + 1e-12)) * sqrt(365)` | std=0 → 0.0, n<2 → 0.0 |
| mdd | `min(equity / (cummax(equity) + 1e-12) - 1)` | 단일 바 → 0.0 |
| turnover | `mean(turnover_series)` (finite only) | |
| min_bet | `mean(min_bet_series)` (finite only) | |
| max_weight | `max(max_weight_series)` (finite only) | |
| expt_per_alpha | `mean(abs(long_weight - short_weight))` | |
| maxexp | `max(abs(long_weight - short_weight))` | |
| quality | `sharpe * sqrt(abs(1 - maxexp)) * (1 - max_weight)` | 잠정 공식 |
| sharpe400 | 최근 400바 기간의 샤프 비율 | 데이터 부족 시 전체 기간 |
| win_rate | `count(r > 0) / count(r finite)` | |
| margin | `mean(r) / (turnover + 1e-12)` | |

### 5-B. 연도별 분해 메트릭

각 calendar year에 대해 독립적으로 메트릭 산출. equity는 각 연도 1.0에서 시작 (calendar-year return, 누적 아님).

| 메트릭 | 공식 |
|--------|------|
| pnl_pct | `(equity_end / equity_start - 1) * 100` |
| ann_ret | 부분 연도: `(1 + pnl_frac)^(365/n_days) - 1` |
| sharpe | 연도 내 `(mean(r) / std(r, ddof=1)) * sqrt(365)` |
| max_dd | 연도 내 equity 기준 MDD |

### 5-C. 시계열 출력

```cpp
struct TimeseriesOutput {
    std::vector<int64_t> timestamps;
    std::vector<double> equity;          // cumulative equity (1.0 base)
    std::vector<double> pnl_gross;       // fee 차감 전
    std::vector<double> turnover_pct;
    std::vector<int> nonzero_positions;
    std::vector<double> max_weight_pct;
    std::vector<double> drawdown;        // equity / cummax - 1
};
```

---

## Phase 6: Checkpoint and SIMD

### Checkpoint
- Raw binary stream (`std::ostream` / `std::istream`) 사용. boost::archive 아님.
- 저장: simulator state + 각 모듈의 `save_state(os)` 순차 호출.
- 복구: simulator state + 각 모듈의 `load_state(is)` 순차 호출, 다음 di부터 재개.

### AVX2 SIMD (star_module.jpg 참조)
```cpp
#ifdef __AVX2__
    for (; ii < ii_aligned_end; ii += 8)
        process_8instruments(ii, curr, past, output);
    for (; ii < numAssets; ++ii)
        process_1instrument(ii, curr, past, output);
#else
    // Scalar fallback with 2x unrolling
#endif
```

- Scalar parity gate 필수: AVX2 vs scalar 결과 1e-6 이내.
- `StarVector<T>` → SIMD-aligned allocator 교체 가능.
- 현재 17 instruments로는 O(1) scalar가 충분히 빠름. SIMD는 후순위.

---

## Phase 7: GA Multithread

### 목표
XML 탐색공간에서 유전 알고리즘으로 alpha 최적화. 멀티스레드 병렬 평가.

### 파일 구조
```
src/ga/
  search_space.h / .cpp
  ga_engine.h / .cpp
  parallel_evaluator.h / .cpp
```

### GA 스펙
- Population=50, Generations=20, Tournament size=3.
- `std::mt19937` + fixed seed → 재현성 보장.
- Hash cache로 중복 alpha 평가 방지.
- Single thread vs multi thread: 동일 seed → 동일 결과.

---

## 전체 디렉토리 구조

```
cpp-engine/
  CMakeLists.txt
  include/
    module.h
    signal_vector.h
    star_vector.h
    ModulePrelude.h           # 강제 선행 include
  src/
    data/
      parquet_reader.h / .cpp
      data_panel.h / .cpp
      config_loader.h / .cpp
    compiler/
      xml_parser.h / .cpp
      expansion.h / .cpp
      dag_builder.h / .cpp
      expr_parser.h / .cpp
    modules/                   # 헤더 온리 — .cpp 없음
      Data.h
      Delay.h
      Decay.h
      TsMean.h
      TsRank.h
      TsZscore.h
      Event.h
      Rank.h
      Zscore.h
      Ops.h
      Constant.h
    engine/
      simulator.h / .cpp
    metrics/
      summary_metrics.h / .cpp
      yearly_metrics.h / .cpp
      timeseries_output.h / .cpp
      metrics_engine.h / .cpp
    checkpoint/
      checkpoint_manager.h / .cpp
    simd/
      simd_utils.h
    ga/
      search_space.h / .cpp
      ga_engine.h / .cpp
      parallel_evaluator.h / .cpp
  tests/
    test_data_loader.cpp
    test_xml_compiler.cpp
    test_modules.cpp
    test_simulator.cpp
    test_summary_metrics.cpp
    test_yearly_metrics.cpp
    test_checkpoint.cpp
    test_simd_parity.cpp
    test_ga.cpp
    test_determinism.cpp
```

## 빌드 의존성

| 라이브러리 | 용도 | 버전 |
|-----------|------|------|
| Apache Arrow C++ | Parquet 읽기 | >= 14.0 |
| pugixml | XML 파싱 | >= 1.13 |
| yaml-cpp | YAML config 파싱 | >= 0.7 |
| GoogleTest | 테스트 | >= 1.14 |

CMake minimum 3.20, C++17.

---

## 테스트 전략

### 1. 단위 테스트 (GoogleTest)
- 각 모듈, 각 메트릭 함수에 대한 독립 테스트.
- Edge case 중심: NaN, 빈 입력, 단일 바, 단일 심볼.

### 2. Self-Consistency 테스트
- O(1) incremental vs O(N) brute-force 대조 (동일 입력 → 동일 출력).
- Scalar vs AVX2 결과 1e-6 이내 일치.
- Checkpoint save/load 후 결과 일치.

### 3. Determinism 테스트
- Fixed seed GA 2회 실행 → 동일 결과.
- Single thread vs multi thread → 동일 best alpha.

### 4. 통합 테스트
- XML → 컴파일 → 시뮬레이션 → 메트릭 end-to-end.
- 실제 parquet 데이터 사용.

---

## Assumptions

1. STAR C++ 규약이 최우선 기준이다.
2. SIMD/병렬은 scalar 정합성 통과 후 활성화한다.
3. STAR 메트릭 중 `quality`와 `sharpe400`는 정확한 공식이 비공개이므로, 합리적 근사치로 구현하고 추후 보정한다.
4. BARS_PER_YEAR = 365 (crypto 365일 기준) 전역 상수.
5. fee_rate: config.yaml의 `transaction_fee` (bps 단위) → 내부 fraction 변환. 편도 비용이며 turnover에 1회만 곱함.
6. Phase 순서대로 구현하되, Phase 0-4는 선형 의존, Phase 5 이후 병렬 가능.

---

## Phase 간 의존성

```
Phase 0 (Data)
  → Phase 1 (Contract Lock)
    → Phase 2 (XML Compiler)
      → Phase 3 (Module Runtime)
        → Phase 4 (Simulator Kernel)
          → Phase 5 (Metrics Engine)
            → Phase 6 (Checkpoint + SIMD)  [독립 병렬 가능]
            → Phase 7 (GA Multithread)     [독립 병렬 가능]
```
