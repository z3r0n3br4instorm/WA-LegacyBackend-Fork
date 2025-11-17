from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from nio import (
    AsyncClient,
    DownloadError,
    DownloadResponse,
    LocalProtocolError,
    RoomGetEventError,
    RoomGetEventResponse,
    RoomMessagesError,
    RoomReadMarkersError,
    RoomSendResponse,
    SyncError,
    SyncResponse,
    UploadError,
    UploadResponse,
)
from nio.events.room_events import RoomCreateEvent

from .config import AppConfig
from .media import (
    convert_audio_to_mp3,
    convert_video_to_quicktime,
    convert_voice_note_to_ogg,
    generate_thumbnail_from_video,
)
from .socket_server import NotificationSocketServer
from .state import MessageCache, MessageRecord, RoomDirectory, RoomSnapshot, MuteStore
from .utils import (
    b64_to_bytes,
    contact_id_from_room,
    contact_id_from_user,
    jid_from_contact,
    media_extension_for_mime,
)


logger = logging.getLogger(__name__)

_ROOM_CREATE_PATCHED = False
_MISSING_CREATOR_LOGGED = False


def _patch_room_create_event() -> None:
    """Work around rooms missing optional fields in Matrix create events."""
    global _ROOM_CREATE_PATCHED
    if _ROOM_CREATE_PATCHED:
        return

    original_from_dict = RoomCreateEvent.from_dict.__func__

    def _safe_from_dict(cls, parsed_dict: Dict[str, Any]):
        global _MISSING_CREATOR_LOGGED
        content = parsed_dict.get("content") or {}
        patched_dict = parsed_dict
        patched_content: Optional[Dict[str, Any]] = None

        def ensure_content() -> Dict[str, Any]:
            nonlocal patched_content, patched_dict
            if patched_content is None:
                patched_content = dict(content)
                patched_dict = dict(parsed_dict)
                patched_dict["content"] = patched_content
            return patched_content

        if "creator" not in content:
            fallback_creator = parsed_dict.get("sender", "")
            ensure_content()["creator"] = fallback_creator
            if not _MISSING_CREATOR_LOGGED:
                logger.warning(
                    "Matrix create event missing 'creator'; using sender '%s'",
                    fallback_creator,
                )
                _MISSING_CREATOR_LOGGED = True

        if "m.federate" not in content:
            ensure_content()["m.federate"] = True

        if "room_version" not in content:
            ensure_content()["room_version"] = "1"

        if "type" not in content:
            ensure_content()["type"] = ""

        return original_from_dict(cls, patched_dict)

    RoomCreateEvent.from_dict = classmethod(_safe_from_dict)
    _ROOM_CREATE_PATCHED = True


_patch_room_create_event()


