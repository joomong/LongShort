"""XML -> Signal compiler for LongShort.

Implements Phase 1 (pipeline) and Phase 2 (branching/expansion) per PLAN/xml_parser_plan.md.

Public API:
- compile(xml: str) -> list[Signal]  (always returns a list)

Supported tags (Phase 1):
- <data expr="..." delay="..."/>
- <rank/>, <zscore/>
- <tszscore days="..."/>, <tsrank days="..."/>, <tsmean days="..."/>
- <delay days="..."/>
- <signal id="..."/> (registry only)

Phase 2 expansion:
- Pipe in attribute values: "A | B | C"
- Range in integer attributes: "20...10...60" or "3...5"
- <choice>...</choice> where each child tag is an alternative pipeline step

Notes:
- Users can provide multiple top-level tags; internally we wrap them with <alpha>..</alpha>.
- For backwards compatibility, a *single* XML element must still have <data> as the root.
"""

from __future__ import annotations

from itertools import permutations, product
from math import factorial
from typing import Dict, List, Sequence, Tuple
import xml.etree.ElementTree as ET

from framework.signal_base import Signal
from framework.exceptions import XmlCompileError
from framework.modules.delay import Delay

import framework.modules  # trigger __init_subclass__ registration

_EXPECTED_TAGS = {"rank", "zscore", "tsmean", "tszscore", "tsrank", "delay", "event"}
_missing = _EXPECTED_TAGS - set(Signal._tag_registry.keys())
if _missing:
    raise ImportError(f"Signal modules failed to register tags: {_missing}")


MAX_ALPHA_COUNT = 1000


# -------------------------
# Parsing helpers
# -------------------------


def _split_pipe(value: str) -> List[str]:
    """Split attribute value by pipe.

    We treat both "|" and "\\|" as separators (tests use unescaped '|').
    """

    raw = (value or "").strip()
    if not raw:
        return [raw]

    # Normalize escaped pipes so both syntaxes work.
    raw = raw.replace("\\|", "|")
    if "|" not in raw:
        return [raw]

    parts = [p.strip() for p in raw.split("|")]
    return [p for p in parts if p != ""]


def _parse_range(value: str) -> List[str]:
    """Parse 'start...step...end' or 'start...end' into a list of string ints.

    If the value is not a range expression, returns [value].
    """

    raw = (value or "").strip()
    if "..." not in raw:
        return [raw]

    parts = raw.split("...")
    try:
        if len(parts) == 3:
            start, step, end = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
            step = 1
        else:
            return [raw]
    except ValueError as e:
        raise XmlCompileError(f"Invalid range expression: {raw!r}") from e

    if step == 0:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")

    # Basic direction sanity. (Keep simple; plan's use-cases are increasing.)
    if step > 0 and start > end:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")
    if step < 0 and start < end:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")

    return [str(v) for v in range(start, end + (1 if step > 0 else -1), step)]


# -------------------------
# Expansion model
# -------------------------


Op = Tuple[str, Dict[str, str]]  # (tag, attrib)


def _apply_vars_to_value(value: str, vars_map: Dict[str, str]) -> str:
    """Replace occurrences of $[name] with the corresponding var value."""

    out = value
    for k, v in vars_map.items():
        out = out.replace(f"$[{k}]", v)
    return out


def _expand_element(elem: ET.Element, vars_map: Dict[str, str] | None = None) -> List[Op]:
    """Expand a single element into alternative concrete ops (Phase 2).

    - For normal tags: expand attributes via pipe/range.
    - For <choice> (without bind): returns flattened alternatives of its children.

    Phase 3 special forms (<shuffle>, <choice bind="same">) are handled at pipeline expansion
    time, not here.
    """

    if elem.tag == "choice":
        bind = (elem.attrib.get("bind") or "").strip()
        if bind:
            # Handled by _expand_pipeline.
            raise XmlCompileError(
                "<choice bind=...> is only supported in pipeline context (bind=\"same\")"
            )
        alts: List[Op] = []
        for child in list(elem):
            alts.extend(_expand_element(child, vars_map))
        return alts

    # Expand each attribute: apply vars -> pipe -> range.
    # Cartesian product across attributes.
    keys = list(elem.attrib.keys())
    values_per_key: List[List[str]] = []
    vm = vars_map or {}
    for k in keys:
        v = elem.attrib.get(k, "")
        v = _apply_vars_to_value(v, vm)
        pipe_parts = _split_pipe(v)
        expanded: List[str] = []
        for pp in pipe_parts:
            expanded.extend(_parse_range(pp))
        values_per_key.append(expanded if expanded else [""])

    ops: List[Op] = []
    for combo in product(*values_per_key) if keys else [()]:
        attrib = {k: str(v).strip() for k, v in zip(keys, combo)}
        ops.append((elem.tag, attrib))
    return ops


