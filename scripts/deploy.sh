#!/usr/bin/env bash
# Ship a *released* version (releases/vX.Y.Z, made by release.sh) to a machine over SSH.
#
#   scripts/deploy.sh HOST [--version X.Y.Z] [--restart] [--force-restart]
#                          [--install-args "..."] [--rotate-signer] [--dry-run]
#
# * Only signed, tagged releases are shipped: nothing from the working tree ever leaves this machine.
# * First time (no crewhall on HOST): unpack + install.sh (+ --install-args, e.g. "--service --tailscale").
# * After that: `crewhall update --from <staged release>` on HOST: verifies sha256 + signature,
#   builds and tests a new venv, swaps atomically, keeps the previous one for rollback.
# * The daemon is NOT restarted unless you pass --restart (restarting closes running agents; it refuses
#   while agents run unless --force-restart).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ $# -ge 1 ] || { sed -n 2,14p "$0"; exit 2; }
HOST="$1"; shift
VERSION=""; RESTART=""; INSTALL_ARGS="--service"; ROTATE=0; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --restart) RESTART="--restart"; shift ;;
    --force-restart) RESTART="--restart --force-restart"; shift ;;
    --install-args) INSTALL_ARGS="$2"; shift 2 ;;
    --rotate-signer) ROTATE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }

# ---- which release
if [ -z "$VERSION" ]; then
  VERSION="$(git tag --list 'v[0-9]*' | sed 's/^v//' | sort -V | tail -n1)"
  [ -n "$VERSION" ] || die "no release tags yet: run scripts/release.sh first"
fi
REL="$ROOT/releases/v$VERSION"
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null || die "v$VERSION is not a tagged release"

# The signing public key is required only once a real release is being shipped.
KEY_PUB="${AT_RELEASE_KEY:?set AT_RELEASE_KEY to the path of your signing key}.pub"
[ -f "$REL/release.json" ] || die "$REL/release.json missing: build it with scripts/release.sh"
[ ! -f "$REL/UNSIGNED" ] || die "v$VERSION was built with --no-sign: refusing to ship it"
TARBALL="$(python3 -c "import json;print(json.load(open('$REL/release.json'))['tarball'])")"
[ -f "$REL/$TARBALL.sig" ] || die "signature $TARBALL.sig missing"
say "deploying v$VERSION (commit $(python3 -c "import json;print(json.load(open('$REL/release.json'))['commit'])")) to $HOST"

# ---- reachability (key-based only: never prompts)
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true >/dev/null 2>&1 \
  || die "cannot ssh to $HOST non-interactively (check ~/.ssh/config, keys, tailscale)"

# ---- trust: the target must trust *this* signing key (first deploy sets it; changes need --rotate-signer)
[ -f "$KEY_PUB" ] || die "public key $KEY_PUB not found"
LINE="$(printf 'agent-terminal-release namespaces="agent-terminal" %s' "$(cut -d' ' -f1,2 "$KEY_PUB")")"
REMOTE_SIGNERS='$HOME/.config/crewhall/allowed_signers'
CURRENT="$(ssh "$HOST" "cat $REMOTE_SIGNERS 2>/dev/null || true")"
if [ -z "$CURRENT" ]; then
  say "first deploy: trusting signer $(ssh-keygen -lf "$KEY_PUB" | cut -d' ' -f2) on $HOST"
  run ssh "$HOST" "mkdir -p \$HOME/.config/crewhall && chmod 700 \$HOME/.config/crewhall && printf '%s\n' '$LINE' > $REMOTE_SIGNERS && chmod 600 $REMOTE_SIGNERS"
elif [ "$CURRENT" != "$LINE" ]; then
  [ "$ROTATE" = 1 ] || die "$HOST trusts a different signing key. If you really rotated yours, re-run with --rotate-signer"
  say "rotating the trusted signer on $HOST"
  run ssh "$HOST" "printf '%s\n' '$LINE' > $REMOTE_SIGNERS && chmod 600 $REMOTE_SIGNERS"
fi

# ---- stage
STAGE="\$HOME/.cache/crewhall-updates/v$VERSION"
say "uploading release files"
run ssh "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
run scp -q "$REL/release.json" "$REL/$TARBALL" "$REL/$TARBALL.sig" "$HOST:.cache/crewhall-updates/v$VERSION/"

AT='$HOME/.local/bin/crewhall'
if ssh "$HOST" "test -x $AT"; then
  say "updating the installation"
  rc=0; run ssh "$HOST" "$AT update --from $STAGE $RESTART" || rc=$?
  if [ "$rc" = 3 ]; then
    say "installed, but the daemon was NOT restarted (agents are running). When idle: ssh $HOST '$AT update --restart'"
  elif [ "$rc" != 0 ]; then exit "$rc"; fi
else
  say "first installation on $HOST (install.sh $INSTALL_ARGS)"
  SHA="$(python3 -c "import json;print(json.load(open('$REL/release.json'))['sha256'])")"
  run ssh "$HOST" "cd $STAGE && echo '$SHA  $TARBALL' | sha256sum -c - && \
     ssh-keygen -Y verify -f $REMOTE_SIGNERS -I agent-terminal-release -n agent-terminal -s $TARBALL.sig < $TARBALL >/dev/null && \
     tar xzf $TARBALL && cd crewhall-*-installer && ./install.sh $INSTALL_ARGS"
fi

say "verification on $HOST"
run ssh "$HOST" "$AT update --status; $AT doctor | tail -4"
echo
echo "Roll back if needed:  ssh $HOST '$AT update --rollback --restart'"
