# 사용자 모듈 자동 발견과 기동

`config.yaml` 옆의 `modules/`에 모듈 헤더를 넣고 아래 프로그램을 시작한다. 중앙 등록 목록이나 CMakeLists를 수정할 필요가 없다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/config.yaml
```

기동 프로그램은 설정 파일을 확인하고, 해당 사용자의 모듈 디렉터리로 CMake configure → 증분 빌드 → 서버 시작을 순서대로 실행한다. 모듈 검색·생성·컴파일 중 하나라도 실패하면 오류를 표시하고 종료한다. 남아 있는 이전 서버 실행 파일로 계속 진행하지 않는다.

기본 경로는 설정 파일 옆의 `modules/`와 `.longshort/build/`이다. 두 폴더가 없으면 생성한다. 여러 사용자가 같은 엔진 소스를 사용하더라도 각자의 설정 파일과 모듈·빌드 디렉터리로 분리된다.

```bash
python3 cpp-engine/tools/run_engine.py \
  --config /research/alice/config.yaml \
  --modules-dir /research/alice/my_modules \
  --build-dir /research/alice/.longshort/build \
  --port 8001
```

`--build-only`는 같은 발견·빌드 단계를 실행하고 서버 시작 전에 종료한다. `--jobs`와 `--build-type`으로 컴파일 병렬도 및 구성도 선택할 수 있다. 기동 프로그램은 현재 POSIX 환경에서 동작한다. 실행 중 모듈 파일을 바꾼 경우 다음 프로그램 시작 때 반영된다.

## 빌드 연결

- `LONGSHORT_MODULE_DIR` CMake 변수로 사용자 모듈 위치를 지정한다. 직접 CMake를 사용하면 기본값은 저장소 루트의 `modules/`이다.
- `cmake/DiscoverModules.cmake`는 `.h`, `.hpp`, `.cpp`, `.cc`, `.cxx`를 재귀적으로 검색한다. 추가 구현 소스도 자동으로 엔진에 포함한다. 상대경로 중 점으로 시작하는 파일/폴더는 생성기와 동일하게 제외하므로 `.archive/`로 옮긴 모듈과 구현 파일은 비활성화된다.
- 실제 `tools/generate_modules.py --modules-dir DIR --output FILE`를 매 빌드 때 실행한다. 생성한 `register_discovered_modules(ModuleRegistry&)`가 엔진의 기본 registry를 구성한다. 생성기를 우회하는 수동 등록 fallback은 없다.
- 생성기가 동일 내용의 파일을 다시 쓰지 않으므로 변경이 없으면 C++ 컴파일·링크를 생략한다. `CONFIGURE_DEPENDS`가 파일 추가/삭제를 감지하므로 이미 configure한 뒤 `cmake --build`만 실행해도 모듈 목록이 갱신된다.
- 생성된 registry는 해당 빌드 디렉터리의 `generated/engine_discovered_modules.cpp`에만 저장한다. 사용자 소스와 공통 엔진 소스에 생성 코드를 쓰지 않는다.
- 빌드 디렉터리를 모듈 디렉터리 안에 두는 구성은 거절한다. CMake가 만든 소스를 다시 사용자 모듈로 검색하는 일을 방지한다.
- 동일 빌드 디렉터리의 동시 configure/build는 잠금으로 거절한다. 다른 소스 경로에서 만든 CMakeCache는 재사용·삭제하지 않고 새 `--build-dir`을 지정하라는 오류를 표시한다.

관련 동작은 CMake의 [CONFIGURE_DEPENDS](https://cmake.org/cmake/help/latest/command/file.html#filesystem)와 [custom target/BYPRODUCTS](https://cmake.org/cmake/help/latest/command/add_custom_target.html)를 사용한다. Make와 Ninja에서 실제 증분 동작을 검사했다.

## 최초 빌드 환경

모듈 추가 때 빌드 파일을 편집하는 작업은 없어졌지만, C++ 컴파일러·CMake와 프로젝트 의존성은 필요하다. 기존 Arrow/Parquet CMake 패키지를 우선 사용하며, PyArrow의 번들 라이브러리를 이용할 때는 아래처럼 명시한다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/config.yaml \
  --cmake-define LONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  --cmake-define CMAKE_PREFIX_PATH=/path/to/dependency-prefix
```

`--cmake-define NAME=VALUE`는 CMake에 독립된 인자로 전달한다. 필요한 의존성 설정은 빌드 디렉터리의 CMakeCache에 저장되므로 이후 동일 디렉터리를 사용할 때 반복할 필요가 없다. 모듈 위치·Python 생성기·빌드 타입은 기동 프로그램이 지정한다.

