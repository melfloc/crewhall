#!/usr/bin/env bash
# Builds a release directory:  release.json + crewhall-<ver>-installer.tar.gz (+ .sha256)
# The tarball holds the wheel + installer, so a target needs no source tree.
#
#   build-release.sh [--src DIR] [--out DIR] [--commit SHA]
#
# --src   tree to build from (release.sh passes a `git archive` of the tagged commit)
# --commit baked into the wheel as _build_info.json so the version maps to one commit
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$HERE"; OUT=""; COMMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$(cd "$2" && pwd)"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --commit) COMMIT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
OUT="${OUT:-$HERE/dist}"
cd "$SRC"
read -r VERSION MINPY < <(python3 - <<'PY'
import tomllib, re
c = tomllib.load(open("pyproject.toml", "rb"))["project"]
print(c["version"], re.sub(r"[^0-9.]", "", c["requires-python"]))
PY
)
SCHEMA="$(python3 - <<'PY'
import re
print(re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", open("crewhall/persistence.py").read(), re.M).group(1))
PY
)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
rm -f "$OUT"/crewhall-"$VERSION"*.whl "$OUT/crewhall-$VERSION-installer"* "$OUT/release.json"

if [ -n "$COMMIT" ]; then
  printf '{"commit": "%s", "built": "%s", "channel": "release"}\n' "$COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > crewhall/_build_info.json
fi

echo "==> building wheel $VERSION"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/python" -m pip install --quiet --upgrade pip build >/dev/null
"$WORK/venv/bin/python" -m build --wheel --outdir "$WORK/out" "$SRC" >/dev/null
WHEEL="$(ls "$WORK/out"/crewhall-"$VERSION"-*.whl)"
"$WORK/venv/bin/python" - "$WHEEL" "${COMMIT:-}" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
assert "crewhall/web/static/index.html" in names, "Web UI page missing from wheel"
assert "crewhall/web/static/app.css" in names and "crewhall/web/static/js/core.js" in names, "Web UI assets missing from wheel"
assert "crewhall/web/static/vendor/xterm/xterm.js" in names, "vendored xterm assets missing from wheel"
if sys.argv[2]:
    assert "crewhall/_build_info.json" in names, "build info missing from wheel"
PY

NAME="crewhall-$VERSION-installer"
STAGE="$WORK/$NAME"; mkdir -p "$STAGE/examples"
cp "$WHEEL" "$STAGE/"
cp scripts/install.sh scripts/uninstall.sh "$STAGE/"
cp examples/*.toml "$STAGE/examples/"
cp INSTALL.md README.md CHANGELOG.md "$STAGE/"
chmod +x "$STAGE/install.sh" "$STAGE/uninstall.sh"
tar -C "$WORK" --sort=name --owner=0 --group=0 --numeric-owner -czf "$OUT/$NAME.tar.gz" "$NAME"
SHA="$(sha256sum "$OUT/$NAME.tar.gz" | cut -d' ' -f1)"
echo "$SHA  $NAME.tar.gz" > "$OUT/$NAME.tar.gz.sha256"
python3 - "$OUT/release.json" "$VERSION" "$COMMIT" "$NAME.tar.gz" "$SHA" "$MINPY" "$SCHEMA" <<'PY'
import json, sys, time
_, path, version, commit, tarball, sha, minpy, schema = sys.argv
json.dump({"version": version, "commit": commit or None, "tarball": tarball, "sha256": sha,
           "min_python": ".".join(minpy.split(".")[:2]), "state_schema": int(schema),
           "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "channel": "release"},
          open(path, "w"), indent=2, sort_keys=True)
PY
echo "==> $OUT/$NAME.tar.gz"
cat "$OUT/$NAME.tar.gz.sha256"
