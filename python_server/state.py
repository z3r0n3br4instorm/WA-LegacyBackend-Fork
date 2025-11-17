from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Iterable, List, MutableMapping, Optional


@dataclass
class RoomSnapshot:
    room_id: str
    contact_id: str
    is_group: bool
    name: str
    topic: Optional[str] = None
    avatar_url: Optional[str] = None
    last_event_ts: Optional[int] = None
    last_message_id: Optional[str] = None
    unread_count: int = 0
    mute_expiration: int = 0
    participants: List[str] = field(default_factory=list)


@dataclass
class MessageRecord:
    event_id: str
    room_id: str
    contact_id: str
    is_group: bool
    payload: Dict


class MessageCache:
    def __init__(self, max_messages_per_room: int = 512):
        self._cache: MutableMapping[str, Deque[MessageRecord]] = defaultdict(deque)
        self._index: Dict[str, MessageRecord] = {}
        self._max = max_messages_per_room

    def add(self, record: MessageRecord) -> None:
        room_deque = self._cache[record.room_id]
        room_deque.append(record)
        self._index[record.event_id] = record
        while len(room_deque) > self._max:
            removed = room_deque.popleft()
            self._index.pop(removed.event_id, None)

    def get_room(self, room_id: str) -> List[MessageRecord]:
        return list(self._cache.get(room_id, []))

    def get_message(self, event_id: str) -> Optional[MessageRecord]:
        return self._index.get(event_id)

    def clear_room(self, room_id: str) -> None:
        records = self._cache.pop(room_id, [])
        for record in records:
            self._index.pop(record.event_id, None)


class MuteStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._data: Dict[str, int] = {}
        self._lock = threading.Lock()
        if path and path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))
        elif path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")

    def get(self, contact_id: str) -> int:
        return int(self._data.get(contact_id, 0))

    def set(self, contact_id: str, expiration: int) -> None:
        with self._lock:
            if expiration <= 0:
                self._data.pop(contact_id, None)
            else:
                self._data[contact_id] = expiration
            self._flush()

    def _flush(self) -> None:
        if not self._path:
            return
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)


class RoomDirectory:
    def __init__(self):
        self._rooms: Dict[str, RoomSnapshot] = {}

    def upsert(self, snapshot: RoomSnapshot) -> None:
        self._rooms[snapshot.room_id] = snapshot

    def get(self, room_id: str) -> Optional[RoomSnapshot]:
        return self._rooms.get(room_id)

    def all(self) -> Iterable[RoomSnapshot]:
        return list(self._rooms.values())

    def by_contact_id(self, contact_id: str) -> Optional[RoomSnapshot]:
        for snap in self._rooms.values():
            if snap.contact_id == contact_id:
                return snap
        return None
