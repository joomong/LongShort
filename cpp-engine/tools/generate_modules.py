#!/usr/bin/env python3
"""Discover ordinary C++ Module headers and generate their XML factories.

This scans the framework's declaration convention, not arbitrary C++ reflection:
one concrete, directly/publicly Module-derived type per header; XML parameters
are public, directly declared IntParameter/FloatParameter instance fields.
C++ still compiles the header and evaluates its member initializers normally.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile


class DiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str


@dataclass(frozen=True)
class DiscoveredModule:
    header: Path
    tag: str
    class_name: str
    parameters: tuple[Parameter, ...]
    bind_panel: bool = False


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|::|[^\s]", re.ASCII)
ATTRIBUTE = r"(?:\[\[[\s\S]*?\]\]|alignas\s*\([^{};]*\))"
CLASS = re.compile(
    r"\b(?P<kind>struct|class)\s+(?:" + ATTRIBUTE + r"\s*)*"
    r"(?P<name>[A-Za-z_][A-Za-z_0-9]*)\s*(?:" + ATTRIBUTE + r"\s*)*"
    r"(?:final\s*)?(?:\:\s*(?P<bases>[^{};]+))?\s*\{", re.ASCII
)
NAMESPACE = re.compile(r"\bnamespace\s+([A-Za-z_][A-Za-z_0-9]*(?:::[A-Za-z_][A-Za-z_0-9]*)*)\s*\{")
RESERVED_TAGS = frozenset({"data", "signal", "event", "choice", "alpha",
                           "tsmean", "tsrank", "tszscore", "decay", "delay", "rank", "zscore",
                           "neutralize", "meta", "ew", "mvo", "minvar", "erc", "ivol",
                           "spca", "glasso",
                           "abs", "log", "sqrt", "sign", "inverse", "reverse",
                           "power", "signedpower", "max", "min"})


def error(path: Path, source: str, offset: int, message: str):
    line = source.count("\n", 0, offset) + 1
    raise DiscoveryError(f"{path}:{line}: {message}")


def mask_non_code(source: str, path: Path) -> str:
    """Mask comments/literals without changing offsets or source line numbers."""
    out = list(source)
    i = 0
    while i < len(source):
        start = i
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            while end >= 0 and end > i and source[end - 1] == "\\":
                end = source.find("\n", end + 1)
            i = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                error(path, source, i, "unterminated comment")
            i = end + 2
        else:
            raw = re.match(r'(?:u8|u|U|L)?R"([^\s()\\]{0,16})\(', source[i:])
            if raw:
                closing = ")" + raw.group(1) + '"'
                end = source.find(closing, i + raw.end())
                if end < 0:
                    error(path, source, i, "unterminated raw string")
                i = end + len(closing)
            else:
                literal = re.match(r'''(?:u8|u|U|L)?(["'])''', source[i:])
                # Numeric separators (including hexadecimal digits) are not character literals.
                separator = (source[i] == "'" and i > 0 and i + 1 < len(source)
                             and source[i - 1].isalnum() and source[i + 1].isalnum())
                if not literal or separator:
                    i += 1
                    continue
                quote = literal.group(1)
                i += literal.end()
                while i < len(source):
                    if source[i] == "\\":
                        i += 2
                    elif source[i] == quote:
                        i += 1
                        break
                    else:
                        i += 1
                else:
                    error(path, source, start, "unterminated string/character literal")
        for j in range(start, min(i, len(out))):
            if out[j] not in "\r\n":
                out[j] = " "
    return "".join(out)


def braces(masked: str, path: Path, source: str):
    pairs, stack = {}, []
    for i, char in enumerate(masked):
        if char == "{":
            stack.append(i)
        elif char == "}":
            if not stack:
                error(path, source, i, "unbalanced closing brace")
            pairs[stack.pop()] = i
    if stack:
        error(path, source, stack[-1], "unbalanced opening brace")
    return pairs


def namespace_ranges(masked: str, pairs):
    return [(m.end() - 1, pairs[m.end() - 1], m.group(1)) for m in NAMESPACE.finditer(masked)]


def qualified_name(match, masked, namespaces):
    enclosing = sorted((a, name) for a, b, name in namespaces if a < match.start() < b)
    depth = masked[:match.start()].count("{") - masked[:match.start()].count("}")
    if depth != len(enclosing):
        return None  # A nested/local/anonymous-namespace class is not an exported module.
    return "::".join([name for _, name in enclosing] + [match.group("name")])


def check_conditionals(masked, source, path, body_start, body_end):
    directives = list(re.finditer(r"(?m)^[ \t]*#[ \t]*(\w+)\b([^\n]*)", masked))
    guard = set()
    if len(directives) >= 3:
        first, second, last = directives[0], directives[1], directives[-1]
        if (first.group(1) == "ifndef" and second.group(1) == "define"
                and first.group(2).strip() == second.group(2).strip()
                and IDENTIFIER.fullmatch(first.group(2).strip())
                and last.group(1) == "endif"):
            guard = {first.start(), last.start()}
    for directive in directives:
        if directive.group(1) not in {"if", "ifdef", "ifndef", "else", "elif", "endif"}:
            continue
        if directive.start() in guard:
            continue
        offset = directive.start()
        in_body = body_start < offset < body_end
        depth = (masked[body_start + 1:offset].count("{")
                 - masked[body_start + 1:offset].count("}")) if in_body else 0
        if not in_body or depth == 0:
            error(path, source, offset,
                  "conditional module/parameter declarations are not supported; keep the exported interface unconditional")


def split_declarators(tokens):
    parts, current, stack = [], [], []
    for token in tokens:
        value = token.group()
        if value in {"(", "[", "{"}:
            stack.append(value)
        elif value in {")", "]", "}"}:
            if stack:
                stack.pop()
        if value == "," and not stack:
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts.append(current)
    return parts


def without_attributes(values):
    """Strip declaration attributes; their semantics remain in the compiled header."""
    result = []
    i = 0
    while i < len(values):
        if values[i:i + 2] == ["[", "["]:
            i += 2
            depth = 2
            while i < len(values) and depth:
                if values[i] == "[":
                    depth += 1
                elif values[i] == "]":
                    depth -= 1
                i += 1
            if depth:
                return values  # Unsupported/incomplete: the caller will reject it.
        elif values[i] == "alignas" and values[i + 1:i + 2] == ["("]:
            i += 2
            depth = 1
            while i < len(values) and depth:
                if values[i] == "(":
                    depth += 1
                elif values[i] == ")":
                    depth -= 1
                i += 1
            if depth:
                return values
        else:
            result.append(values[i])
            i += 1
    return result


def parameters_for(masked, source, path, body_start, body_end, is_struct):
    tokens = list(TOKEN.finditer(masked, body_start + 1, body_end))
    access = "public" if is_struct else "private"
    depth = parentheses = 0
    statement_start = 0
    parameters = []
    bind_panel = False
    i = 0
    while i < len(tokens):
        value = tokens[i].group()
        if depth == 0 and parentheses == 0:
            if value in {"public", "private", "protected"} and i + 1 < len(tokens) and tokens[i + 1].group() == ":":
                access = value
                i += 2
                statement_start = i
                continue
            prefix = without_attributes([token.group() for token in tokens[statement_start:i]])
            if value in {"IntParameter", "FloatParameter"}:
                # A return type is not a configurable instance field.
                after = [t.group() for t in tokens[i + 1:i + 4]]
                if ((len(after) >= 2 and IDENTIFIER.fullmatch(after[0]) and after[1] == "(")
                        or (len(after) >= 3 and after[0] in {"*", "&"}
                            and IDENTIFIER.fullmatch(after[1]) and after[2] == "(")):
                    i += 1
                    continue
                if prefix and any(p not in {"::", "static", "const", "constexpr", "mutable", "inline", "volatile", "thread_local", "extern", "register"} for p in prefix):
                    i += 1  # E.g. vector<IntParameter> or a type alias.
                    continue
                if access != "public":
                    error(path, source, tokens[i].start(), "XML parameter fields must be public")
                if any(p != "::" for p in prefix):
                    error(path, source, tokens[i].start(), "XML parameter fields must be non-static, mutable instance values")
                j = i + 1
                nesting = 0
                while j < len(tokens):
                    symbol = tokens[j].group()
                    if symbol == ";" and nesting == 0:
                        break
                    if symbol in {"(", "[", "{"}:
                        nesting += 1
                    elif symbol in {")", "]", "}"}:
                        nesting -= 1
                    j += 1
                if j == len(tokens):
                    error(path, source, tokens[i].start(), "parameter declaration needs a semicolon")
                for declaration in split_declarators(tokens[i + 1:j]):
                    if not declaration or not IDENTIFIER.fullmatch(declaration[0].group()):
                        error(path, source, tokens[i].start(), "parameters must be plain value members, not pointers/references")
                    name = declaration[0].group()
                    suffix = without_attributes([t.group() for t in declaration[1:]])
                    if suffix and suffix[0] not in {"=", "{"}:
                        error(path, source, declaration[0].start(), "unsupported parameter declarator; use a plain member with = or {} initialization")
                    if name == "input":
                        error(path, source, declaration[0].start(), "input is reserved for XML signal routing")
                    if any(p.name == name for p in parameters):
                        error(path, source, declaration[0].start(), f"duplicate XML parameter {name!r}")
                    parameters.append(Parameter(name, value))
                i = j + 1
                statement_start = i
                continue
            # Optional existing-style access to the engine's read-only data panel.
            look = [t.group() for t in tokens[i:i + 4]]
            if look == ["const", "DataPanel", "*", "panel"] and not prefix:
                if access != "public":
                    error(path, source, tokens[i].start(), "the optional panel binding must be public")
                following = tokens[i + 4].group() if i + 4 < len(tokens) else None
                if following not in {"=", "{", ";"}:
                    error(path, source, tokens[i].start(), "panel must be a const DataPanel* value member")
                bind_panel = True
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                statement_start = i + 1
        elif depth == 0:
            if value == "(":
                parentheses += 1
            elif value == ")":
                parentheses -= 1
            elif value == ";" and parentheses == 0:
                statement_start = i + 1
        i += 1
    return tuple(parameters), bind_panel


def parse_header(path: Path):
    source = path.read_text(encoding="utf-8")
    masked = mask_non_code(source, path)
    pairs = braces(masked, path, source)
    namespaces = namespace_ranges(masked, pairs)
    matches = []
    for match in CLASS.finditer(masked):
        bases = match.group("bases") or ""
        if not re.search(r"\bModule\b", bases):
            continue
        name = qualified_name(match, masked, namespaces)
        if name is None:
            continue
        base = re.fullmatch(r"\s*(?:(public|private|protected)\s+)?(?:::)?Module\s*", bases)
        if not base or base.group(1) in {"private", "protected"} or (match.group("kind") == "class" and not base.group(1)):
            error(path, source, match.start(), "modules must inherit directly and publicly from Module")
        prefix_start = max(masked.rfind(";", 0, match.start()), masked.rfind("}", 0, match.start()), masked.rfind("{", 0, match.start())) + 1
        if re.search(r"\btemplate\s*<", masked[prefix_start:match.start()]):
            error(path, source, match.start(), "exported modules must be concrete, not class templates")
        matches.append((match, name))
    if not matches:
        if re.search(r":\s*(?:(?:public|private|protected)\s+)?(?:::)?Module\b", masked):
            error(path, source, 0, "no discoverable Module declaration; use a global or named-namespace, directly Module-derived type")
        return None  # An ordinary helper header is allowed beside module headers.
    if len(matches) != 1:
        error(path, source, matches[1][0].start(), "use one exported Module type per header")
    match, name = matches[0]
    body_start = match.end() - 1
    body_end = pairs[body_start]
    check_conditionals(masked, source, path, body_start, body_end)
    parameters, bind_panel = parameters_for(masked, source, path, body_start, body_end, match.group("kind") == "struct")
    tag = path.stem.lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]*", tag):
        error(path, source, match.start(), "header stem must be a valid ASCII XML tag (letters, digits, _, -, .)")
    if tag in RESERVED_TAGS:
        error(path, source, match.start(), f"module tag {tag!r} is reserved by the engine")
    return DiscoveredModule(path.resolve(), tag, name, parameters, bind_panel)


def builtin_type_names():
    engine = Path(__file__).resolve().parents[1]
    paths = [* (engine / "include").glob("*.h"), * (engine / "src/modules").glob("*.h"), engine / "src/data/data_panel.h"]
    names = set()
    for path in paths:
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        masked = mask_non_code(source, path)
        pairs = braces(masked, path, source)
        namespaces = namespace_ranges(masked, pairs)
        for match in CLASS.finditer(masked):
            name = qualified_name(match, masked, namespaces)
            if name:
                names.add(name)
    return names


def discover(modules_dir: Path):
    if not modules_dir.exists():
        return []
    if not modules_dir.is_dir():
        raise DiscoveryError(f"{modules_dir}: expected a module directory")
    headers = sorted(p for p in modules_dir.rglob("*") if p.is_file() and p.suffix in {".h", ".hpp"}
                     and not any(part.startswith(".") for part in p.relative_to(modules_dir).parts))
    modules, tags, classes = [], {}, {}
    reserved_classes = builtin_type_names()
    for header in headers:
        module = parse_header(header)
        if module is None:
            continue
        if module.tag in tags:
            raise DiscoveryError(f"duplicate module tag {module.tag!r}: {tags[module.tag]} and {header}")
        if module.class_name in reserved_classes:
            raise DiscoveryError(f"{header}: class {module.class_name!r} conflicts with an engine type; use a unique name/namespace")
        if module.class_name in classes:
            raise DiscoveryError(f"duplicate module class {module.class_name!r}: {classes[module.class_name]} and {header}")
        tags[module.tag], classes[module.class_name] = header, header
        modules.append(module)
    return modules


def cpp_string(value):
    return json.dumps(value, ensure_ascii=True)


def render(modules):
    lines = ['// Generated by generate_modules.py. Do not edit.',
             '#include "compiler/dag_builder.h"', '#include "compiler/module_attributes.h"',
             '#include <type_traits>']
    for module in modules:
        header = module.header.as_posix()
        if any(char in header for char in ('"', "\\", "\n", "\r")):
            raise DiscoveryError(f"{module.header}: unsupported quote/backslash/newline in header path")
        # C++ header-names are not string literals: preserve UTF-8 instead of JSON escapes.
        lines.append(f'#include "{header}"')
    lines.extend(['', 'void register_discovered_modules(ModuleRegistry& registry) {'])
    if not modules:
        lines.append('    (void)registry;')
    for module in modules:
        name, tag = "::" + module.class_name, cpp_string(module.tag)
        lines.extend([
            f'    static_assert(std::is_base_of_v<Module, {name}> && !std::is_abstract_v<{name}>, "Discovered modules must be concrete Module types");',
            f'    registry.register_module({tag}, [](const auto& attrs, const DataPanel* panel) {{',
            '        module_attributes::validate_keys(attrs, {' + ', '.join(cpp_string(p.name) for p in module.parameters) + '}, ' + tag + ');',
            f'        auto module = std::make_unique<{name}>();',
            '        module->panel = panel;' if module.bind_panel else '        (void)panel;',
        ])
        for parameter in module.parameters:
            key = cpp_string(parameter.name)
            conversion = 'parse_int' if parameter.kind == 'IntParameter' else 'parse_float'
            lines.extend([
                f'        static_assert(std::is_same_v<decltype(module->{parameter.name}), ::{parameter.kind}>, \"XML parameters must use the SDK parameter types\");',
                f'        if (auto it = attrs.find({key}); it != attrs.end())',
                f'            module->{parameter.name} = {parameter.kind}(module_attributes::{conversion}(it->second, {tag}, {key}));',
            ])
        lines.extend(['        return module;', '    });'])
    lines.extend(['}', ''])
    return '\n'.join(lines)


def write_if_changed(path: Path, contents: str):
    if path.exists() and path.read_text(encoding="utf-8") == contents:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--modules-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        modules = discover(args.modules_dir.resolve())
        changed = write_if_changed(args.output, render(modules))
        if args.manifest:
            manifest = [{'tag': m.tag, 'class': m.class_name, 'header': str(m.header),
                         'parameters': [{'name': p.name, 'type': p.kind} for p in m.parameters],
                         'binds_panel': m.bind_panel} for m in modules]
            write_if_changed(args.manifest, json.dumps(manifest, indent=2)+'\n')
        print('Discovered modules: ' + (', '.join(m.tag for m in modules) or '(none)')
              + ('; factories updated' if changed else '; factories unchanged'))
        return 0
    except (DiscoveryError, OSError, UnicodeError) as exc:
        print(f'Module discovery failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
