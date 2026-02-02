import pytest

from api.xml_alpha import compile, XmlCompileError
from framework.modules.event import EventSignal


@pytest.mark.parametrize(
    "bet_all,expected",
    [("1", True), ("0", False)],
)
def test_event_happy_path_compile_to_eventsignal(bet_all: str, expected: bool):
    xml = (
        '<data expr="close"/>'
        '<signal id="base"/>'
        '<rank/>'
        '<signal id="trig"/>'
        f'<event signal="base" trigger="trig" threshold="0.0" maxHolddays="2" betAll="{bet_all}"/>'
    )

    results = compile(xml)
    assert len(results) == 1
    sig = results[0]
    assert isinstance(sig, EventSignal)
    assert sig.threshold == 0.0
    assert sig.max_holddays == 2
    assert sig.bet_all is expected


@pytest.mark.parametrize(
    "xml,match",
    [
        (
            '<data expr="close"/><signal id="base"/><signal id="trig"/>'
            '<event signal="base" trigger="trig" threshold="x" maxHolddays="2" betAll="1"/>',
            "Invalid threshold",
        ),
        (
            '<data expr="close"/><signal id="base"/><signal id="trig"/>'
            '<event signal="base" trigger="trig" threshold="0" maxHolddays="0" betAll="1"/>',
            "Invalid maxHolddays",
        ),
        (
            '<data expr="close"/><signal id="base"/><signal id="trig"/>'
            '<event signal="base" trigger="trig" threshold="0" maxHolddays="2" betAll="2"/>',
            "Invalid betAll",
        ),
        (
            '<data expr="close"/><signal id="base"/><signal id="trig"/>'
            '<event trigger="trig" threshold="0" maxHolddays="2" betAll="1"/>',
            "Missing required attribute: signal",
        ),
        (
            '<data expr="close"/><signal id="base"/><signal id="trig"/>'
            '<event signal="base" threshold="0" maxHolddays="2" betAll="1"/>',
            "Missing required attribute: trigger",
        ),
    ],
)
def test_event_compile_validation_errors(xml: str, match: str):
    with pytest.raises(XmlCompileError, match=match):
        compile(xml)
