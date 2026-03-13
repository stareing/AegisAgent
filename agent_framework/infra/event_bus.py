from __future__ import annotations

from typing import Any, Callable

from blinker import Namespace


class EventBus:
    """Simple event bus backed by blinker."""

    def __init__(self) -> None:
        self._ns = Namespace()

    def subscribe(self, event_name: str, handler: Callable[..., Any]) -> None:
        signal = self._ns.signal(event_name)
        signal.connect(handler)

    def publish(self, event_name: str, payload: Any = None) -> None:
        signal = self._ns.signal(event_name)
        signal.send(self, payload=payload)

    def unsubscribe(self, event_name: str, handler: Callable[..., Any]) -> None:
        signal = self._ns.signal(event_name)
        signal.disconnect(handler)