class MatrixBridge:
    def __init__(self, config: AppConfig, notifier: NotificationSocketServer):
        self.config = config
        self.notifier = notifier
        self.state = RoomDirectory()
        self.messages = MessageCache()
        mute_store_path = (
            config.matrix.store_path / "mutes.json" if config.matrix.store_path else None
        )
        self.mutes = MuteStore(path=mute_store_path)
        self.client = AsyncClient(
            homeserver=config.matrix.homeserver,
            user=config.matrix.user_id,
            device_id=config.matrix.device_id,
            store_path=str(config.matrix.store_path) if config.matrix.store_path else None,
        )
        self.client.access_token = config.matrix.access_token
        self.client.verify_ssl = config.matrix.verify_ssl
        self.client.add_response_callback(self._on_sync, SyncResponse)
        self._http = httpx.AsyncClient(
            base_url=config.matrix.homeserver.rstrip("/"),
            headers={"Authorization": f"Bearer {config.matrix.access_token}"},
            timeout=30.0,
            verify=config.matrix.verify_ssl,
        )
        self._ready = asyncio.Event()
        self._sync_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._room_contact_map: Dict[str, str] = {}
        self._user_contact_map: Dict[str, str] = {}
        self.user_profiles: Dict[str, Dict[str, Any]] = {}

    async def start(self) -> None:
        await self._initial_sync()
        self._sync_task = asyncio.create_task(self._sync_forever())

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
        await self.client.close()
        await self._http.aclose()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def get_chats(self) -> Dict[str, Any]:
        await self.wait_ready()
        chats = []
        groups = []
        for snapshot in self.state.all():
            chat_dict = self._snapshot_to_chat(snapshot)
            chats.append(chat_dict)
            if snapshot.is_group:
                groups.append(chat_dict)
        chats.sort(key=lambda c: c.get("timestamp", 0), reverse=True)
        return {"chatList": chats, "groupList": groups}

    async def get_contacts(self) -> Dict[str, Any]:
        await self.wait_ready()
        contacts = [self._build_self_contact()]
        seen = {contacts[0]["number"]}
        for snapshot in self.state.all():
            if not snapshot.is_group:
                contact = self._snapshot_to_contact(snapshot)
                if contact["number"] not in seen:
                    contacts.append(contact)
                    seen.add(contact["number"])
            for participant in snapshot.participants:
                participant_contact = self._participant_to_contact(participant)
                if participant_contact["number"] not in seen:
                    contacts.append(participant_contact)
                    seen.add(participant_contact["number"])
        contacts_sorted = sorted(contacts, key=lambda item: item.get("name", "").lower())
        return {"contactList": contacts_sorted}

    async def get_groups(self) -> Dict[str, Any]:
        await self.wait_ready()
        groups = [self._snapshot_to_chat(snapshot) for snapshot in self.state.all() if snapshot.is_group]
        return {"groupList": groups}

    async def get_room_by_contact(self, contact_id: str) -> RoomSnapshot:
        await self.wait_ready()
        snapshot = self.state.by_contact_id(contact_id)
        if not snapshot:
            raise ValueError(f"Unknown contact id {contact_id}")
        return snapshot

    async def fetch_messages(
        self, snapshot: RoomSnapshot, limit: int = 100, direction: str = "b"
    ) -> List[Dict]:
        await self.wait_ready()
        start_token = self.client.sync_token
        if not start_token:
            raise RuntimeError("Matrix client not synced yet")
        resp = await self.client.room_messages(
            room_id=snapshot.room_id, start=start_token, limit=limit, direction=direction
        )
        if isinstance(resp, RoomMessagesError):
            raise RuntimeError(resp.message)
        results: List[Dict] = []
        for event in reversed(resp.chunk):
            converted = self._convert_message(snapshot, event)
            if converted:
                record = MessageRecord(
                    event_id=converted["id"]["_serialized"],
                    room_id=snapshot.room_id,
                    contact_id=snapshot.contact_id,
                    is_group=snapshot.is_group,
                    payload=converted,
                )
                self.messages.add(record)
                results.append(converted)
        return results

    async def sync_chat(self, snapshot: RoomSnapshot, limit: int = 200) -> None:
        await self.fetch_messages(snapshot, limit=limit)

    async def send_text_message(
        self, snapshot: RoomSnapshot, message_text: str, reply_to: Optional[str] = None
    ) -> RoomSendResponse:
        content: Dict[str, Any] = {"msgtype": "m.text", "body": message_text}
        self._apply_reply(content, reply_to)
        return await self._room_send(snapshot, content)

    async def send_voice_note(
        self, snapshot: RoomSnapshot, audio_bytes: bytes, reply_to: Optional[str] = None
    ) -> RoomSendResponse:
        ogg_path = convert_voice_note_to_ogg(audio_bytes)
        data = ogg_path.read_bytes()
        ogg_path.unlink(missing_ok=True)
        content_uri = await self._upload_media(data, "audio/ogg", "voice-note.ogg")
        content = {
            "msgtype": "m.audio",
            "body": "Voice note",
            "url": content_uri,
            "info": {"mimetype": "audio/ogg", "size": len(data)},
            "org.matrix.msc2516.voice": {},
        }
        self._apply_reply(content, reply_to)
        return await self._room_send(snapshot, content)

    async def _room_send(self, snapshot: RoomSnapshot, content: Dict[str, Any]) -> RoomSendResponse:
        resp = await self.client.room_send(
            room_id=snapshot.room_id, message_type="m.room.message", content=content
        )
        if isinstance(resp, LocalProtocolError):
            raise RuntimeError(str(resp))
        return resp

    def _apply_reply(self, content: Dict[str, Any], reply_to: Optional[str]) -> None:
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}

    async def _upload_media(self, data: bytes, mimetype: str, filename: str) -> str:
        resp = await self.client.upload(data, content_type=mimetype, filename=filename)
        if isinstance(resp, UploadError):
            raise RuntimeError(resp.message)
        assert isinstance(resp, UploadResponse)
        return resp.content_uri

    async def send_image_message(
        self,
        snapshot: RoomSnapshot,
        image_bytes: bytes,
        mimetype: str,
        caption: Optional[str],
        reply_to: Optional[str] = None,
    ) -> RoomSendResponse:
        ext = media_extension_for_mime(mimetype)
        filename = f"photo{ext}"
        content_uri = await self._upload_media(image_bytes, mimetype, filename)
        content = {
            "msgtype": "m.image",
            "body": caption or "Photo",
            "url": content_uri,
            "info": {"mimetype": mimetype, "size": len(image_bytes)},
        }
        self._apply_reply(content, reply_to)
        return await self._room_send(snapshot, content)

    async def set_typing(self, snapshot: RoomSnapshot, is_voice_note: bool) -> None:
        await self.client.room_typing(snapshot.room_id, typing_state=True, timeout=6000)

    async def clear_typing(self, snapshot: RoomSnapshot) -> None:
        await self.client.room_typing(snapshot.room_id, typing_state=False)

    async def mark_read(self, snapshot: RoomSnapshot) -> None:
        if not snapshot.last_message_id:
            return
        resp = await self.client.room_read_markers(
            room_id=snapshot.room_id, fully_read_event=snapshot.last_message_id
        )
        if isinstance(resp, RoomReadMarkersError):
            raise RuntimeError(resp.message)
        snapshot.unread_count = 0

    async def delete_message(self, snapshot: RoomSnapshot, event_id: str) -> None:
        await self.client.room_redact(snapshot.room_id, event_id, reason="Deleted via WhatsAppX bridge")

    async def leave_room(self, snapshot: RoomSnapshot) -> None:
        await self.client.room_leave(snapshot.room_id)

    async def delete_chat_history(self, snapshot: RoomSnapshot) -> None:
        self.messages.clear_room(snapshot.room_id)

    async def get_message_record(self, event_id: str) -> MessageRecord:
        record = self.messages.get_message(event_id)
        if record:
            return record
        for snapshot in self.state.all():
            resp = await self.client.room_get_event(snapshot.room_id, event_id)
            if isinstance(resp, RoomGetEventError):
                continue
            if isinstance(resp, RoomGetEventResponse):
                converted = self._convert_message(snapshot, resp.event)
                if converted:
                    new_record = MessageRecord(
                        event_id=event_id,
                        room_id=snapshot.room_id,
                        contact_id=snapshot.contact_id,
                        is_group=snapshot.is_group,
                        payload=converted,
                    )
                    self.messages.add(new_record)
                    return new_record
        raise KeyError(event_id)

    async def download_media(self, event_id: str) -> tuple[bytes, str]:
        record = await self.get_message_record(event_id)
        media_url = record.payload.get("_data", {}).get("mxcUrl")
        if not media_url:
            raise ValueError("Message has no media attached")
        resp = await self.client.download(media_url)
        if isinstance(resp, DownloadError):
            raise RuntimeError(resp.message)
        assert isinstance(resp, DownloadResponse)
        mimetype = resp.content_type or "application/octet-stream"
        return resp.body, mimetype

    async def get_profile_media(self, contact_id: str, is_group: bool) -> Optional[tuple[bytes, str]]:
        avatar_url = None
        if is_group:
            snapshot = await self.get_room_by_contact(contact_id)
            avatar_url = snapshot.avatar_url
        else:
            snapshot = self.state.by_contact_id(contact_id)
            if snapshot and snapshot.avatar_url:
                avatar_url = snapshot.avatar_url
            else:
                user_id = self._user_contact_map.get(contact_id)
                if user_id:
                    avatar_url = self.user_profiles.get(user_id, {}).get("avatar_url")
        if not avatar_url:
            return None
        resp = await self.client.download(avatar_url)
        if isinstance(resp, DownloadError):
            raise RuntimeError(resp.message)
        mimetype = resp.content_type or "image/jpeg"
        return resp.body, mimetype

    async def set_mute(self, contact_id: str, mute_level: int) -> None:
        now = int(time.time())
        if mute_level == -1:
            self.mutes.set(contact_id, 0)
            return
        offsets = {
            0: 8 * 60 * 60,  # 8 hours
            1: 7 * 24 * 60 * 60,  # 1 week
        }
        if mute_level == 2:
            expiration = now + (10 * 365 * 24 * 60 * 60)
        else:
            expiration = now + offsets.get(mute_level, 0)
        self.mutes.set(contact_id, expiration)

    async def toggle_block(self, contact_id: str) -> bool:
        user_id = self._user_contact_map.get(contact_id)
        if not user_id:
            raise ValueError(f"Unknown user for contact {contact_id}")
        path = f"/_matrix/client/v3/user/{self.config.matrix.user_id}/account_data/m.ignored_user_list"
        get_resp = await self._http.get(path)
        get_resp.raise_for_status()
        payload = get_resp.json() if get_resp.content else {}
        ignored_raw = payload.get("ignored_users", {})
        if isinstance(ignored_raw, dict):
            ignored = set(ignored_raw.keys())
        elif isinstance(ignored_raw, list):
            ignored = set(ignored_raw)
        else:
            ignored = set()
        if user_id in ignored:
            ignored.remove(user_id)
        else:
            ignored.add(user_id)
        new_payload = {"ignored_users": {uid: {} for uid in ignored}}
        put_resp = await self._http.put(path, json=new_payload)
        put_resp.raise_for_status()
        return user_id in ignored

    async def set_status_message(self, status: str) -> None:
        path = f"/_matrix/client/v3/profile/{self.config.matrix.user_id}/status_msg"
        resp = await self._http.put(path, json={"status_msg": status})
        resp.raise_for_status()

    async def audio_as_mp3(self, event_id: str) -> bytes:
        data, _ = await self.download_media(event_id)
        mp3_path = convert_audio_to_mp3(data)
        mp3_bytes = mp3_path.read_bytes()
        mp3_path.unlink(missing_ok=True)
        return mp3_bytes

    async def video_as_quicktime(self, event_id: str, data: Optional[bytes] = None) -> bytes:
        if data is None:
            data, mimetype = await self.download_media(event_id)
        else:
            mimetype = "video/mp4"
        if not mimetype.startswith("video/"):
            return data
        mov_path = convert_video_to_quicktime(data)
        mov_bytes = mov_path.read_bytes()
        mov_path.unlink(missing_ok=True)
        return mov_bytes

    async def video_thumbnail(self, event_id: str, data: Optional[bytes] = None) -> bytes:
        if data is None:
            data, mimetype = await self.download_media(event_id)
        else:
            mimetype = "video/mp4"
        if not mimetype.startswith("video/"):
            raise ValueError("Message is not a video")
        thumb_path = generate_thumbnail_from_video(data)
        thumb_bytes = thumb_path.read_bytes()
        thumb_path.unlink(missing_ok=True)
        return thumb_bytes

    async def create_room(self, name: str, invitees: List[str]) -> str:
        resp = await self.client.room_create(
            visibility="private",
            name=name,
            invite=invitees,
            is_direct=True,
            creation_content={"creator": self.config.matrix.user_id},
        )
        if isinstance(resp, LocalProtocolError):
            raise RuntimeError(str(resp))
        return resp.room_id

    def _snapshot_to_chat(self, snapshot: RoomSnapshot) -> Dict[str, Any]:
        record = self.messages.get_message(snapshot.last_message_id) if snapshot.last_message_id else None
        last_message = record.payload if record else self._empty_message(snapshot)
        jid = jid_from_contact(snapshot.contact_id, snapshot.is_group)
        return {
            "id": {"user": snapshot.contact_id, "server": "g.us" if snapshot.is_group else "c.us"},
            "name": snapshot.name,
            "isGroup": snapshot.is_group,
            "timestamp": snapshot.last_event_ts or 0,
            "muteExpiration": self.mutes.get(snapshot.contact_id),
            "unreadCount": snapshot.unread_count,
            "groupDesc": snapshot.topic or "",
            "lastMessage": last_message,
            "idServer": jid,
        }

    def serialize_room(self, snapshot: RoomSnapshot) -> Dict[str, Any]:
        return self._snapshot_to_chat(snapshot)

    def _snapshot_to_contact(self, snapshot: RoomSnapshot) -> Dict[str, Any]:
        return {
            "id": {"user": snapshot.contact_id, "server": "c.us"},
            "number": snapshot.contact_id,
            "name": snapshot.name,
            "shortName": snapshot.name,
            "pushname": snapshot.name,
            "formattedNumber": snapshot.name,
            "isWAContact": True,
            "isMyContact": True,
            "isMe": False,
            "profileAbout": snapshot.topic or "",
            "commonGroups": [],
        }

    def _participant_to_contact(self, user_id: str) -> Dict[str, Any]:
        contact_id = contact_id_from_user(user_id)
        profile = self.user_profiles.get(user_id, {})
        display_name = profile.get("display_name") or user_id
        self._user_contact_map[contact_id] = user_id
        return {
            "id": {"user": contact_id, "server": "c.us"},
            "number": contact_id,
            "name": display_name,
            "shortName": display_name,
            "pushname": display_name,
            "formattedNumber": display_name,
            "isWAContact": True,
            "isMyContact": True,
            "isMe": user_id == self.config.matrix.user_id,
            "profileAbout": profile.get("status_msg", ""),
            "commonGroups": [],
        }

    def _build_self_contact(self) -> Dict[str, Any]:
        contact_id = contact_id_from_user(self.config.matrix.user_id)
        self._user_contact_map[contact_id] = self.config.matrix.user_id
        return {
            "id": {"user": contact_id, "server": "c.us"},
            "number": contact_id,
            "name": "You",
            "shortName": "You",
            "pushname": "You",
            "formattedNumber": "You",
            "isWAContact": True,
            "isMyContact": True,
            "isMe": True,
            "profileAbout": "",
            "commonGroups": [],
        }

    def _empty_message(self, snapshot: RoomSnapshot) -> Dict[str, Any]:
        jid = jid_from_contact(snapshot.contact_id, snapshot.is_group)
        return {
            "body": "",
            "type": "chat",
            "fromMe": False,
            "timestamp": int(time.time()),
            "ack": 0,
            "id": {
                "_serialized": f"{jid}-placeholder",
                "fromMe": False,
                "remote": jid,
                "id": "placeholder",
                "participant": {"user": snapshot.contact_id},
            },
            "_data": {},
            "hasQuotedMsg": False,
        }

    async def _initial_sync(self) -> None:
        logger.info("Performing initial Matrix sync...")
        try:
            resp = await self.client.sync(30000, full_state=True)
            if isinstance(resp, SyncError):
                raise RuntimeError(f"Matrix sync failed: {resp.message}")
        except Exception as exc:
            logger.error("Initial sync failed: %s", exc, exc_info=True)
        self._ready.set()
        logger.info("Matrix bridge ready")

    async def _sync_forever(self) -> None:
        while True:
            try:
                await self.client.sync_forever(timeout=30000, full_state=False)
            except Exception as exc:
                logger.error("Matrix sync loop crashed: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def _on_sync(self, response: SyncResponse) -> None:
        await self._ready.wait()
        for room_id, room_data in response.rooms.join.items():
            snapshot = self._ensure_snapshot(room_id)
            if room_data.unread_notifications:
                snapshot.unread_count = room_data.unread_notifications.notification_count or 0
            self._process_timeline(snapshot, room_data.timeline.events)
            state_events = getattr(room_data.state, "events", []) or []
            for state_event in state_events:
                self._update_membership(room_id, state_event)
            ephemeral_events = getattr(room_data.ephemeral, "events", []) or []
            for ephemeral in ephemeral_events:
                self._handle_ephemeral(snapshot, ephemeral)
            timeline_events = room_data.timeline.events
            if timeline_events:
                snapshot.last_event_ts = timeline_events[-1].server_timestamp

    def _ensure_snapshot(self, room_id: str) -> RoomSnapshot:
        snapshot = self.state.get(room_id)
        if snapshot:
            return snapshot
        contact_id = contact_id_from_room(room_id)
        room = self.client.rooms.get(room_id)
        name = (room.display_name if room else None) or (room.name if room else None) or room_id
        participants = list(room.users.keys()) if room else []
        if room and hasattr(room, "is_direct"):
            is_group = not room.is_direct
        else:
            is_group = len(participants) > 2
        snapshot = RoomSnapshot(
            room_id=room_id,
            contact_id=contact_id,
            is_group=is_group,
            name=name,
            topic=room.topic if room else "",
            avatar_url=room.avatar_url if room else None,
            participants=participants,
        )
        self.state.upsert(snapshot)
        self._room_contact_map[contact_id] = room_id
        return snapshot

    def _process_timeline(self, snapshot: RoomSnapshot, events: List[Any]) -> None:
        for event in events:
            if event.type == "m.room.message":
                converted = self._convert_message(snapshot, event)
                if not converted:
                    continue
                record = MessageRecord(
                    event_id=converted["id"]["_serialized"],
                    room_id=snapshot.room_id,
                    contact_id=snapshot.contact_id,
                    is_group=snapshot.is_group,
                    payload=converted,
                )
                self.messages.add(record)
                snapshot.last_message_id = record.event_id
                asyncio.create_task(
                    self.notifier.broadcast(
                        {
                            "sender": "wspl-server",
                            "response": "NEW_MESSAGE_NOTI",
                            "body": {
                                "msgBody": converted.get("body"),
                                "from": snapshot.contact_id,
                                "author": converted.get("author", {}).get("user"),
                                "type": converted.get("type"),
                            },
                        }
                    )
                )
                asyncio.create_task(
                    self.notifier.broadcast(
                        {
                            "sender": "wspl-server",
                            "response": "ACK_MESSAGE",
                            "body": {
                                "from": snapshot.contact_id,
                                "msgId": record.event_id,
                                "ack": 4 if converted.get("fromMe") else 2,
                            },
                        }
                    )
                )
            elif event.type == "m.room.redaction":
                asyncio.create_task(
                    self.notifier.broadcast({"sender": "wspl-server", "response": "REVOKE_MESSAGE"})
                )

    def _handle_ephemeral(self, snapshot: RoomSnapshot, event: Any) -> None:
        if event.type == "m.typing":
            typing_users = event.source.get("content", {}).get("user_ids", [])
            status = "composing" if typing_users else "paused"
            asyncio.create_task(
                self.notifier.broadcast(
                    {
                        "sender": "wspl-server",
                        "response": "CONTACT_CHANGE_STATE",
                        "body": {"status": status, "from": snapshot.contact_id},
                    }
                )
            )

    def _update_membership(self, room_id: str, event: Any) -> None:
        if event.type != "m.room.member":
            return
        content = event.source.get("content", {})
        user_id = event.state_key
        display_name = content.get("displayname") or user_id
        avatar = content.get("avatar_url")
        presence_status = content.get("status_msg", "")
        self.user_profiles[user_id] = {
            "display_name": display_name,
            "avatar_url": avatar,
            "status_msg": presence_status,
        }
        contact_id = contact_id_from_user(user_id)
        self._user_contact_map[contact_id] = user_id
        snapshot = self.state.get(room_id)
        if snapshot and user_id not in snapshot.participants:
            snapshot.participants.append(user_id)

    def _convert_message(self, snapshot: RoomSnapshot, event: Any) -> Optional[Dict[str, Any]]:
        content = event.source.get("content", {})
        msgtype = content.get("msgtype", "m.text")
        timestamp = event.server_timestamp or int(time.time() * 1000)
        from_me = event.sender == self.config.matrix.user_id
        contact_jid = jid_from_contact(snapshot.contact_id, snapshot.is_group)
        author_id = contact_id_from_user(event.sender)
        self._user_contact_map[author_id] = event.sender
        base = {
            "type": self._map_msgtype(msgtype, content),
            "body": content.get("body", ""),
            "timestamp": int(timestamp / 1000),
            "fromMe": from_me,
            "ack": 4 if from_me else 2,
            "duration": self._duration_from_content(content),
            "id": {
                "_serialized": event.event_id,
                "fromMe": from_me,
                "remote": contact_jid,
                "id": event.event_id,
                "participant": {"user": author_id},
            },
            "_data": {
                "author": {"user": author_id},
                "quotedMsg": None,
                "quotedParticipant": None,
                "mimetype": content.get("info", {}).get("mimetype"),
                "size": content.get("info", {}).get("size"),
                "width": content.get("info", {}).get("w"),
                "height": content.get("info", {}).get("h"),
                "caption": content.get("body"),
                "lat": self._extract_lat(content),
                "lng": self._extract_lng(content),
                "mxcUrl": content.get("url"),
            },
            "hasQuotedMsg": False,
        }
        relates_to = content.get("m.relates_to", {})
        reply = relates_to.get("m.in_reply_to")
        if reply:
            quoted = self.messages.get_message(reply.get("event_id"))
            if quoted:
                base["_data"]["quotedMsg"] = {
                    "body": quoted.payload.get("body"),
                    "type": quoted.payload.get("type"),
                }
                base["_data"]["quotedParticipant"] = {
                    "user": quoted.payload.get("id", {}).get("participant", {}).get("user")
                }
                base["_data"]["quotedStanzaID"] = quoted.payload.get("id", {}).get("_serialized")
                base["hasQuotedMsg"] = True
        return base

    def _map_msgtype(self, msgtype: str, content: Dict[str, Any]) -> str:
        if msgtype == "m.text":
            return "chat"
        if msgtype == "m.notice":
            return "chat"
        if msgtype == "m.emote":
            return "chat"
        if msgtype == "m.image":
            return "image"
        if msgtype == "m.video":
            return "video"
        if msgtype == "m.file":
            return "document"
        if msgtype == "m.audio":
            if content.get("org.matrix.msc2516.voice"):
                return "ptt"
            return "audio"
        if msgtype == "m.location":
            return "location"
        if msgtype == "m.sticker":
            return "sticker"
        return "chat"

    def _duration_from_content(self, content: Dict[str, Any]) -> int:
        info = content.get("info", {})
        duration_ms = info.get("duration") or content.get("duration")
        if not duration_ms:
            return 0
        return int(duration_ms / 1000)

    def _extract_lat(self, content: Dict[str, Any]) -> Optional[float]:
        geo = content.get("geo_uri")
        if not geo:
            return None
        try:
            lat, _lng = geo.replace("geo:", "").split(",")
            return float(lat)
        except Exception:
            return None

    def _extract_lng(self, content: Dict[str, Any]) -> Optional[float]:
        geo = content.get("geo_uri")
        if not geo:
            return None
        try:
            _, lng = geo.replace("geo:", "").split(",")
            return float(lng)
        except Exception:
            return None
