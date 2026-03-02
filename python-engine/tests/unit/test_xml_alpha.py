"""XML Alpha Compiler 테스트.

MVP 테스트 + Phase 1 파이프라인 + Phase 2 분기 + 엣지 케이스.
Phase 1/2 미구현 기능은 xfail로 표시.
"""
import pytest

from api.xml_alpha import compile, XmlCompileError


def one(xml: str):
    """Helper: compile a non-branching XML and return the single Signal."""
    alphas = compile(xml)
    assert isinstance(alphas, list)
    assert len(alphas) == 1
    return alphas[0]
from framework.modules.sources import Price
from framework.modules.delay import Delay
from framework.modules.rank import Rank
from framework.modules.zscore import ZScore
from framework.modules.tszscore import TSZScore
from framework.modules.tsrank import TSRank
from framework.modules.tsmean import TsMean


# ── MVP (현재 구현) ────────────────────────────────────────────

class TestMVPDataExpr:
    """TC-001~005: <data expr="..."/> 기본 컴파일."""

    @pytest.mark.parametrize("expr", ["open", "high", "low", "close", "volume"])
    def test_compile_data_expr_supported(self, expr: str):
        compiled = one(f'<data expr="{expr}"/>')
        assert isinstance(compiled, Price)
        assert compiled.column == expr


class TestMVPDelay:
    """TC-015: delay 속성 래핑."""

    def test_compile_data_with_delay_wraps_delay(self):
        compiled = one('<data expr="close" delay="2"/>')
        assert isinstance(compiled, Delay)
        assert compiled.p == 2
        assert isinstance(compiled.x, Price)
        assert compiled.x.column == "close"

    def test_delay_zero(self):
        """delay=0 — Delay 래핑하되 period 0."""
        compiled = one('<data expr="close" delay="0"/>')
        assert isinstance(compiled, Delay)
        assert compiled.p == 0

    def test_delay_negative(self):
        """음수 delay — 현재 파서는 int 변환만 하므로 동작은 하나, 경고가 바람직."""
        compiled = one('<data expr="close" delay="-1"/>')
        assert isinstance(compiled, Delay)
        assert compiled.p == -1


class TestMVPErrors:
    """TC-023~040: 에러 핸들링."""

    def test_compile_invalid_xml_raises(self):
        with pytest.raises(XmlCompileError, match="Invalid XML"):
            compile('<data expr="close"')

    def test_compile_wrong_root_raises(self):
        with pytest.raises(XmlCompileError, match="Root element must be <data>"):
            compile('<alpha expr="close"/>')

    def test_compile_unsupported_expr_raises(self):
        with pytest.raises(XmlCompileError, match="Unsupported expr"):
            compile('<data expr="adj_close"/>')

    def test_compile_invalid_delay_raises(self):
        with pytest.raises(XmlCompileError, match="Invalid delay"):
            compile('<data expr="close" delay="abc"/>')

    def test_compile_missing_expr_raises(self):
        """expr 속성이 없는 경우."""
        with pytest.raises(XmlCompileError, match="Unsupported expr"):
            compile('<data/>')

    def test_compile_empty_expr_raises(self):
        """expr="" 빈 문자열."""
        with pytest.raises(XmlCompileError, match="Unsupported expr"):
            compile('<data expr=""/>')


# ── 엣지 케이스 ────────────────────────────────────────────────

