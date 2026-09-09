# 2026-09-09 리팩토링 독립 검증

## 검증 범위

계산의 의미를 유지하는 변수명/자료구조/메모리 수명 정리를 검증한다. DB, YAML 및 체결 모델 확장은 포함하지 않는다. 이 파일과 `cpp-engine/tools/run_logic_tests.py`는 제품 소스 편집 담당자와 별도 검증 담당자가 작성했다.

`run_logic_tests.py`는 Arrow/Parquet 및 서버 빌드 환경과 분리하여 실제 엔진 소스와 기존 비 HTTP 테스트를 컴파일한다. `DataPanel::load()`의 미사용 함수는 링크 시 제거되며, 파일 로딩/YAML/HTTP 통합을 검증했다는 뜻은 아니다. `main.cpp`는 공식 Drogon/Trantor 헤더를 이용한 별도 구문·타입 검사를 제공한다.

## 기존 Boost 실패 해결

- 기본 컴파일러 검색 경로에서 `/usr/local/include/boost`의 **1.85.0 헤더**를 사용하면서 시스템 **1.83.0 동적 serialization 라이브러리**와 링크하고 있었다.
- 원본 `module_input_legacy_v0.bin`은 archive version **20**이다. 이전 라이브러리가 지원하는 버전은 **19**이므로 엔진의 역직렬화 함수에 들어가기 전부터 `unsupported version`이 발생했다.
- [Boost 1.84 공식 소스](https://www.boost.org/doc/libs/1_84_0/libs/serialization/src/basic_archive.cpp)에 archive version 20 전환이 명시돼 있다. 검증에서는 설치돼 있는 헤더에 맞춰 [공식 Boost.Serialization 1.85.0 소스](https://github.com/boostorg/serialization/tree/boost-1.85.0)를 `/tmp`에 별도 정적 빌드했다.
- 그 라이브러리로 최소 아카이브 헤더 검사와 기존 체크포인트 테스트 **6개가 모두 통과**했다. `ModuleInputLoadsLegacyFixtureV0`도 포함한다. 원본 체크포인트 픽스처와 시스템 패키지는 수정하지 않았다.

```text
Boost headers=1_85 supported archive version=20
Checkpoint.ModuleInputLoadsLegacyFixtureV0: PASSED
Checkpoint suite: 6 / 6 PASSED
```

## 재현 환경

- Linux x86_64, GCC 13.3.0, C++17, Python 3.11.
- Boost 헤더 1.85.0: `/usr/local/include` (읽기 전용으로 사용).
- 공식 소스 고정: Boost.Serialization 1.85.0, GoogleTest 1.14.0, JsonCpp 1.9.5, pugixml 1.14.
- 의존성 소스: `/tmp/longshort_refactor_deps`.
- 검증 빌드: `/tmp/longshort_refactor_standalone`.
- 일반 CMake/서버 환경을 설치하거나 저장소 CMake 구조를 바꾸지 않았다.

아래 명령은 위 임시 디렉터리가 없어졌을 때 사용할 의존성 준비 절차다. 기존 Boost 1.85.0 헤더가 다른 위치에 있다면 실행할 때 `--boost-include`로 지정한다. 다른 버전 헤더는 혼합 링크를 방지하기 위해 거절한다.

```bash
validation_deps=/tmp/longshort_refactor_deps
mkdir -p "$validation_deps"
while read -r name checksum url; do
  curl --fail --location --retry 2 "$url" -o "$validation_deps/$name.tar.gz"
  printf '%s  %s\n' "$checksum" "$validation_deps/$name.tar.gz" | sha256sum --check -
  tar -xzf "$validation_deps/$name.tar.gz" -C "$validation_deps"
done <<'SOURCES'
serialization-boost-1.85.0 d075339a6104f75c60c45b27160be87a50fce49400369a00d865f26da536646d https://codeload.github.com/boostorg/serialization/tar.gz/refs/tags/boost-1.85.0
googletest-v1.14.0 8ad598c73ad796e0d8280b082cebd82a630d73e73cd3c70057938a6501bba5d7 https://codeload.github.com/google/googletest/tar.gz/refs/tags/v1.14.0
jsoncpp-1.9.5 f409856e5920c18d0c2fb85276e24ee607d2a09b5e7d5f0a371368903c275da2 https://codeload.github.com/open-source-parsers/jsoncpp/tar.gz/refs/tags/1.9.5
pugixml-v1.14 610f98375424b5614754a6f34a491adbddaaec074e9044577d965160ec103d2e https://codeload.github.com/zeux/pugixml/tar.gz/refs/tags/v1.14
SOURCES
```

저장소 루트에서 실행한다.

```bash
python3 cpp-engine/tools/run_logic_tests.py \
  --deps-dir /tmp/longshort_refactor_deps \
  --build-dir /tmp/longshort_refactor_standalone \
  --boost-include /usr/local/include \
  --jobs 4
```

고정된 의존성 오브젝트는 같은 빌드 디렉터리에서 재사용하고 제품/테스트 소스는 매번 다시 컴파일한다. 의존성 소스나 헤더를 수정한 경우 새 `--build-dir`을 사용한다. `--prepare-only`로 의존성만 미리 빌드할 수 있다.

## 서버 호출부 구문 검사

서버의 실제 의존성인 [Drogon v1.9.8](https://github.com/drogonframework/drogon/tree/v1.9.8)과 그 버전이 고정한 [Trantor 8bf280b](https://github.com/an-tao/trantor/tree/8bf280ba043eb77c3eb02121065e0891a9a4f431)를 사용한다. API를 흉내 낸 대체 헤더는 사용하지 않는다.

준비한 소스 디렉터리는 `/tmp/longshort_refactor_deps/drogon-1.9.8` 및 `/tmp/longshort_refactor_deps/trantor-8bf280ba043eb77c3eb02121065e0891a9a4f431`이고, Drogon의 `trantor` 하위 경로는 해당 Trantor 소스에 연결했다. 공식 소스의 CMake로 생성한 헤더 위치는 `/tmp/longshort_refactor_build/drogon-config`이다.

재다운로드가 필요한 경우 공식 소스와 SHA-256은 다음과 같다.

| 소스 | SHA-256 |
|---|---|
| [Drogon v1.9.8 archive](https://codeload.github.com/drogonframework/drogon/tar.gz/refs/tags/v1.9.8) | `62332a4882cc7db1c7cf04391b65c91ddf6fcbb49af129fc37eb0130809e0449` |
| [Trantor pinned archive](https://codeload.github.com/an-tao/trantor/tar.gz/8bf280ba043eb77c3eb02121065e0891a9a4f431) | `f32272fe923bdbc20143fe5de4b66908747b25b7e32e068c0e83f58171197990` |

이 머신에서 사용한 configure 명령은 아래와 같다. 헤더 생성만 수행하며 라이브러리 설치는 하지 않는다. JsonCpp/UUID/Zlib의 기존 시스템 경로를 읽는다.

```bash
cmake -S /tmp/longshort_refactor_deps/drogon-1.9.8 \
  -B /tmp/longshort_refactor_build/drogon-config \
  -DCMAKE_CXX_STANDARD=17 \
  -DBUILD_CTL=OFF -DBUILD_EXAMPLES=OFF -DBUILD_ORM=OFF \
  -DBUILD_TESTING=OFF -DBUILD_BROTLI=OFF -DBUILD_YAML_CONFIG=OFF \
  -DBUILD_C-ARES=OFF -DTRANTOR_USE_TLS=none \
  -DJSONCPP_INCLUDE_DIRS=/tmp/longshort_refactor_deps/jsoncpp-1.9.5/include \
  -DJSONCPP_LIBRARIES=/usr/lib/x86_64-linux-gnu/libjsoncpp.so.25 \
  -DUUID_INCLUDE_DIRS=/home/jgo/61/jgo/miniconda3/include/uuid \
  -DUUID_LIBRARIES=/usr/lib/x86_64-linux-gnu/libuuid.so.1
```

위 테스트 실행 명령에 다음 두 인자를 추가하면 `main.cpp`도 `-fsyntax-only`로 검사한다.

```text
--drogon-source /tmp/longshort_refactor_deps/drogon-1.9.8
--drogon-build /tmp/longshort_refactor_build/drogon-config
```

## 최종 실행 결과

제품 소스 18개와 테스트 소스 17개, 총 35개 translation unit을 다시 컴파일했다. 리뷰에서 추가한 observer 계약 테스트까지 포함한 최종 실행에서 **130개 테스트, 28개 suite가 모두 통과**했다. 최종 테스트 실행 시간은 약 3.5초이며 컴파일 시간은 별도다. 마지막 GA simStart 입력 타입 검사 보완 후 main.cpp 구문·타입 검사도 다시 통과했다.

| 검증 | 결과 |
|---|---|
| 체크포인트/기존 archive fixture | 6개 통과 |
| NumPy golden 교차 검증 | 8개 통과 |
| 신규 JSON 키/plot offset/분포 계약 | 7개 통과 |
| 평균·최소 보유 종목 수와 실제 보유한 bar 수 구분 | 통과 |
| 기존 D0/D1 손익·비용·결측 종가 및 두 실행 경로 | 통과 |
| raw observer 호출·warmup NaN·정규화 전 원본 신호·replay 비수집 계약 | 통과 |
| XML, 모듈, SIMD, GA, 2,000알파 스트레스 | 통과 |
| 실제 Drogon/Trantor 헤더를 이용한 main.cpp 구문·타입 검사 | 통과 |
| 변경한 계산/GA/serializer 구현 7개, -Wall -Wextra -Wunused | 오류 0, 경고 0 |

원본 checkpoint fixture의 SHA-256은 baseline과 동일하다.

```text
f3ab6590bef02d0a4797a4e26193cd2f7a0ae44eb1b0aa5de77cd73a8ad9501c
```

실행 로그는 `/tmp/longshort_refactor_standalone/test_output.log`, 구조화된 결과는 `test_results.json`, 실제 compile 명령은 `engine_objects/compile_results.json`에 있다. 서버 검사는 `server_syntax_check.json`, 미사용 변수 경고 검사는 `unused_warning_check.json`에 기록했다.

## 독립 코드 리뷰

이번 리팩토링 직전의 미커밋 상태를 보존한 `/tmp/longshort_refactor_baseline_20260909`와 비교했다. 이전 논리 수정까지 이번 리팩토링의 효과로 합산하지 않았다.

- `PortfolioSimulation`의 공통 계산은 종가 검증, 정규화, 기존 보유 손익, 편도 거래량 비용, 통계 계산, 상태 교체 순서를 유지한다. mode 1의 두 swap과 mode 0의 단일 swap은 기존과 같다. 제거된 mode 0의 이전 목표 벡터는 해당 분기에서 읽히지 않았다.
- 직접 신호 입력은 행 데이터를 읽는 동안 원본 행렬이 살아 있다. 수집 경로의 임시 신호 배열을 지우거나 외부 포인터를 장기간 보관하지 않는다.
- `avg_position_count`, `min_position_count`, `active_bar_count`, `position_count`는 실제 집계 의미와 맞는다. 구형 JSON 키는 serializer 경계에서 유지되며 계산 내부 이름과 분리됐다.
- 누적손익 임시 배열 제거는 합산 순서를 유지한다. 초기 고점 0과 낙폭의 음수 부호도 유지된다.
- 분포 계산의 2회 순회는 이전의 유한값·절댓값 임계값·50개 bin·상수 신호 처리를 유지한다. offset 방식의 plot은 같은 시작 위치를 날짜와 신호 행에 적용한다.
- 사용되지 않던 GA metric 선택 함수/설정과 초기값만 담던 `best_metrics` 제거는 실제 평가 callback에 영향을 주지 않는다. 마지막 세대 이후 미사용 개체 생성 생략은 다음 평가가 없는 시점에만 적용된다.

현재 비교 범위에서 기능 회귀를 발견하지 못했다. 데이터 로딩/YAML 및 서버 링크·HTTP 실행 검증은 하지 않았다. 멀티알파 combined 합산 순서/가중치 의미는 이번 리팩토링에서 변경하지 않았다.

검토 중 남긴 후속 후보는 단일 알파 경로의 `single_sim_out` 수명이다. 그 구조는 이후 신호/날짜만 읽는데도 원래 수익률/회전율/비중/종목 수 배열을 응답 작성이 끝날 때까지 보유한다. 실제 사용하는 필드로 보관 범위를 줄일 여지가 있으나 이번 검증된 패치의 필수 수정으로 보지는 않았다.
