from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


logger = logging.getLogger(__name__)


def contact_id_from_room(room_id: str) -> str:
    digest = hashlib.sha1(room_id.encode("utf-8")).hexdigest()
    return digest[:16]


def contact_id_from_user(user_id: str) -> str:
    digest = hashlib.md5(user_id.encode("utf-8")).hexdigest()
    return digest[:16]


def jid_from_contact(contact_id: str, is_group: bool) -> str:
    suffix = "g.us" if is_group else "c.us"
    return f"{contact_id}@{suffix}"


def build_message_id(event_id: str, contact_id: str, is_group: bool) -> Dict[str, Any]:
    jid = jid_from_contact(contact_id, is_group)
    return {
        "_serialized": event_id,
        "fromMe": False,
        "remote": jid,
        "id": event_id,
        "participant": {"user": contact_id},
    }


@contextmanager
def temp_file_with_suffix(suffix: str) -> Iterable[Path]:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        path = Path(handle.name)
    try:
        yield path
    finally:
        if path.exists():
            path.unlink(missing_ok=True)


def b64_to_bytes(data: str) -> bytes:
    return base64.b64decode(data.encode("utf-8"))


def bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def ensure_dir(path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_extension_for_mime(mimetype: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/aac": ".aac",
        "audio/wav": ".wav",
    }
    return mapping.get(mimetype, ".bin")


def random_boundary() -> str:
    return secrets.token_hex(8)
