"""Device contracts (Protocols) and scriptable mock implementations.

Real vendor SDK adapters implement the same Protocols. The mocks can inject faults and delays so
retry, timeout and duplicate handling are testable without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw

from app.db import utcnow
from app.errors import DeviceFault
from app.rfid.events import Event, EventBus

# ---------- contracts ----------


class TagReader(Protocol):
    device_id: str

    def read_tags(self) -> list[str]: ...
    def write_tag(self, uid: str, payload: dict) -> None: ...
    def set_security(self, uid: str, secured: bool) -> None: ...
    def signal(self, ok: bool) -> None: ...


class Handheld(Protocol):
    device_id: str

    def read_batch(self) -> list[str]: ...


class Gate(Protocol):
    device_id: str
    location: str

    def sound_alarm(self) -> None: ...


class Camera(Protocol):
    def capture(self, path: Path, caption: str) -> Path: ...


class LabelPrinter(Protocol):
    def print_label(self, text: str) -> None: ...


class CardReader(Protocol):
    def read_card(self) -> str | None: ...


# ---------- mocks ----------


@dataclass
class TagMemory:
    payload: dict = field(default_factory=dict)
    afi: int | None = None


class MockStaffReader:
    """Desk reader. Tags are 'placed' on it by tests or the demo script."""

    def __init__(self, device_id: str = "reader-desk-1", *, afi_secured: int = 0xC2, afi_unsecured: int = 0x07):
        self.device_id = device_id
        self.afi_secured, self.afi_unsecured = afi_secured, afi_unsecured
        self.memory: dict[str, TagMemory] = {}
        self.on_pad: list[str] = []
        self.signals: list[bool] = []
        self._faults = 0

    def place(self, *uids: str) -> None:
        self.on_pad = list(uids)
        for uid in uids:
            self.memory.setdefault(uid, TagMemory())

    def fail_next(self, n: int = 1) -> None:
        self._faults = n

    def _check(self) -> None:
        if self._faults > 0:
            self._faults -= 1
            raise DeviceFault("DEVICE_FAULT", f"{self.device_id} did not respond")

    def read_tags(self) -> list[str]:
        self._check()
        return list(self.on_pad)

    def write_tag(self, uid: str, payload: dict) -> None:
        self._check()
        mem = self.memory.setdefault(uid, TagMemory())
        mem.payload = dict(payload)
        mem.afi = payload.get("afi")

    def set_security(self, uid: str, secured: bool) -> None:
        self._check()
        mem = self.memory.setdefault(uid, TagMemory())
        mem.afi = self.afi_secured if secured else self.afi_unsecured
        mem.payload["afi"] = mem.afi

    def signal(self, ok: bool) -> None:
        self.signals.append(ok)


class MockHandheld:
    def __init__(self, device_id: str = "handheld-1"):
        self.device_id = device_id
        self._queue: list[str] = []

    def load(self, uids: list[str]) -> None:
        self._queue = list(uids)

    def read_batch(self) -> list[str]:
        batch, self._queue = self._queue, []
        return batch


class MockGate:
    """Gate that publishes tag reads and footfall to the bus, like a real controller would."""

    def __init__(self, bus: EventBus, device_id: str = "gate-main", location: str = "MAIN"):
        self.bus, self.device_id, self.location = bus, device_id, location
        self.alarms_sounded = 0

    def walk_through(self, uids: list[str], people: int = 1, *, event_id: str | None = None) -> None:
        self.bus.publish(Event("gate.footfall", self.device_id, {"count": people, "location": self.location}))
        for uid in uids:
            kwargs = {"event_id": event_id} if event_id else {}
            self.bus.publish(Event("gate.tag_read", self.device_id, {"uid": uid, "location": self.location}, **kwargs))

    def sound_alarm(self) -> None:
        self.alarms_sounded += 1


class MockCamera:
    def capture(self, path: Path, caption: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (640, 360), (24, 32, 48))
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), "MOCK CCTV FRAME", fill=(240, 240, 240))
        draw.text((20, 60), caption, fill=(240, 180, 60))
        draw.text((20, 100), utcnow().isoformat(timespec="seconds") + " UTC", fill=(160, 170, 190))
        img.save(path, format="PNG")
        return path


class MockPrinter:
    def __init__(self):
        self.labels: list[str] = []

    def print_label(self, text: str) -> None:
        self.labels.append(text)


class MockCardReader:
    def __init__(self):
        self.current: str | None = None

    def present(self, uid: str | None) -> None:
        self.current = uid

    def read_card(self) -> str | None:
        return self.current


@dataclass
class DeviceHub:
    """The set of devices one site uses. Swap fields for real adapters in production."""

    bus: EventBus
    reader: TagReader
    handheld: Handheld
    gate: MockGate
    camera: Camera
    printer: LabelPrinter
    card_reader: CardReader


def mock_hub(*, afi_secured: int = 0xC2, afi_unsecured: int = 0x07) -> DeviceHub:
    bus = EventBus(backoff=0)
    return DeviceHub(
        bus=bus,
        reader=MockStaffReader(afi_secured=afi_secured, afi_unsecured=afi_unsecured),
        handheld=MockHandheld(),
        gate=MockGate(bus),
        camera=MockCamera(),
        printer=MockPrinter(),
        card_reader=MockCardReader(),
    )
