"""Static asset routes (Handler mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import os

from ..shared import INDEX, STATIC, _STATIC_TYPES


class StaticRoutes:
    def _serve_static(self, rel: str) -> None:
        full = os.path.realpath(os.path.join(STATIC, rel))
        ctype = _STATIC_TYPES.get(os.path.splitext(full)[1].lower())
        if not full.startswith(STATIC + os.sep) or ctype is None or not os.path.isfile(full):
            return self._error("not found", 404)
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        try:
            with open(INDEX, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error("index missing", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
