# 개인 C++ 모듈

개인 `.h` 또는 `.hpp` 모듈은 `/home/jgo/longshort_workspace/modules/`에 넣습니다. 이 문서는 공용 모듈 작성 안내이며, 기본 모듈은 플랫폼의 `cpp-engine/src/modules/`에 있습니다. workspace에서 공용 실행기를 호출합니다.

```bash
cd /home/jgo/longshort_workspace
python3 /home/jgo/longshort_platform/cpp-engine/tools/run_engine.py --config config.yaml --port 50007
```

실행기가 파일을 자동 발견하고 필요한 C++ 빌드를 수행한 뒤 엔진을 시작합니다. 중앙 registry나 CMake 소스 목록은 수정하지 않습니다. 파일을 수정/삭제한 경우 다음 프로그램 시작에 반영됩니다.

- 파일당 기본 생성 가능하고 `Module`을 직접 public 상속하는 클래스 하나를 작성합니다.
- 파일명이 `MySignal.h`이면 XML 태그는 `<mysignal/>`입니다.
- XML 숫자 속성은 public `IntParameter`/`FloatParameter` 멤버로 선언합니다. 생략된 속성은 C++ 초기값을 유지합니다. `FloatParameter`는 float로 저장하며 속성 글자를 float로 바로 읽습니다(평문 소수만, float 범위 밖은 오류). 기본값은 `1e-12f`처럼 float 리터럴로 씁니다. `double x = param;`, `param * 2.0`은 그대로 동작하지만 `param == 0.1`처럼 double과 직접 비교하면 float 값으로 비교됩니다.
- 모듈 출력의 ±inf는 다음 모듈로 넘어가기 전에 NaN으로 바뀝니다.
- `init()` 및 `calculate(unsigned, SignalVector&)`를 구현합니다. 상태가 있다면 `init()`에서 초기화합니다.
- 일반 내부 상태는 `int`, `double`, vector 등으로 두고 XML parameter wrapper와 구분합니다.
- 필요하면 public `const DataPanel* panel = nullptr;`을 선언하여 현재 데이터를 참조합니다.
- `.cpp`/`.cc`/`.cxx` 구현 파일도 이 폴더 아래에서 자동으로 빌드됩니다.
- 점으로 시작하는 파일/폴더는 탐색에서 제외됩니다. 예를 들어 `.archive/`로 옮기면 비활성화됩니다.
- 내장 태그/타입과 중복된 이름은 오류입니다. 다른 연구자 모듈과 충돌하지 않도록 namespace/클래스 이름을 구분하세요.

기본 모듈을 개인적으로 변형할 때도 새 클래스·파일·태그 이름으로 workspace에 작성합니다. 개인 설정과 모듈·알파는 보관하고, 자동 생성되는 `.longshort/`는 Git 및 workspace 복사 대상에서 제외합니다.

[Affine 예제](../cpp-engine/examples/modules/Affine.h)를 복사하면 `<affine factor="-1" offset="2"/>`로 사용할 수 있습니다. 모듈 헤더는 기존 STAR 스타일처럼 기본 타입을 `ModulePrelude.h`에서 받습니다.

최초 C++ 빌드 환경과 지원 문법, PM에게 넘길 항목은 [자동 모듈 사용법](../docs/build_and_validation.md)에 설명합니다.
