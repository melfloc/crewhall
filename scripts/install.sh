#!/usr/bin/env bash
# crewhall installer (idempotent: running it again upgrades in place).
#
# Works from a source checkout (pyproject.toml next to / above this script) or
# from a release bundle (a crewhall-*.whl next to this script, offline-safe).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${AT_PREFIX:-$HOME/.local/share/crewhall}"
BIN_DIR="${AT_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/crewhall"
PYTHON_BIN=""
INSTALL_DEPS=0
WANT_SERVICE=0
WANT_WEB=0
WEB_PORT=8765
WEB_TAILSCALE=0
SKIP_DOCTOR=0

usage() {
  cat <<USAGE
Usage: install.sh [options]

  --prefix DIR        where the private virtualenv lives   (default: $PREFIX)
  --bin-dir DIR       where the 'crewhall' command is linked (default: $BIN_DIR)
  --python PATH       python >= 3.11 to use (default: auto-detect)
  --install-deps      install missing system packages (tmux) with sudo
  --service           install + start the systemd *user* service for the daemon (starts at boot)
  --web               enable the local Web UI (127.0.0.1:$WEB_PORT) right away
  --web-port N        Web UI port (default $WEB_PORT)
  --tailscale         enable the Web UI over Tailscale right away (token required)
  (switch any time later, no restart needed:  crewhall local | tailscale | off)
  --no-doctor         do not run 'crewhall doctor' at the end
  -h, --help          show this help
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --install-deps) INSTALL_DEPS=1; shift ;;
    --service) WANT_SERVICE=1; shift ;;
    --web) WANT_WEB=1; shift ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    --tailscale) WEB_TAILSCALE=1; shift ;;
    --no-doctor) SKIP_DOCTOR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python >= 3.11
