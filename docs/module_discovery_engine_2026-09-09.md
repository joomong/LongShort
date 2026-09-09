# 자동 모듈 탐색 — C++ 등록 및 속성 계약

이 변경은 모듈 파일에서 생성한 factory를 기존 XML compiler에 연결한다. 기존 모듈의 계산·기본 파라미터·손익·JSON 계약은 변경하지 않는다.

## 등록 연결

- `cpp-engine/src/compiler/dag_builder.h`에 `register_discovered_modules(ModuleRegistry&)`를 선언했다. 함수 구현은 모듈 생성기가 출력하는 C++ 파일에 있으며 빌드가 포함한다. 수동 empty fallback이나 수동 custom 태그 목록은 두지 않았다.
- `ModuleRegistry::default_registry()`는 기존 built-in factory를 생성한 후 생성된 등록 함수를 한 번 호출한다. 함수 지역 static 초기화이므로 동시 첫 호출에서도 초기화 과정이 겹치지 않는다.
- `register_module`은 이미 등록된 태그를 덮어쓰지 않고 `CompileError`를 반환한다. 중복 등록 실패 후에도 원래 factory가 남는다.
- compiler가 직접 처리하는 `data/signal/event/choice/shuffle` 등은 registry entry가 아니므로 생성기가 예약 태그로 별도 거절해야 한다. 일반 registry 중복 검사는 built-in factory와 custom factory 사이의 충돌을 막는다.

## 자동 발견 모듈의 속성 읽기

`cpp-engine/src/compiler/module_attributes.h`는 생성된 factory가 이용하는 header-only helper다.

```cpp
module_attributes::validate_keys(attrs, {"count", "factor"}, "scale");
module_attributes::parse_int(raw, "scale", "count");
module_attributes::parse_float(raw, "scale", "factor");
```

- 앞뒤 ASCII 공백은 허용한다. 숫자 뒤에 쓰레기 문자열이 붙거나 정수에 소수/지수 표기가 붙으면 오류다.
- 정수는 전체 문자열을 10진 int로 변환하고 int 범위 밖의 값은 거절한다.
- float 속성은 double로 읽고 전체 문자열 소비와 finite 여부를 확인한다. NaN/Inf, 숫자 변환 overflow, 부분 파싱 결과는 거절한다. std::stod가 범위 오류로 보고하는 값도 거절한다.
- 오류에는 XML 모듈 태그·속성 이름·입력값을 넣어 잘못된 파라미터 위치를 알 수 있게 했다.
- 선언하지 않은 속성은 거절한다. 공통 `input="saved_signal"`은 compiler가 먼저 처리하는 속성이므로 자동 모듈에서도 허용한다. 따라서 public 파라미터 이름 `input`은 생성기에서 충돌로 처리해야 한다.
- 입력 XML에서 생략한 속성은 생성된 factory가 대입하지 않는다. 모듈 멤버의 C++ 기본값을 보존한다. helper는 생략된 값을 임의의 0이나 1로 채우지 않는다.

기존 built-in 파라미터 파서는 helper로 교체하지 않았다. tsmean 등의 days 기본값 20, delay 기본값 1, rank/tsrank의 bool 문자열 처리, event의 별도 파라미터 규칙은 이전대로 유지한다. 새 custom IntParameter 속성은 정수 문자열로 지정한다.

## 테스트

- 새 `test_module_attributes.cpp` 7개가 통과했다. 정수 경계·잘못된 값/overflow, finite 실수, NaN/Inf/부분파싱, 오류 문맥, 허용/미지정/unknown 속성, 공통 input을 검사한다.
- `test_xml_compiler.cpp`에 registry 중복 태그의 원래 factory 보존과 built-in rank 덮어쓰기 방지 테스트 2개를 추가했다. 생성 C++와 함께 전체 compiler 검증에 포함한다.
- 속성 helper 단독 g++ C++17/O2/Wall/Wextra 빌드에 경고가 없었으며 담당 diff의 공백 검사도 통과했다.

생성 파일의 실제 탐색·파라미터 추출·빌드 자동화와 모듈 계산 end-to-end 검증은 생성기/빌드 담당의 결과와 함께 취합한다. 이 문서의 단독 테스트 통과는 실행 중 hot reload 지원을 뜻하지 않는다.
