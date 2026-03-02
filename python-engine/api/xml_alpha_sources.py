"""Helpers to return compiled alphas along with their expanded XML source.

We keep api/xml_alpha.py's `compile(xml) -> list[Signal]` stable for existing callers/tests,
while providing a richer API for endpoints that need per-alpha XML (detail view).
"""

from __future__ import annotations

from typing import List, Tuple

import xml.etree.ElementTree as ET

from api.xml_alpha import XmlCompileError, _expand_pipeline, _compile_pipeline, Op


def _pipeline_to_xml(pipeline: list[Op]) -> str:
    """Serialize a single expanded pipeline (list of Op tuples) to XML fragment."""
    parts: list[str] = []
    for tag, attrib in pipeline:
        elem = ET.Element(tag, {k: str(v) for k, v in (attrib or {}).items()})
        parts.append(ET.tostring(elem, encoding="unicode"))
    return "\n".join(parts).strip()


def compile_with_sources(xml: str) -> List[Tuple[object, str]]:
    """Compile XML fragment into [(compiled_signal, expanded_xml), ...]."""

    if xml is None:
        raise XmlCompileError("Empty XML")

    src = xml.strip()
    if not src:
        raise XmlCompileError("Empty XML")

    # Try single root.
    try:
        root = ET.fromstring(src)
    except ET.ParseError:
        root = None

    if root is not None:
        if root.tag != "data":
            raise XmlCompileError(
                f"Root element must be <data>, got <{root.tag}>. "
                f"Use format: <data expr=\"close\" delay=\"1\"/>"
            )
        pipelines = _expand_pipeline([root])
        return [(_compile_pipeline(p), _pipeline_to_xml(p)) for p in pipelines]

    wrapped = f"<alpha>{src}</alpha>"
    try:
        alpha_root = ET.fromstring(wrapped)
    except ET.ParseError as e:
        raise XmlCompileError(f"Invalid XML: {e}") from e

    elems = list(alpha_root)
    if not elems:
        raise XmlCompileError("Empty XML")
    if elems[0].tag != "data":
        raise XmlCompileError("Pipeline must start with <data>")

    pipelines = _expand_pipeline(elems)
    return [(_compile_pipeline(p), _pipeline_to_xml(p)) for p in pipelines]