find_python() {
  local c
  for c in "$PYTHON_BIN" python3.14 python3.13 python3.12 python3.11 python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "Python 3.11 or newer is required (found none). Install it, or pass --python PATH."
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
  || die "The 'venv'/'ensurepip' modules are missing (Debian/Ubuntu: sudo apt install python3-venv)."
say "Python: $("$PY" --version) ($PY)"

# ---------------------------------------------------------------- system packages
pkg_hint() {
  if   command -v pacman >/dev/null; then echo "sudo pacman -S --needed tmux"
  elif command -v apt-get >/dev/null; then echo "sudo apt-get install -y tmux"
  elif command -v dnf >/dev/null; then echo "sudo dnf install -y tmux"
  elif command -v zypper >/dev/null; then echo "sudo zypper install -y tmux"
  elif command -v brew >/dev/null; then echo "brew install tmux"
  else echo "install 'tmux' with your package manager"; fi
}
if ! command -v tmux >/dev/null 2>&1; then
  if [ "$INSTALL_DEPS" = 1 ]; then
    say "Installing tmux: $(pkg_hint)"; eval "$(pkg_hint)"
  else
    warn "tmux is not installed (the default agent backend). Run: $(pkg_hint)   (or re-run with --install-deps)"
  fi
fi
for cli in claude opencode; do
  command -v "$cli" >/dev/null 2>&1 || warn "'$cli' CLI not found: you can still install crewhall, but you need at least one agent CLI to create agents."
done

# ---------------------------------------------------------------- what to install
TARGET=""
if compgen -G "$SCRIPT_DIR/crewhall-*.whl" >/dev/null; then
  TARGET="$(ls -1 "$SCRIPT_DIR"/crewhall-*.whl | sort -V | tail -n1)"; MODE="release wheel"
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
  TARGET="$(cd "$SCRIPT_DIR/.." && pwd)"; MODE="source checkout"
elif [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
  TARGET="$SCRIPT_DIR"; MODE="source checkout"
else
  die "Nothing to install: no crewhall-*.whl or pyproject.toml next to this script."
fi
say "Installing from $MODE: $TARGET"

# ---------------------------------------------------------------- virtualenv + install
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  say "Creating virtualenv in $PREFIX/venv"
  mkdir -p "$PREFIX"
  "$PY" -m venv "$PREFIX/venv"
fi
VENV_PY="$PREFIX/venv/bin/python"
"$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
"$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade --force-reinstall --no-deps "$TARGET"
VERSION="$("$VENV_PY" -P -m crewhall --version)"
say "Installed $VERSION"
# Marker that makes this a *managed* install: `crewhall update` only ever touches these.
"$VENV_PY" - "$PREFIX/install.json" <<'PY'
import json, sys, time
from crewhall import __version__
from crewhall.buildinfo import build_info
from crewhall.persistence import SCHEMA_VERSION
info = build_info()
json.dump({"version": __version__, "commit": info.get("commit"), "channel": info["kind"],
           "state_schema": SCHEMA_VERSION, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
          open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY

# ---------------------------------------------------------------- command on PATH
mkdir -p "$BIN_DIR"
LINK="$BIN_DIR/crewhall"
LAUNCHER_MARK="# managed by crewhall install.sh"
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
  if [ -L "$LINK" ] || grep -q "$LAUNCHER_MARK" "$LINK" 2>/dev/null; then rm -f "$LINK"   # ours (older symlink or launcher)
  else cp -p "$LINK" "$LINK.bak.$(date +%s)"; rm -f "$LINK"; warn "existing $LINK was not ours; backed it up"; fi
fi
# A path-based launcher (not a symlink to venv/bin/crewhall): `update` swaps the venv directory,
# and console scripts embed the path they were created in.
printf '#!/bin/sh\n%s\nexec "%s/venv/bin/python" -P -m crewhall "$@"\n' "$LAUNCHER_MARK" "$PREFIX" > "$LINK"
chmod 755 "$LINK"
say "Command: $LINK -> $PREFIX/venv/bin/python -P -m crewhall"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *)
  warn "$BIN_DIR is not on your PATH. Add to your shell profile:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- config scaffolding
mkdir -p "$CONFIG_DIR/teams"; chmod 700 "$CONFIG_DIR"
for ex in profiles.toml; do
  src=""
  for d in "$SCRIPT_DIR/examples" "$SCRIPT_DIR/../examples"; do [ -f "$d/$ex" ] && src="$d/$ex" && break; done
  [ -n "$src" ] && [ ! -e "$CONFIG_DIR/$ex" ] && [ ! -e "$CONFIG_DIR/$ex.example" ] && cp "$src" "$CONFIG_DIR/$ex.example" || true
done
for d in "$SCRIPT_DIR/examples" "$SCRIPT_DIR/../examples"; do
  [ -f "$d/team.toml" ] && [ ! -e "$CONFIG_DIR/teams/example.toml.example" ] && cp "$d/team.toml" "$CONFIG_DIR/teams/example.toml.example" && break || true
done
say "Config dir: $CONFIG_DIR (examples copied as *.example; rename to activate)"

# ---------------------------------------------------------------- services (optional)
AT="$BIN_DIR/crewhall"
if [ "$WANT_SERVICE" = 1 ]; then
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    say "Installing the daemon user service (it also restores the web interfaces you enable)"
    "$AT" service install >/dev/null
    command -v loginctl >/dev/null 2>&1 && warn "To keep the daemon running after you log out (needed on a server): sudo loginctl enable-linger $USER"
  else
    warn "systemd user manager not available: the daemon will start on demand but not at boot."
  fi
fi
# The web interfaces live inside the daemon and are switched at runtime; this only picks the initial mode.
if [ "$WEB_TAILSCALE" = 1 ]; then
  say "Enabling the Web UI over Tailscale (token required)"
  "$AT" tailscale --port "$WEB_PORT" || warn "could not enable Tailscale access; fix the cause and run: crewhall tailscale"
elif [ "$WANT_WEB" = 1 ]; then
  say "Enabling the local Web UI"
  "$AT" local --port "$WEB_PORT" || warn "could not enable the local Web UI; run: crewhall local"
fi

# ---------------------------------------------------------------- verify
if [ "$SKIP_DOCTOR" = 0 ]; then
  say "Environment check"
  "$AT" doctor || warn "doctor reported problems (see above)"
fi
echo
say "Done. Try:  crewhall agent create -t claude -n demo --wait   |   crewhall web"
