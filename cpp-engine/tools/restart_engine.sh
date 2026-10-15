#!/usr/bin/env bash
# restart_engine.sh — DAG task restart_engine (STEP_CONTRACT §2, brief 2H): restart one longshort-server after publish_thin
# and verify /api/config (simEnd == manifest asOfDate, universes == yaml universe.names present in the published universe.parquet);
# roll back to the previous launch on failure.
#   bash restart_engine.sh [--config PATH] [--port N] [--timeout S] [--stop] [--binary PATH] [--log-dir DIR]
#   defaults: ~/longshort_workspace/config.yaml, 50007, 600; log ~/longshort_workspace/.longshort/logs/engine_<port>.log
# Logic lives in restart_engine.py next to this file; its exit code is passed through
# (0 ok / 1 failed — rolled back when an old server existed / 4 --stop with nothing running).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${ENGINE_PY:-/home/jgo/usr/venv-3.11/bin/python}" "$HERE/restart_engine.py" "$@"
