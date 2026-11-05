#!/usr/bin/env python3
"""Merge saved-alpha metadata.yml into alpha.xml (dry run unless --apply).

Pass saved-alpha roots, never research archives. Requires PyYAML only for this
one-time legacy conversion; normal platform reads/writes are implemented in C++.
"""
import argparse
import datetime
import os
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

import yaml

SIMULATION_KEYS = (
    "simStart", "simEnd", "isStart", "osStart", "universe", "executionPrice", "threads",
)


def parse(text):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.fromstring(text, parser=parser)


def convert(xml, metadata):
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a mapping")
    metadata = dict(metadata)
    for key in ("simStart", "simEnd", "isStart", "osStart"):
        if isinstance(metadata.get(key), datetime.date):
            metadata[key] = metadata[key].isoformat()
    for key in SIMULATION_KEYS[:-1]:
        if key not in metadata or not isinstance(metadata[key], str):
            raise ValueError(f"missing or invalid {key}")
    if metadata.get("level") not in ("xml", "results", "positions"):
        raise ValueError("invalid level")
    try:
        root = parse(xml)
    except ET.ParseError:
        root = None  # legacy operation fragments have multiple roots
    if root is None or root.tag != "alpha":
        root = ET.Element("alpha")
        pipeline = ET.SubElement(root, "pipeline")
        # XML declarations belong to the document, never inside the fragment.
        fragment = parse("<fragment>" + re.sub(r"<\?xml\s[^?]*\?>", "", xml) + "</fragment>")
        pipeline.extend(fragment)
    if len(root.findall("pipeline")) != 1 or len(root.findall("metadata")) > 1:
        raise ValueError("invalid alpha envelope")
    if root.attrib or any(node.tag not in ("pipeline", "metadata", ET.Comment) for node in root):
        raise ValueError("unexpected alpha attributes or children")
    meta = root.find("metadata")
    if meta is not None:
        raise ValueError("embedded metadata already exists; refusing to overwrite it with YAML")
    meta = ET.SubElement(root, "metadata")
    attributes = {key: str(metadata[key]) for key in SIMULATION_KEYS if key in metadata}
    if "threads" in attributes and not 1 <= int(attributes["threads"]) <= 32:
        raise ValueError("invalid threads")
    ET.SubElement(meta, "simulation", attributes)
    for key, value in metadata.items():
        if key in SIMULATION_KEYS or key == "level":
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key) or isinstance(value, (dict, list)):
            raise ValueError(f"unsupported descriptive metadata: {key}")
        ET.SubElement(meta, key).text = str(value)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    plans = []
    for root in args.roots:
        if not root.is_dir():
            parser.error(f"not a directory: {root}")
        for file in sorted(root.glob("*/metadata.yml")):
            if file.parent.name.startswith(".") or file.parent.is_symlink() or file.is_symlink():
                continue
            xml = file.with_name("alpha.xml")
            if xml.is_symlink():
                raise ValueError(f"refusing symlink: {xml}")
            original_xml, original_yaml = xml.read_bytes(), file.read_bytes()
            metadata = yaml.safe_load(original_yaml)
            level = "positions" if (file.parent / "positions").is_dir() and (file.parent / "results/index.json").is_file() else "results" if (file.parent / "results/index.json").is_file() else "xml"
            if level != metadata.get("level"):
                raise ValueError(f"stored artifacts disagree with level: {file}")
            converted = convert(original_xml.decode(), metadata)
            plans.append((xml, file, original_xml, original_yaml, converted))
    # Validate every input and back up every original before changing any alpha.
    if args.apply and plans:
        backup = args.backup_dir / datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for xml, file, *_ in plans:
            target = backup / str(xml.parent.resolve()).lstrip("/")
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(xml, target / xml.name)
            shutil.copy2(file, target / file.name)
        print(f"Backup: {backup}")
    for xml, file, original_xml, original_yaml, converted in plans:
        if args.apply:
            if xml.read_bytes() != original_xml or file.read_bytes() != original_yaml:
                raise RuntimeError(f"input changed during migration: {xml}")
            fd, temporary = tempfile.mkstemp(prefix=".alpha-", dir=xml.parent)
            try:
                with os.fdopen(fd, "w") as out:
                    out.write(converted)
                    out.flush()
                    os.fsync(out.fileno())
                shutil.copymode(xml, temporary)
                os.replace(temporary, xml)
                file.unlink()
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        print(f"{'Converted' if args.apply else 'Would convert'}: {xml}")
    print(f"{len(plans)} alphas")


if __name__ == "__main__":
    main()
