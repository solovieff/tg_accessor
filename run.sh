#!/bin/bash
set -e

# Use uv if available (in PATH or ~/.local/bin/uv)
if command -v uv &>/dev/null; then
    UV_CMD="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_CMD="$HOME/.local/bin/uv"
else
    UV_CMD=""
fi

if [ -n "$UV_CMD" ]; then
    exec "$UV_CMD" run group-seeker "$@"
else
    # Fallback if uv is not in environment
    source .venv/bin/activate
    exec group-seeker "$@"
fi


