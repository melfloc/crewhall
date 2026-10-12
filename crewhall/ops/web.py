"""Daemon ops: web (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
from typing import Any
from .common import OpsBase


class OpsWeb(OpsBase):

    def _op_frontend_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.frontends.status()

    def _op_frontend_set(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..frontends import FrontendError

        try:
            return self.frontends.set_mode(request["mode"], request.get("port"))
        except FrontendError as exc:
            raise ValueError(str(exc)) from exc