class TestEdgeCases:
    """EC-XML-001 ~ EC-XML-008."""

    def test_ec001_empty_xml_string(self):
        """EC-XML-001: 빈 XML 문자열."""
        with pytest.raises(XmlCompileError):
            compile("")

    def test_ec001_whitespace_only(self):
        """빈 공백만 있는 XML."""
        with pytest.raises(XmlCompileError):
            compile("   ")

    def test_ec002_pipeline_without_data(self):
        """EC-XML-002: <data> 없이 시작하는 파이프라인 → 에러.
        MVP: wrong root tag 에러, Phase 1 이후: 첫 태그가 data가 아님 에러."""
        with pytest.raises(XmlCompileError):
            compile('<rank/>')

    def test_ec003_unsupported_tag_in_pipeline(self):
        """EC-XML-003: 지원하지 않는 태그 → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: 미지원 태그 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><neutralize/>')

    def test_ec004_days_missing(self):
        """EC-XML-004: days 속성 누락 → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: days 누락 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tszscore/>')

    def test_ec005_range_parse_error(self):
        """EC-XML-005: range 표기 오류 → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 2 이후: range 파싱 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tszscore days="20...abc...60"/>')

    def test_ec006_max_alpha_count_exceeded(self):
        """EC-XML-006: 조합 수 폭발 시 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 2 이후: limit 초과 에러."""
        exprs = " | ".join(["close"] * 10)
        xml = f'<data expr="{exprs}"/><tszscore days="1...1...200"/>'
        with pytest.raises(XmlCompileError):
            compile(xml)

    def test_ec008_backward_compat_single_data(self):
        """EC-XML-008: 기존 MVP XML 하위 호환."""
        compiled = one('<data expr="close"/>')
        assert isinstance(compiled, Price)
        assert compiled.column == "close"


# ── Phase 1: 파이프라인 (TC-XP-001 ~ TC-XP-009) ───────────────

class TestPhase1Pipeline:
    """파이프라인 파싱 — 다중 태그 순차 처리."""

    def test_xp001_single_data_compat(self):
        """TC-XP-001: 단일 data (기존 호환)."""
        compiled = one('<data expr="close"/>')
        assert isinstance(compiled, Price)
        assert compiled.column == "close"

    def test_xp002_data_delay(self):
        """TC-XP-002: data + delay."""
        compiled = one('<data expr="close" delay="1"/>')
        assert isinstance(compiled, Delay)
        assert isinstance(compiled.x, Price)

    def test_xp003_data_rank_pipeline(self):
        """TC-XP-003: data + rank 파이프라인."""
        result = one('<data expr="close"/><rank/>')
        sig = result
        assert isinstance(sig, Rank)
        assert isinstance(sig.x, Price)
        assert sig.x.column == "close"

    def test_xp004_data_rank_tszscore(self):
        """TC-XP-004: data + rank + tszscore."""
        result = one(
            '<data expr="close"/><rank/><tszscore days="20"/>'
        )
        sig = result
        assert isinstance(sig, TSZScore)
        assert sig.window == 20
        assert isinstance(sig.x, Rank)
        assert isinstance(sig.x.x, Price)

    def test_xp005_data_tsmean(self):
        """TC-XP-005: data + tsmean."""
        result = one(
            '<data expr="volume"/><tsmean days="10"/>'
        )
        sig = result
        assert isinstance(sig, TsMean)
        assert sig.window == 10
        assert isinstance(sig.x, Price)
        assert sig.x.column == "volume"

    def test_xp006_data_signal_save(self):
        """TC-XP-006: data + signal 저장 (결과 동일, signal은 메타정보만)."""
        result = one(
            '<data expr="close"/><signal id="raw"/>'
        )
        sig = result
        assert isinstance(sig, Price)
        assert sig.column == "close"

    def test_xp007_data_delay_explicit_tag(self):
        """delay를 <delay days="3"/>로 별도 태그로 지정."""
        result = one(
            '<data expr="close"/><delay days="3"/>'
        )
        sig = result
        assert isinstance(sig, Delay)
        assert sig.p == 3
        assert isinstance(sig.x, Price)

    def test_xp008_days_missing_error(self):
        """TC-XP-008: tszscore에 days 누락 시 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: days 누락 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tszscore/>')

    def test_xp009_unsupported_tag_error(self):
        """TC-XP-009: 미지원 태그 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: 미지원 태그 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><neutralize/>')

    def test_data_zscore_pipeline(self):
        """data + zscore 파이프라인."""
        result = one('<data expr="close"/><zscore/>')
        sig = result
        assert isinstance(sig, ZScore)
        assert isinstance(sig.x, Price)

    def test_data_tsrank_pipeline(self):
        """data + tsrank 파이프라인."""
        result = one(
            '<data expr="close"/><tsrank days="10"/>'
        )
        sig = result
        assert isinstance(sig, TSRank)
        assert sig.window == 10

    def test_long_pipeline(self):
        """data + delay + rank + tszscore + tsmean 긴 파이프라인."""
        result = one(
            '<data expr="close" delay="1"/>'
            '<rank/>'
            '<tszscore days="20"/>'
            '<tsmean days="5"/>'
        )
        sig = result
        assert isinstance(sig, TsMean)
        assert sig.window == 5


# ── Phase 2: 분기 탐색 (TC-XP-010 ~ TC-XP-017) ───────────────

# ── Phase 3: 고급 탐색 (shuffle, choice bind="same") ───────────────

class TestPhase3AdvancedVarEvent:
    def test_xp3_020_var_substitution_in_attr(self):
        results = compile(
            '<data expr="close"/>'
            '<var d="1|2"/>'
            '<delay days="$[d]"/>'
        )
        assert len(results) == 2
        assert isinstance(results[0], Delay)
        assert isinstance(results[1], Delay)

    def test_xp3_021_missing_var_reference_causes_error(self):
        """TC-XP3-021: 미정의 var 참조 → $[x] 문자열이 그대로 남아 days 파싱 실패."""
        xml = (
            '<data expr="close"/>'
            '<delay days="$[undefined_var]"/>'
        )
        with pytest.raises(XmlCompileError):
            compile(xml)

    def test_xp3_var_with_range(self):
        """var 값에 range 표기 사용 → 확장됨."""
        results = compile(
            '<data expr="close"/>'
            '<var d="1...3"/>'
            '<delay days="$[d]"/>'
        )
        assert len(results) == 3

    def test_xp3_030_event_requires_named_signals(self):
        xml = (
            '<data expr="close"/>'
            '<event signal="base" trigger="trig" threshold="0" maxHolddays="2" betAll="1"/>'
        )
        with pytest.raises(XmlCompileError, match="Unknown signal id"):
            compile(xml)


class TestPhase2Branching_:
    """Phase 2 분기 탐색 테스트 (pipe, range, choice)."""

    def test_xp010_pipe_2way(self):
        """TC-XP-010: pipe 2-way."""
        results = compile('<data expr="close | volume"/>')
        assert len(results) == 2

    def test_xp011_pipe_3way(self):
        """TC-XP-011: pipe 3-way."""
        results = compile('<data expr="close | volume | open"/>')
        assert len(results) == 3

    def test_xp012_range_basic(self):
        """TC-XP-012: range 기본 — days="20...10...40" → 3개 (20,30,40)."""
        results = compile(
            '<data expr="close"/><tszscore days="20...10...40"/>'
        )
        assert len(results) == 3

    def test_xp013_range_step1(self):
        """TC-XP-013: range step=1 — days="3...5" → 3개 (3,4,5)."""
        results = compile(
            '<data expr="close"/><tsmean days="3...5"/>'
        )
        assert len(results) == 3

    def test_xp014_pipe_plus_range(self):
        """TC-XP-014: pipe + range 조합 → 2×3=6."""
        results = compile(
            '<data expr="close | volume"/><tszscore days="20...10...40"/>'
        )
        assert len(results) == 6

    def test_xp015_choice_basic(self):
        """TC-XP-015: choice 기본 — 2개 대안."""
        results = compile(
            '<data expr="close"/>'
            '<choice><rank/><zscore/></choice>'
        )
        assert len(results) == 2

    def test_xp016_choice_with_range(self):
        """TC-XP-016: choice + range — (6+3) = 9개 대안."""
        results = compile(
            '<data expr="close"/>'
            '<choice>'
            '  <tsmean days="5...10"/>'
            '  <delay days="1...3"/>'
            '</choice>'
        )
        assert len(results) == 9

    def test_xp017_max_alpha_exceeded(self):
        """TC-XP-017: 최대 알파 수 초과.
        MVP: 다중 태그 XML 파싱 실패, Phase 2 이후: limit 초과 에러."""
        exprs = " | ".join(["close"] * 50)
        xml = f'<data expr="{exprs}"/><tszscore days="1...1...50"/>'
        with pytest.raises(XmlCompileError):
            compile(xml)


# ── Phase 1 + Backtester 통합 ─────────────────────────────────

class TestPhase1Integration:
    """TC-XP-018, TC-XP-019: 파이프라인 알파로 Backtester 실행."""

    def test_xp018_pipeline_alpha_backtest(self):
        """TC-XP-018: 파이프라인 알파로 Backtester 실행 → 유효한 결과."""
        result = one(
            '<data expr="close"/><rank/>'
        )
        sig = result
        # 시그널이 Signal 인스턴스이고 compute 가능한지 확인
        from framework.signal_base import Signal
        assert isinstance(sig, Signal)
        assert hasattr(sig, "compute")

    def test_xp019_branching_alpha_multi_backtest(self):
        """TC-XP-019: 분기 알파 목록으로 다수 결과."""
        results = compile(
            '<data expr="close | volume"/>'
        )
        from framework.signal_base import Signal
        assert len(results) >= 2
        for r in results:
            assert isinstance(r, Signal)


# ── 추가 테스트: 파싱 견고성 ──────────────────────────────────

class TestParsingRobustness:
    """xml_parser_plan.md에 명시되지 않았으나 필요한 추가 테스트."""

    def test_extra_whitespace_in_expr(self):
        """expr 값에 공백이 있는 경우."""
        compiled = one('<data expr="  close  "/>')
        assert isinstance(compiled, Price)
        assert compiled.column == "close"

    def test_extra_attributes_ignored(self):
        """알 수 없는 속성은 무시."""
        compiled = one('<data expr="close" foo="bar"/>')
        assert isinstance(compiled, Price)

    def test_delay_with_whitespace(self):
        """delay 값에 공백이 있는 경우."""
        compiled = one('<data expr="close" delay=" 2 "/>')
        assert isinstance(compiled, Delay)
        assert compiled.p == 2

    def test_days_invalid_type_in_pipeline(self):
        """days 속성에 실수 값 → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: 유효성 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tszscore days="3.5"/>')

    def test_days_zero_in_pipeline(self):
        """days=0 → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: window 유효성 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tszscore days="0"/>')

    def test_days_negative_in_pipeline(self):
        """음수 days → 에러.
        MVP: 다중 태그 XML 파싱 실패, Phase 1 이후: 유효성 에러."""
        with pytest.raises(XmlCompileError):
            compile('<data expr="close"/><tsmean days="-5"/>')


# ── Phase 3A: <shuffle> (TC-XP3-001 ~ 004) ───────────────────

class TestPhase3Shuffle:
    def test_xp3_001_shuffle_two_steps(self):
        results = compile(
            '<data expr="close"/>'
            '<shuffle><rank/><zscore/></shuffle>'
        )
        assert len(results) == 2
        shapes = set()
        for r in results:
            sig = r
            # record type chain (outer, inner)
            shapes.add((type(sig), type(sig.x)))
        assert (ZScore, Rank) in shapes
        assert (Rank, ZScore) in shapes

    def test_xp3_002_shuffle_three_steps_count(self):
        results = compile(
            '<data expr="close"/>'
            '<shuffle><rank/><zscore/><delay days="1"/></shuffle>'
        )
        assert len(results) == 6

    def test_xp3_003_shuffle_plus_range(self):
        results = compile(
            '<data expr="close"/>'
            '<shuffle>'
            '  <tszscore days="2...3"/>'
            '  <tsrank days="2...3"/>'
            '</shuffle>'
        )
        assert len(results) == 8

    def test_xp3_004_shuffle_limit_exceeded(self):
        # 8! = 40320 permutations > MAX_ALPHA_COUNT (1000)
        xml = (
            '<data expr="close"/>'
            '<shuffle>'
            '  <rank/>'
            '  <zscore/>'
            '  <delay days="1"/>'
            '  <delay days="2"/>'
            '  <tsmean days="2"/>'
            '  <tsmean days="3"/>'
            '  <tsrank days="2"/>'
            '  <tszscore days="2"/>'
            '</shuffle>'
        )
        with pytest.raises(XmlCompileError, match="Too many alphas"):
            compile(xml)

    def test_xp3_005_nested_shuffle_forbidden(self):
        """TC-XP3-005: 중첩 shuffle 금지 → XmlCompileError."""
        xml = (
            '<data expr="close"/>'
            '<shuffle>'
            '  <rank/>'
            '  <shuffle><zscore/><delay days="1"/></shuffle>'
            '</shuffle>'
        )
        with pytest.raises(XmlCompileError, match="Nested.*shuffle"):
            compile(xml)


# ── Phase 3B: <choice bind="same"> (TC-XP3-010 ~ 012) ───────

class TestPhase3ChoiceBindSame:
    def test_xp3_010_bind_same_two_blocks(self):
        results = compile(
            '<data expr="close"/>'
            '<choice bind="same"><rank/><zscore/></choice>'
            '<tszscore days="5"/>'
            '<choice bind="same"><rank/><zscore/></choice>'
        )
        assert len(results) == 2

        # Validate structure: rank...rank and zscore...zscore
        seen = set()
        for r in results:
            sig = r
            seen.add((type(sig), type(sig.x.x)))
        assert (Rank, Rank) in seen
        assert (ZScore, ZScore) in seen

    def test_xp3_011_bind_same_mismatch_raises(self):
        xml = (
            '<data expr="close"/>'
            '<choice bind="same"><rank/><zscore/></choice>'
            '<choice bind="same"><rank/><zscore/><tsmean days="3"/></choice>'
        )
        with pytest.raises(XmlCompileError, match="same number of alternatives"):
            compile(xml)

    def test_xp3_012_unbound_choice_cartesian(self):
        results = compile(
            '<data expr="close"/>'
            '<choice><rank/><zscore/></choice>'
            '<choice><rank/><zscore/></choice>'
        )
        assert len(results) == 4
