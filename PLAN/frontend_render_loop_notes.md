# Frontend Render Loop Notes

Context
- A React "Maximum update depth exceeded" error was triggered by unstable store snapshots.

Root cause
- `useAlphaStore` selectors returning `?? []` created a new array on every render.
- React treated the snapshot as changed and re-rendered repeatedly.

Prevention
- Avoid `?? []` (or other freshly allocated objects) inside selectors.
- Use module-level stable constants (e.g., `EMPTY_ALPHAS`) or return `undefined` and apply fallbacks in render.

Related guardrails
- If a controlled editor can emit `onChange` for programmatic updates, guard against same-value writes.
- Store setters should no-op when the next value equals current state.
