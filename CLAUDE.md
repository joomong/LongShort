
# CLAUDE.md (LongShort)

## Rules
- Make small, reviewable diffs.
- After each change: show diff (or file snippet) + run a minimal smoke test.
- Prefer existing running servers; do not start new servers unless needed.
- Avoid long-running interactive sessions; finish tasks in <=10 minutes.

## Current conventions
- XML format: <data expr="close" delay="1"/> (no wrappers).
- Backtester viz dataframe should have a datetime-like index named 'date'.

