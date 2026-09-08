#!/bin/sh
# Scoped environment only; does not modify shell startup files.
set -eu
DCA_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
umask 077
if [ -n "${DCA_UV_ROOT:-}" ]; then
  case "$DCA_UV_ROOT" in /*) ;; *) printf '%s\n' 'DCA_UV_ROOT must be absolute' >&2; exit 2;; esac
else
  DCA_UV_ROOT="$DCA_ROOT/runtime/toolchain"
fi
export TMPDIR="$DCA_ROOT/runtime/tmp"
export TMP="$TMPDIR" TEMP="$TMPDIR"
export UV_INSTALL_DIR="${UV_INSTALL_DIR:-$DCA_UV_ROOT/bin}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$DCA_ROOT/runtime/cache/uv}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$DCA_UV_ROOT/python}"
export UV_TOOL_DIR="${UV_TOOL_DIR:-$DCA_UV_ROOT/tools}"
export UV_TOOL_BIN_DIR="${UV_TOOL_BIN_DIR:-$DCA_UV_ROOT/bin}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$DCA_ROOT/.venv}"
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-managed}"
export UV_PYTHON_DOWNLOADS=never
export UV_NO_ENV_FILE=1
export DCA_WORKSPACE_CONFIG="${DCA_WORKSPACE_CONFIG:-$DCA_ROOT/configs/workspace.json}"
export XDG_CACHE_HOME="$DCA_ROOT/runtime/cache"
export BLACK_CACHE_DIR="$DCA_ROOT/runtime/cache/black"
export RUFF_CACHE_DIR="$DCA_ROOT/runtime/cache/ruff"
export PYTHONDONTWRITEBYTECODE=1
export PATH="$UV_TOOL_BIN_DIR:$DCA_ROOT/.venv/bin:$PATH"
unset PYTHONPATH PYTHONHOME
test -d "$TMPDIR"
test -d "$DCA_ROOT/runtime/cache"
cd "$DCA_ROOT"
exec "$@"
