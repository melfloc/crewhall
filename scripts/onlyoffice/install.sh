#!/usr/bin/env bash
# Atajo del repositorio para `crewhall office setup`.
#
#   bash scripts/onlyoffice/install.sh
#
# Instala/arranca Docker + OnlyOffice, aplica los ajustes y activa los
# frontends. Idempotente. La vía canónica (también desde un paquete instalado)
# es:  crewhall office setup
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

if command -v crewhall >/dev/null 2>&1; then
  exec crewhall office setup "$@"
fi
exec env PYTHONPATH="$repo" python3 -m crewhall office setup "$@"
