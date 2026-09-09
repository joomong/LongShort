# 개인 C++ 모듈

이 폴더에 `.h` 또는 `.hpp` 모듈을 넣고 저장소 루트에서 다음 명령으로 실행합니다.

```bash
python3 cpp-engine/tools/run_engine.py --config config.yaml
```

실행기가 파일을 자동 발견하고 필요한 C++ 빌드를 수행한 뒤 엔진을 시작합니다. 중앙 registry나 CMake 소스 목록은 수정하지 않습니다. 파일을 수정/삭제한 경우 다음 프로그램 시작에 반영됩니다.

- 파일당 기본 생성 가능하고 `Module`을 직접 public 상속하는 클래스 하나를 작성합니다.
- 파일명이 `MySignal.h`이면 XML 태그는 `<mysignal/>`입니다.
- XML 숫자 속성은 public `IntParameter`/`FloatParameter` 멤버로 선언합니다. 생략된 속성은 C++ 초기값을 유지합니다.
- `init()` 및 `calculate(unsigned, SignalVector&)`를 구현합니다. 상태가 있다면 `init()`에서 초기화합니다.
- 일반 내부 상태는 `int`, `double`, vector 등으로 두고 XML parameter wrapper와 구분합니다.
- 필요하면 public `const DataPanel* panel = nullptr;`을 선언하여 현재 데이터를 참조합니다.
- `.cpp`/`.cc`/`.cxx` 구현 파일도 이 폴더 아래에서 자동으로 빌드됩니다.
- 점으로 시작하는 파일/폴더는 탐색에서 제외됩니다. 예를 들어 `.archive/`로 옮기면 비활성화됩니다.
- 내장 태그/타입과 중복된 이름은 오류입니다. 다른 연구자 모듈과 충돌하지 않도록 namespace/클래스 이름을 구분하세요.

[Affine 예제](../cpp-engine/examples/modules/Affine.h)를 복사하면 `<affine factor="-1" offset="2"/>`로 사용할 수 있습니다. 모듈 헤더는 기존 STAR 스타일처럼 기본 타입을 `ModulePrelude.h`에서 받습니다.

최초 C++ 빌드 환경과 지원 문법, PM에게 넘길 항목은 [자동 모듈 사용법](../docs/module_discovery_2026-09-09.md)에 설명합니다.
