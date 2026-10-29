"""In-process pub/sub for live execution events (node start/finish, LLM tokens)."""

import asyncio
import time
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self, buffer_ttl: float = 300):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._buffer: dict[str, list[dict]] = defaultdict(list)
        self._finished: dict[str, float] = {}
        self._ttl = buffer_ttl

    def publish(self, channel: str, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        buf = self._buffer[channel]
        # Tokens are high-volume; only keep structural events for replay.
        if event.get("type") != "token":
            buf.append(event)
            if len(buf) > 2000:
                del buf[:500]
        for q in list(self._subs.get(channel, ())):
            q.put_nowait(event)
        if event.get("type") == "execution_finished":
            self._finished[channel] = time.time()
        self._gc()

    def subscribe(self, channel: str) -> tuple[asyncio.Queue, list[dict]]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[channel].add(q)
        return q, list(self._buffer.get(channel, []))

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        self._subs[channel].discard(q)
        if not self._subs[channel]:
            self._subs.pop(channel, None)

    def _gc(self) -> None:
        now = time.time()
        for ch, t in list(self._finished.items()):
            if now - t > self._ttl:
                self._finished.pop(ch, None)
                self._buffer.pop(ch, None)


bus = EventBus()
