#!/bin/bash
# PostToolUse hook: run npm build check after frontend code files are modified
# Reads tool input JSON from stdin

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

# Only check code files under frontend/
case "$FILE_PATH" in
  */frontend/*.tsx|*/frontend/*.ts|*/frontend/*.css|*/frontend/*.jsx|*/frontend/*.js)
    ;;
  *)
    exit 0
    ;;
esac

# Skip test files and config files
case "$FILE_PATH" in
  */frontend/tests/*|*/frontend/__tests__/*|*/frontend/*.config.*|*/frontend/*.spec.*)
    exit 0
    ;;
esac

# Find frontend directory
FRONTEND_DIR=$(echo "$FILE_PATH" | sed 's|\(.*frontend\)/.*|\1|')

if [ ! -d "$FRONTEND_DIR" ]; then
  exit 0
fi

# Run build check with timeout
cd "$FRONTEND_DIR"
BUILD_OUTPUT=$(timeout 120 npm run build 2>&1)
BUILD_EXIT=$?

if [ $BUILD_EXIT -ne 0 ]; then
  # Truncate output if too long
  TRUNCATED=$(echo "$BUILD_OUTPUT" | tail -50)
  echo "BUILD FAILED after editing $FILE_PATH:"
  echo "$TRUNCATED"
  exit 1
fi
