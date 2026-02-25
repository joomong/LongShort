#!/bin/bash
# PreToolUse hook: ask permission before editing frontend/ files
# Reads tool input JSON from stdin, outputs decision JSON to stdout

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
FILE_PATH=""

case "$TOOL_NAME" in
  Edit|Write)
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
    ;;
  *)
    exit 0
    ;;
esac

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Check if path is under frontend/
case "$FILE_PATH" in
  */frontend/*)
    RELATIVE=$(echo "$FILE_PATH" | sed 's|.*/frontend/|frontend/|')
    echo "{\"decision\":\"ask\",\"message\":\"Frontend file modification: ${RELATIVE}\"}"
    ;;
  *)
    exit 0
    ;;
esac
