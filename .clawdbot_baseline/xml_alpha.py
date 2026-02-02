"""Minimal XML -> Signal compiler for LongShort.

MVP goal: support a single self-contained <data expr="close" delay="1"/> tag.
The root element MUST be <data>. No <alpha> or <expression> wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
import xml.etree.ElementTree as ET

from framework.modules.sources import Price
from framework.modules.delay import Delay
from framework.signal_base import Signal


@dataclass(frozen=True)
class CompiledAlpha:
    """Holds the compiled alpha signal graph."""

    signal: Signal


class XmlCompileError(ValueError):
    pass


def compile_alpha_from_xml(xml: str) -> CompiledAlpha:
    """Parse XML and return a compiled alpha.

    Expected (MVP):

    <data expr="close" delay="1"/>

    The root element MUST be <data>. No wrappers allowed.
    """

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise XmlCompileError(f"Invalid XML: {e}") from e

    # Root MUST be <data>
    if root.tag != "data":
        raise XmlCompileError(
            f"Root element must be <data>, got <{root.tag}>. "
            f"Use format: <data expr=\"close\" delay=\"1\"/>"
        )

    expr = (root.attrib.get("expr") or "").strip()
    delay = (root.attrib.get("delay") or "").strip()

    if expr != "close":
        raise XmlCompileError(f"MVP only supports expr=\"close\" (got: {expr!r})")

    # Build a signal graph
    sig: Signal = Price(column="close")

    if delay:
        try:
            p = int(delay)
        except ValueError as e:
            raise XmlCompileError(f"Invalid delay: {delay!r}") from e
        sig = Delay(sig, p)

    return CompiledAlpha(signal=sig)
