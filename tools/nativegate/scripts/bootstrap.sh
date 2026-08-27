#!/usr/bin/env bash
# Sets up a venv for nativegate and, if a service name is given, generates
# its Python package right away.
#
#   ./scripts/bootstrap.sh                 # just set up .venv
#   ./scripts/bootstrap.sh calculator       # set up .venv, then `ngate generate calculator`
#
#   source scripts/bootstrap.sh            # also activates the venv in your current shell
#
# Run from tools/nativegate/ (or pass an absolute path via N2P_DIR).
# Note: activation only takes effect in your shell if this script is
# *sourced* (`source scripts/bootstrap.sh`), not merely executed — a
# script run as a subprocess cannot change its parent shell's environment.
set -euo pipefail

# Resolve this script's own path whether run under bash or zsh, and whether
# executed or sourced.
if [ -n "${BASH_SOURCE:-}" ]; then
    _SCRIPT_PATH="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
    _SCRIPT_PATH="${(%):-%N}"
else
    _SCRIPT_PATH="$0"
fi

N2P_DIR="${N2P_DIR:-$(cd "$(dirname "$_SCRIPT_PATH")/.." && pwd)}"
REPO_ROOT="$(cd "$N2P_DIR/../.." && pwd)"
VENV="$N2P_DIR/.venv"

if [ ! -d "$VENV" ]; then
    echo "Creating venv at $VENV"
    python3 -m venv "$VENV"
fi

echo "Installing nativegate + build/test/docs dependencies..."
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$N2P_DIR/requirements.txt"

echo
echo "nativegate $("$VENV/bin/nativegate" --version | awk '{print $NF}') installed."

# Activate in the current shell only if we were sourced, not executed.
_SOURCED=0
if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
    _SOURCED=1
elif [ -n "${ZSH_EVAL_CONTEXT:-}" ] && [[ "$ZSH_EVAL_CONTEXT" == *:file* ]]; then
    _SOURCED=1
fi

if [ "$_SOURCED" = "1" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    echo "Activated: $VENV"
else
    echo "Activate with: source $VENV/bin/activate"
    echo "(tip: run 'source scripts/bootstrap.sh' next time to activate automatically)"
fi

if [ -n "${1:-}" ]; then
    SERVICE="$1"
    echo
    echo "Generating Python package for service '$SERVICE'..."
    (cd "$REPO_ROOT" && "$VENV/bin/nativegate" generate "$SERVICE")
fi

command -v cmake >/dev/null 2>&1 || echo "NOTE: cmake not found on PATH — needed for 'ngate build'. See docs/troubleshooting.md."
command -v ninja >/dev/null 2>&1 || echo "NOTE: ninja not found on PATH — needed for 'ngate build'. See docs/troubleshooting.md."
command -v gfortran >/dev/null 2>&1 || echo "NOTE: gfortran not found on PATH — needed for 'ngate build' (Fortran). See docs/troubleshooting.md."