def _expand_shuffle(elem: ET.Element, vars_map: Dict[str, str] | None = None) -> List[List[Op]]:
    """Expand a <shuffle> block into alternative *sequences* of ops.

    Rules (Phase 3A):
    - The shuffle element permutes the order of its direct child steps.
    - Nested <shuffle> is not allowed.
    - Each child step can itself expand via pipe/range/<choice> (unbound).

    Returns: list of concrete op sequences to splice into the pipeline.
    """

    if elem.tag != "shuffle":
        raise AssertionError("_expand_shuffle called with non-shuffle element")

    # Disallow nested shuffle anywhere under this node.
    for sub in elem.iter():
        if sub is elem:
            continue
        if sub.tag == "shuffle":
            raise XmlCompileError("Nested <shuffle> is not supported")

    children = list(elem)
    if not children:
        return [[]]

    # Expand each child into concrete ops.
    alts_per_child: List[List[Op]] = []
    for child in children:
        if child.tag == "choice" and (child.attrib.get("bind") or "").strip():
            raise XmlCompileError("<choice bind=...> is not supported inside <shuffle>")
        alts = _expand_element(child, vars_map)
        alts_per_child.append(alts)

    # Quick size estimate to fail fast.
    total = 1
    for alts in alts_per_child:
        total *= max(1, len(alts))
    total *= factorial(len(children))
    if total > MAX_ALPHA_COUNT:
        raise XmlCompileError(
            f"Too many alphas generated (>{MAX_ALPHA_COUNT}) by <shuffle>. Reduce branching."
        )

    seqs: List[List[Op]] = []
    for picked in product(*alts_per_child):
        for perm in permutations(list(picked)):
            seqs.append(list(perm))
            if len(seqs) > MAX_ALPHA_COUNT:
                raise XmlCompileError(
                    f"Too many alphas generated (>{MAX_ALPHA_COUNT}). Reduce branching."
                )
    return seqs