- CMake 최소 버전은 기존처럼 3.20이다. 3.30에서 도입된 `CMP0167`은 존재할 때만 설정한다.
- Boost는 CONFIG 패키지로 가져와 헤더와 serialization 라이브러리를 함께 선택한다.
- `LONGSHORT_PYARROW_PYTHON`은 해당 Python에 설치된 PyArrow의 헤더와 Arrow/Parquet 공유 라이브러리를 한 묶음으로 사용한다. 현재 Linux/macOS 공유 라이브러리 형태를 지원한다.
- Arrow 18과 최신 API의 reader 생성 차이는 `FileReaderBuilder`로 처리했다. 컬럼 선택, 타입 해석, 읽는 데이터와 손익 계산은 이 변경에 포함하지 않았다.

## 검증

자동화 테스트:

```bash
python3 -m unittest discover -s cpp-engine/tests -p test_run_engine.py -v
python3 -m unittest discover -s cpp-engine/tests -p test_module_build.py -v
```

기동 프로그램 테스트는 경로 공백, 빌드 실패 후 이전 실행 파일 미사용, 실행 파일 누락, 설정·모듈 경로 오류, 다른 저장소의 캐시, 동시 빌드 잠금, 잘못된 옵션과 모듈/빌드 경로 중첩을 확인한다.

별도 최소 C++ 프로젝트로 실제 Make/Ninja를 실행하여 헤더 추가·수정·삭제, 추가 `.cpp`의 컴파일/삭제, 생성·컴파일 오류, 생성 파일 삭제 후 복구 및 변경 없는 실행의 registry/binary mtime 유지를 검증한다. 이 테스트는 빌드 그래프를 분리해 검사하며 모듈 문법 검사는 실제 생성기의 테스트가 담당한다.

`run_logic_tests.py`도 실제 생성기를 실행하고 생성된 registry와 사용자 구현 소스를 컴파일한다. `--with-tool-tests`를 추가하면 generator·launcher·증분 빌드 Python 검사도 먼저 실행하며, 결과를 빌드 디렉터리의 `python_tool_test_results.json`에 기록한다. 이미 의존성이 준비된 디렉터리에서 `--prepare-only --with-tool-tests`를 조합하면 C++ suite를 다시 실행하지 않고 도구 검사만 확인할 수 있다.

CMake의 `LONGSHORT_BUILD_TESTS=ON`에서는 `test_generate_modules`, `test_run_engine`, `test_module_build`를 각각 CTest에 등록한다. Python 검사만 재실행하려면 다음 명령을 사용한다.

```bash
ctest --test-dir /path/to/build \
  -R '^test_(generate_modules|run_engine|module_build)$' --output-on-failure
```

검증 완료 결과:

- 실제 생성기의 빈 registry를 포함하여 core 테스트 **139개 모두 통과**했다. 기존 130개와 신규 속성 helper 7개, registry 충돌 2개를 포함한다.
- 생성기 테스트 **18개**, 기동 프로그램 테스트 **11개**, 실제 Make/Ninja 증분 통합 테스트 **1개**가 모두 통과했다. 검증 도구의 `--with-tool-tests`와 실제 CTest 양쪽에서 같은 3개 suite가 모두 통과했다. 독립 리뷰에서 발견한 모듈/빌드 경로 중첩과 숨김 companion 소스 제외 규칙을 수정하고 회귀 검사했다.
- `run_engine.py --build-only`로 실제 서버 전체 configure·컴파일·링크를 완료했다. 결과는 `/tmp/longshort_module_server/build/longshort-server`이다.
- 서버 빌드에는 PyArrow 18.1.0, Boost 1.85.0 CONFIG 패키지, JsonCpp 1.9.5, yaml-cpp 0.8.0, pugixml 1.14, Drogon 1.9.8 및 그 버전의 Trantor를 사용했다. 모든 다운로드 소스는 해당 프로젝트의 공식 저장소에서 가져왔다. 시스템 패키지를 수정하지 않았다.
- 별도 독립 리뷰 담당자가 같은 CMake 빌드 경로로 실제 사용자 모듈 XML/HTTP 검사 **12개**를 통과했다. 기본값·속성 override·입력 signal route·DataPanel 바인딩·원본 신호/지표 parity·헤더 변경/이동/삭제·UTF-8 경로 및 별도 cpp·숨김 backup 제외를 포함한다. 결과는 `/tmp/longshort_peer_e2e_20260909_cj83r_db/results.json`과 `additional_results.json`에 기록돼 있다.
