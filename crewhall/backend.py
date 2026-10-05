from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import InteractiveSession
    from .types import SessionSpec


class Backend(ABC):
    name = "backend"

    def __init__(self) -> None:
        self.session: InteractiveSession | None = None

    def bind(self, session: InteractiveSession) -> None:
        self.session = session

    @abstractmethod
    def start(self, spec: SessionSpec) -> None: ...

    @abstractmethod
    def write(self, text: str) -> None: ...

    @abstractmethod
    def send_key(self, key: str) -> None: ...

    @abstractmethod
    def capture(self) -> str: ...

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    def terminate(self) -> None: ...

    @abstractmethod
    def kill(self) -> None: ...

    @abstractmethod
    def poll(self) -> int | None: ...

    def pid(self) -> int | None:
        return None

    def meta(self) -> dict[str, Any]:
        return {}

    def wait(self, timeout: float | None = None) -> int | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def close(self) -> None:
        return None
