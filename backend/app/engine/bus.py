"""In-process pub/sub for live execution events (node start/finish, LLM tokens).

Built for production volume: an event costs nothing unless someone listens. Events are delivered to live
subscribers; a replay buffer (for a panel that connects mid-run) is kept only for channels marked with
keep(): editor runs, and workflows open in an editor. Thousands of unwatched webhook executions per second
therefore leave nothing behind in memory. Old buffers are dropped by a periodic sweep, not on every event.
"""

import asyncio
import time
from collections import defaultdict
from typing import Any

BUFFER_MAX = 2000
SWEEP_EVERY = 30.0


class EventBus:
    def __init__(self, buffer_ttl: float = 300):
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._buffer: dict[str, list[dict]] = {}
        self._keep: set[str] = set()
        self._finished: dict[str, float] = {}
        self._idle: dict[str, float] = {}  # channel → when its last subscriber left
        self._ttl = buffer_ttl
        self._last_sweep = time.monotonic()

    def watching(self, channel: str) -> bool:
        return bool(self._subs.get(channel))

    def keep(self, channel: str) -> None:
        """Buffer this channel's events for replay (until it finishes + TTL)."""
        self._keep.add(channel)

    def publish(self, channel: str, event: dict[str, Any]) -> None:
        subs = self._subs.get(channel)
        if not subs and channel not in self._keep:
            return  # nobody listens and nobody will replay it: the common production case
        event = {"ts": time.time(), **event}
        # Tokens are high-volume; only keep structural events for replay.
        if channel in self._keep and event.get("type") != "token":
            buf = self._buffer.setdefault(channel, [])
            buf.append(event)
            if len(buf) > BUFFER_MAX:
                del buf[:500]
        for q in list(subs or ()):
            q.put_nowait(event)
        if event.get("type") == "execution_finished":
            self._finished[channel] = time.monotonic()
        if time.monotonic() - self._last_sweep > SWEEP_EVERY:
            self._sweep()

    def subscribe(self, channel: str) -> tuple[asyncio.Queue, list[dict]]:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[channel].add(q)
        self._idle.pop(channel, None)
        if channel.startswith("wf:"):
            self.keep(channel)  # an editor is open on this workflow: let it replay after a reconnect
        return q, list(self._buffer.get(channel, []))

    def unsubscribe(self, channel: str, q: asyncio.Queue) -> None:
        self._subs[channel].discard(q)
        if not self._subs[channel]:
            self._subs.pop(channel, None)
            self._idle[channel] = time.monotonic()

    def _sweep(self) -> None:
        now = self._last_sweep = time.monotonic()
        for ch, t in list(self._finished.items()):
            if now - t > self._ttl:
                self._finished.pop(ch, None)
                self._buffer.pop(ch, None)
                self._keep.discard(ch)
        for ch, t in list(self._idle.items()):  # workflow channels nobody has watched for a while
            if now - t > self._ttl and not self._subs.get(ch):
                self._idle.pop(ch, None)
                self._buffer.pop(ch, None)
                self._keep.discard(ch)


bus = EventBus()
