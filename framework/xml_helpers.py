from framework.exceptions import XmlCompileError


def parse_positive_int(attrib: dict, key: str) -> int:
    """Parse a required positive integer attribute, raising XmlCompileError on failure."""
    raw = (attrib.get(key) or "").strip()
    if not raw:
        raise XmlCompileError(f"Missing required attribute: {key}")
    try:
        val = int(raw)
    except ValueError as e:
        raise XmlCompileError(f"Invalid {key}: {raw!r}") from e
    if val <= 0:
        raise XmlCompileError(f"Invalid {key}: {raw!r}")
    return val
