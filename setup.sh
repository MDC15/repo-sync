#!/usr/bin/env bash
# Bootstrap, test, build, and install repo-sync in a project-local virtualenv.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

log() { printf '\n[setup] %s\n' "$*"; }
die() { printf '\n[setup] ERROR: %s\n' "$*" >&2; exit 1; }

# Example: PYTHON=python3.12 ./setup.sh
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    die "Python 3.9+ was not found. Install Python and rerun this script."
  fi
fi
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python executable '$PYTHON_BIN' was not found."
PYTHON_BIN="$(command -v "$PYTHON_BIN")"

"$PYTHON_BIN" - <<'PY' || die "repo-sync requires Python 3.9 or newer."
import sys
if sys.version_info < (3, 9):
    print(f"Found Python {sys.version.split()[0]}; need Python 3.9+.", file=sys.stderr)
    raise SystemExit(1)
print(f"Using Python {sys.version.split()[0]} ({sys.executable})")
PY

# Reuse an activated venv, otherwise create a clean project-local one.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  VENV_DIR="$VIRTUAL_ENV"
  log "Using active virtual environment: $VENV_DIR"
  PYTHON_CMD="$(command -v python)"
else
  VENV_DIR="${REPO_SYNC_VENV:-$ROOT_DIR/.venv}"
  if [[ "$VENV_DIR" != /* ]]; then
    VENV_DIR="$ROOT_DIR/$VENV_DIR"
  fi
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log "Creating virtual environment: $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" \
      || die "Could not create virtualenv. On Debian/Ubuntu, install the python3-venv package."
  else
    log "Reusing virtual environment: $VENV_DIR"
  fi
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  PYTHON_CMD="$VENV_DIR/bin/python"
fi

[[ -x "$PYTHON_CMD" ]] || die "Could not locate Python inside the selected virtual environment."

log "Checking the setuptools build backend"
BUILD_PYTHON="$PYTHON_CMD"
has_usable_setuptools() {
  "$1" - <<'PY'
try:
    import setuptools
    parts = tuple(int(part) for part in setuptools.__version__.split(".")[:2])
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if parts >= (68, 0) else 1)
PY
}

if ! has_usable_setuptools "$BUILD_PYTHON"; then
  # A base Python may already have the build backend installed even when the
  # newly-created venv is clean. Use it for building only; install the wheel
  # into the project virtualenv below.
  if has_usable_setuptools "$PYTHON_BIN"; then
    BUILD_PYTHON="$PYTHON_BIN"
    log "Using the base Python's existing setuptools for the build"
  else
    log "Installing setuptools>=68 (internet access may be required)"
    "$PYTHON_CMD" -m pip install --disable-pip-version-check --timeout 10 --retries 1 'setuptools>=68' \
      || die "Could not install setuptools>=68. Check internet access or install it manually in the selected Python environment."
  fi
fi

if [[ "${REPO_SYNC_SKIP_TESTS:-0}" != "1" ]]; then
  log "Running unit tests"
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_CMD" -m unittest discover -s "$ROOT_DIR/tests" -v
else
  log "Skipping tests because REPO_SYNC_SKIP_TESTS=1"
fi

log "Building wheel"
mkdir -p "$ROOT_DIR/dist"
"$BUILD_PYTHON" -m pip wheel --disable-pip-version-check --no-deps --no-build-isolation \
  --wheel-dir "$ROOT_DIR/dist" "$ROOT_DIR"

WHEEL_PATH="$("$PYTHON_CMD" - "$ROOT_DIR/dist" <<'PY'
from pathlib import Path
import sys
folder = Path(sys.argv[1])
wheels = sorted(folder.glob("repo_sync_cli-*.whl"), key=lambda p: p.stat().st_mtime)
if not wheels:
    raise SystemExit("No repo_sync_cli wheel was produced in dist/.")
print(wheels[-1].resolve())
PY
)" || die "Build completed without producing an installable wheel."

log "Installing built package: $WHEEL_PATH"
"$PYTHON_CMD" -m pip install --disable-pip-version-check --force-reinstall --no-deps "$WHEEL_PATH"

log "Verifying installation"
export PATH="$VENV_DIR/bin:$PATH"
command -v repo-sync >/dev/null 2>&1 || die "Package installed, but the repo-sync command was not found on PATH."
repo-sync --help >/dev/null || die "The repo-sync command did not pass its smoke test."

log "Setup completed successfully."
printf '\nProject: %s\nVirtualenv: %s\nWheel: %s\n' "$ROOT_DIR" "$VENV_DIR" "$WHEEL_PATH"
printf '\nNext steps:\n'
printf '  source "%s/bin/activate"\n' "$VENV_DIR"
printf '  repo-sync --help\n'
printf '  gh auth login --scopes workflow   # required for GitHub account scanning/sync\n'
printf '  repo-sync github whoami\n'

if ! command -v gh >/dev/null 2>&1; then
  printf '\n[setup] NOTE: GitHub CLI (gh) is not installed. Install it from https://cli.github.com/ to use GitHub account features.\n'
fi
