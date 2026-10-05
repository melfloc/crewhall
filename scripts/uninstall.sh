#!/usr/bin/env bash
# Removes what install.sh created. Your config/state are kept unless --purge.
set -euo pipefail
PREFIX="${AT_PREFIX:-$HOME/.local/share/crewhall}"
BIN_DIR="${AT_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/crewhall"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/crewhall"
PURGE=0; YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) echo "Usage: uninstall.sh [--prefix DIR] [--bin-dir DIR] [--purge] [--yes]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

AT="$BIN_DIR/crewhall"
if [ -x "$PREFIX/venv/bin/python" ] && [ -x "$AT" ]; then
  say "Stopping services and daemon (running agents will be closed)"
  "$AT" service uninstall-web >/dev/null 2>&1 || true
  "$AT" service uninstall >/dev/null 2>&1 || true
  "$AT" daemon stop >/dev/null 2>&1 || true
fi
LINK="$BIN_DIR/crewhall"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$PREFIX/venv/bin/crewhall" ]; then rm -f "$LINK"; say "Removed $LINK"
elif grep -q "managed by crewhall install.sh" "$LINK" 2>/dev/null; then rm -f "$LINK"; say "Removed $LINK"; fi
if [ -d "$PREFIX/venv" ]; then rm -rf "$PREFIX/venv"; rmdir "$PREFIX" 2>/dev/null || true; say "Removed $PREFIX/venv"; fi
if [ "$PURGE" = 1 ]; then
  if [ "$YES" = 0 ]; then
    printf 'Delete config (%s) and state (%s)? [y/N] ' "$CONFIG_DIR" "$STATE_DIR"; read -r ans
    case "$ans" in y|Y|yes) ;; *) echo "kept."; exit 0 ;; esac
  fi
  rm -rf "$CONFIG_DIR" "$STATE_DIR"; say "Purged config and state"
else
  say "Kept config ($CONFIG_DIR) and state ($STATE_DIR). Use --purge to remove them."
fi
