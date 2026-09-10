#!/bin/sh
# codex-swap installer.
#
#   curl -LsSf https://raw.githubusercontent.com/wonjun-lab/codex-swap/main/install.sh | sh
#
# There is nothing here you could not type yourself — it picks whichever of uv, pipx or
# pip you already have and runs one install command with it. It is short on purpose:
# piping a script into a shell is only reasonable when the script fits on a screen.
#
# It installs into an isolated environment when it can (uv, pipx) and falls back to
# `pip --user`. It never uses sudo and never touches a system Python.

set -eu

REPO="${CODEX_SWAP_REPO:-https://github.com/wonjun-lab/codex-swap.git}"
SOURCE="git+${REPO}"
MIN_PY="3.11"

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- what will run it ---------------------------------------------------------------
#
# Codex-swap needs a Python of its own to live in, but it also needs the *codex* CLI at
# runtime. Checking for codex here would be a lie about what this script does, so it only
# warns: you can install the switcher first and log in afterwards.

python_ok() {
    have "$1" || return 1
    "$1" - <<EOF >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info[:2] >= tuple(int(p) for p in "${MIN_PY}".split(".")) else 1)
EOF
}

if have uv; then
    say "installing with uv"
    uv tool install --force "$SOURCE"
elif have pipx; then
    say "installing with pipx"
    pipx install --force "$SOURCE"
else
    PY=""
    for candidate in python3 python; do
        if python_ok "$candidate"; then PY="$candidate"; break; fi
    done
    [ -n "$PY" ] || die "need Python ${MIN_PY}+, or uv (https://docs.astral.sh/uv/), or pipx"
    say "installing with $PY -m pip --user"
    say "  (uv or pipx would keep this isolated instead: https://docs.astral.sh/uv/)"
    "$PY" -m pip install --user --upgrade --force-reinstall "$SOURCE"
fi

# --- did it land somewhere you can reach? -------------------------------------------
#
# A successful install that you cannot run is the most confusing outcome of the three, so
# say the quiet part out loud rather than leaving `command not found` to explain it.

if have codex-swap; then
    say ""
    say "installed: $(command -v codex-swap)"
else
    say ""
    say "installed, but codex-swap is not on your PATH yet."
    say "Add one of these to your shell profile, whichever exists:"
    say "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    have uv && say "  # uv also offers: uv tool update-shell"
fi

# --- hand over to init ---------------------------------------------------------------
#
# Everything left to do depends on this machine: whether codex is installed, whether the
# ChatGPT desktop app owns ~/.codex, whether the shell is wired, how many accounts exist.
# Printing a fixed list of next steps here would be guessing at all four. `init` looks.

if have codex-swap; then
    say ""
    codex-swap init || true
else
    say ""
    say "next, once codex-swap is on your PATH:"
    say "  codex-swap init"
fi
