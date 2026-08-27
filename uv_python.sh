#!/usr/bin/env bash
set -e
PROMPT_EVAL_DIR="$(dirname "$(realpath "$0")")"
exec uv run --all-extras --project "$PROMPT_EVAL_DIR" python "$@"