def _expand_pipeline(elems: Sequence[ET.Element]) -> List[List[Op]]:
    """Expand a pipeline (list of elements) into a list of concrete pipelines.

    Phase 2 expansions:
    - attribute pipe/range
    - <choice> (unbound)

    Phase 3 expansions:
    - <shuffle> permutations
    - <choice bind="same"> index-synchronized choices across occurrences
    """

    pipelines: List[List[Op]] = [[]]

    # Phase 3C (partial): <var> support.
    # We collect variables as name -> string, and later substitute $[name] in attribute values.
    vars_map: Dict[str, str] = {}

    # bind="same" support: treat the bind value as a group key.
    bound_choice_sizes: Dict[str, int] = {}

    # For each partial pipeline, we carry the chosen index for each bind group.
    partials: List[Tuple[List[Op], Dict[str, int]]] = [([], {})]

    def _check_limit(n: int):
        if n > MAX_ALPHA_COUNT:
            raise XmlCompileError(
                f"Too many alphas generated (>{MAX_ALPHA_COUNT}). Reduce branching."
            )

    for elem in elems:
        tag = elem.tag

        if tag == "var":
            # Define variables for later substitutions: <var x="a|b" y="1...3"/>
            for k, v in elem.attrib.items():
                vars_map[k] = v.strip()
            continue

        if tag == "shuffle":
            seq_alts = _expand_shuffle(elem, vars_map)
            new_partials: List[Tuple[List[Op], Dict[str, int]]] = []
            for ops, binds in partials:
                for seq in seq_alts:
                    new_partials.append(([*ops, *seq], dict(binds)))
                    _check_limit(len(new_partials))
            partials = new_partials
            continue

        if tag == "choice" and (elem.attrib.get("bind") or "").strip():
            bind = (elem.attrib.get("bind") or "").strip()
            if bind != "same":
                raise XmlCompileError(
                    f"Unsupported bind value: {bind!r}. Only bind=\"same\" is supported."
                )

            # Flatten alternatives of this choice block.
            alts: List[Op] = []
            for child in list(elem):
                if child.tag == "shuffle":
                    raise XmlCompileError("<shuffle> inside <choice> is not supported")
                if child.tag == "choice" and (child.attrib.get("bind") or "").strip():
                    raise XmlCompileError(
                        "Nested <choice bind=...> is not supported (bind=\"same\")"
                    )
                alts.extend(_expand_element(child, vars_map))

            if bind in bound_choice_sizes:
                if len(alts) != bound_choice_sizes[bind]:
                    raise XmlCompileError(
                        "All <choice bind=\"same\"> blocks must have the same number "
                        "of alternatives"
                    )
            else:
                bound_choice_sizes[bind] = len(alts)

            new_partials = []
            for ops, binds in partials:
                if bind in binds:
                    idx = binds[bind]
                    if idx >= len(alts):
                        raise XmlCompileError(
                            "All <choice bind=\"same\"> blocks must have the same number "
                            "of alternatives"
                        )
                    new_partials.append(([*ops, alts[idx]], dict(binds)))
                else:
                    for idx in range(len(alts)):
                        b2 = dict(binds)
                        b2[bind] = idx
                        new_partials.append(([*ops, alts[idx]], b2))
                        _check_limit(len(new_partials))
            partials = new_partials
            continue

        # Default: Phase 2 element expansion
        step_alts = _expand_element(elem, vars_map)
        if not step_alts:
            continue

        new_partials = []
        for ops, binds in partials:
            for alt in step_alts:
                new_partials.append(([*ops, alt], dict(binds)))
                _check_limit(len(new_partials))
        partials = new_partials

    pipelines = [ops for ops, _binds in partials]
    return pipelines


# -------------------------
# Signal building
# -------------------------




def _compile_pipeline(ops: Sequence[Op]) -> Signal:
    if not ops:
        raise XmlCompileError("Empty XML")

    registry: Dict[str, Signal] = {}

    first_tag, first_attrib = ops[0]
    if first_tag != "data":
        raise XmlCompileError("Pipeline must start with <data>")

    expr = (first_attrib.get("expr") or "").strip()
    delay = (first_attrib.get("delay") or "").strip()

    from api.expr_parser import parse_expr
    sig: Signal = parse_expr(expr)
    if delay != "":
        try:
            p = int(delay)
        except ValueError as e:
            raise XmlCompileError(f"Invalid delay: {delay!r}") from e
        sig = Delay(sig, p)

    # Remaining pipeline
    for tag, attrib in ops[1:]:
        # <signal> is special: stores sig in registry, does not create a new Signal
        if tag == "signal":
            sid = (attrib.get("id") or "").strip()
            if not sid:
                raise XmlCompileError("Missing required attribute: id")
            registry[sid] = sig
            continue

        factory_cls = Signal._tag_registry.get(tag)
        if factory_cls is None:
            raise XmlCompileError(f"Unsupported tag: <{tag}>")
        sig = factory_cls.from_xml(sig, attrib, registry)

    return sig


# -------------------------
# Public API
# -------------------------


def compile(xml: str) -> List[Signal]:
    """Compile XML fragment into a list of compiled alphas."""

    if xml is None:
        raise XmlCompileError("Empty XML")

    src = xml.strip()
    if not src:
        raise XmlCompileError("Empty XML")

    # First try parsing as a single element (MVP behavior).
    try:
        root = ET.fromstring(src)
    except ET.ParseError:
        root = None

    if root is not None:
        # Backwards compatible strictness: root must be <data>.
        if root.tag != "data":
            raise XmlCompileError(
                f"Root element must be <data>, got <{root.tag}>. "
                f"Use format: <data expr=\"close\" delay=\"1\"/>"
            )
        ops = _expand_pipeline([root])
        return [_compile_pipeline(p) for p in ops]

    # Multi-tag pipeline: wrap.
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
    return [_compile_pipeline(p) for p in pipelines]

# NOTE: compile_alpha_from_xml removed; use compile(xml) which always returns list[Signal].
