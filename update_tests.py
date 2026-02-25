#!/usr/bin/env python3
"""
Script to update test files for numpy-based PeriodResult migration.
"""

import re
import os


def update_test_file(filepath):
    """Update a single test file to handle numpy arrays instead of pandas Series."""

    with open(filepath, "r") as f:
        content = f.read()

    # Update imports if needed
    if "import pandas as pd" not in content:
        content = content.replace(
            "import numpy as np", "import numpy as np\nimport pandas as pd"
        )

    # Update assertions
    replacements = [
        # Series to array assertions
        (
            r"assert isinstance\(\w+_s, pd\.Series\)",
            "assert isinstance(\\1, np.ndarray)",
        ),
        (
            r"assert isinstance\(\w+_series, pd\.Series\)",
            "assert isinstance(\\1, np.ndarray)",
        ),
        # Update index access to array indexing
        (
            r"if day\d+ in \w+_s\.index:",
            "if day_day := next((i for i, ts in enumerate(timestamps) if ts == day_day), None):",
        ),
        (r"ret_s\[day\d+\]", "ret_s[day_idx]"),
        (r"to_s\[day\d+\]", "to_s[day_idx]"),
        # Update .values calls (arrays don't need .values)
        (r"\.values", ""),
        # Update Series methods to numpy equivalents
        (r"\.sum\(\)", "np.nansum(\\1)"),
        (r"\.mean\(\)", "np.nanmean(\\1)"),
        (r"\.std\(\)", "np.nanstd(\\1)"),
        (r"\.max\(\)", "np.nanmax(\\1)"),
        (r"\.min\(\)", "np.nanmin(\\1)"),
        # Update .fillna operations
        (r"(\w+)\.fillna\(0.0\)", "np.nan_to_num(\\1, nan=0.0)"),
        # Update .iloc access
        (r"\.iloc\[-1\]", "[-1]"),
        (r"\.iloc\[([^\]]+)\]", "[\\1]"),
        # Update .empty check
        (r"\.empty", ".size == 0"),
        (r"len\((\w+_s)\)", "len(\\1)"),
        # Update .index access for dates
        (r"(\w+_s)\.index", "timestamps"),
    ]

    for pattern, replacement in replacements:
        content = re.sub(pattern, replacement, content)

    with open(filepath, "w") as f:
        f.write(content)

    print(f"Updated {filepath}")


if __name__ == "__main__":
    # Update the main test file
    update_test_file("tests/unit/test_backtester.py")
    print("Test file migration complete!")
