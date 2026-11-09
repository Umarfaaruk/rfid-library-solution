"""Device-neutral events with idempotent delivery, bounded retries and a local spool.

The spool is what lets the gate keep working while the application layer is down: events that
cannot be handled are kept and replayed in order, and duplicates are dropped by event id.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from app.db import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    type: str
    device_id: str
    payload: dict
    event_id: str = field(default_factory=lambda: str(uuid4()))
    at: datetime = field(default_factory=utcnow)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        backoff: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
        seen_limit: int = 10_000,
    ):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = seen_limit
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self.spool: list[tuple[Event, Handler]] = []
        self.delivered = 0
        self.duplicates = 0

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> bool:
        """Deliver to subscribers. Returns False if this event id was already processed."""
        if event.event_id in self._seen:
            self.duplicates += 1
            return False
        self._remember(event.event_id)
        for handler in self._handlers.get(event.type, []):
            self._deliver(event, handler)
        return True

    def _deliver(self, event: Event, handler: Handler) -> None:
        for attempt in range(self.max_retries):
            try:
                handler(event)
                self.delivered += 1
                return
            except Exception as exc:
                log.warning("handler failed for %s attempt %d: %s", event.type, attempt + 1, exc)
                if attempt < self.max_retries - 1:
                    self._sleep(self.backoff * (2**attempt))
        self.spool.append((event, handler))

    def replay(self) -> int:
        """Retry spooled events once each, in arrival order. Returns how many succeeded."""
        pending, self.spool = self.spool, []
        done = 0
        for event, handler in pending:
            try:
                handler(event)
                self.delivered += 1
                done += 1
            except Exception as exc:
                log.warning("replay failed for %s: %s", event.type, exc)
                self.spool.append((event, handler))
        return done

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)
