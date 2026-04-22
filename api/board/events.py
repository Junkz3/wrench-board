"""Tiny synchronous pub/sub for board-level events (e.g. board:loaded)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any


class BoardEventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[topic].append(handler)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for h in self._handlers.get(topic, []):
            h(payload)
