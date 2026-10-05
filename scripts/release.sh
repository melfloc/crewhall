#!/usr/bin/env bash
# Cut a release from the main line. Everything that reaches another machine goes through here.
#
#   scripts/release.sh [--key PRIVATE_KEY] [--no-sign]
#
# Gates (all must pass): on main · clean tree · pyproject == __init__ == CHANGELOG ·
# the tag does not exist and the version is newer than the last tag · the full test suite passes.
# The release is built from `git archive` of the commit (never from the working tree), signed with
# your SSH key, self-verified, and only then tagged vX.Y.Z. Output: releases/vX.Y.Z/
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
KEY="${AT_RELEASE_KEY:-}"; SIGN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEY="$2"; shift 2 ;;
    --no-sign) SIGN=0; shift ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ "$SIGN" = 1 ]; then
  KEY="${KEY:?set AT_RELEASE_KEY (or pass --key) to the path of your signing private key}"
fi
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

say "gates"
VERSION="$(python3 scripts/release_check.py gates)"
COMMIT="$(git rev-parse --short HEAD)"
say "releasing v$VERSION from $COMMIT"

say "running the full test suite"
python3 -m unittest discover -s tests -t . >/tmp/at-release-tests.log 2>&1 \
  || { tail -25 /tmp/at-release-tests.log; echo "RELEASE GATE FAILED: tests" >&2; exit 1; }
tail -3 /tmp/at-release-tests.log | grep -E "^(Ran|OK)" || true

OUT="$ROOT/releases/v$VERSION"
rm -rf "$OUT"; mkdir -p "$OUT"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git archive HEAD | tar -x -C "$WORK"
say "building from the commit (not from the working tree)"
bash scripts/build-release.sh --src "$WORK" --out "$OUT" --commit "$COMMIT"
python3 scripts/release_check.py notes > "$OUT/NOTES.md"

if [ "$SIGN" = 1 ]; then
  [ -f "$KEY" ] || { echo "signing key $KEY not found (use --key, or --no-sign for a local-only build)" >&2; exit 1; }
  TARBALL="$OUT/$(python3 -c "import json;print(json.load(open('$OUT/release.json'))['tarball'])")"
  say "signing with $KEY"
  rm -f "$TARBALL.sig"   # ssh-keygen would silently keep a stale signature
  ssh-keygen -Y sign -f "$KEY" -n agent-terminal "$TARBALL" >/dev/null
  # self-check: the signature must verify against the matching public key
  PUB="$(cat "$KEY.pub" 2>/dev/null || ssh-keygen -y -f "$KEY")"
  printf 'agent-terminal-release namespaces="agent-terminal" %s\n' "$(echo "$PUB" | cut -d' ' -f1,2)" > "$WORK/allowed"
  ssh-keygen -Y verify -f "$WORK/allowed" -I agent-terminal-release -n agent-terminal -s "$TARBALL.sig" < "$TARBALL" >/dev/null \
    || { echo "RELEASE GATE FAILED: signature self-check" >&2; exit 1; }
  echo "signature verified"
else
  echo "(unsigned build: scripts/deploy.sh will refuse to ship it)" > "$OUT/UNSIGNED"
fi

git tag -a "v$VERSION" -m "crewhall $VERSION" 
say "tagged v$VERSION  ->  $OUT"
echo "Deploy with:  scripts/deploy.sh <ssh-host>"
