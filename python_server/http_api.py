from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from .config import AppConfig, load_config
from .matrix_bridge import MatrixBridge
from .socket_server import NotificationSocketServer
from .state import RoomSnapshot
from .utils import b64_to_bytes


logger = logging.getLogger(__name__)

PLACEHOLDER_QR = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


def bool_from_query(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value in {"1", "true", "True", "yes"}


def create_app(root_dir: Optional[Path] = None) -> FastAPI:
    app = FastAPI(title="WhatsAppX Matrix Bridge")
    base_dir = root_dir or Path(__file__).resolve().parents[1]
    config = load_config(base_dir)

    notifier = NotificationSocketServer(
        host=config.server.host,
        port=config.server.socket_port,
        server_token=config.server.tokens.get("SERVER"),
        client_token=config.server.tokens.get("CLIENT"),
    )
    bridge = MatrixBridge(config=config, notifier=notifier)

    app.state.config = config
    app.state.bridge = bridge
    app.state.notifier = notifier

    @app.on_event("startup")
    async def _startup() -> None:
        await notifier.start()
        await bridge.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await bridge.stop()
        await notifier.stop()

    def get_bridge() -> MatrixBridge:
        return app.state.bridge

    @app.get("/", response_class=PlainTextResponse)
    async def index() -> str:
        return "WhatsApp Legacy Matrix bridge"

    @app.get("/loggedInYet", response_class=PlainTextResponse)
    async def logged_in_yet() -> str:
        return "true" if get_bridge().is_ready() else "false"

    @app.get("/qr", response_class=PlainTextResponse)
    async def qr() -> str:
        return "Success" if get_bridge().is_ready() else PLACEHOLDER_QR

    @app.api_route("/getChats", methods=["GET", "POST"])
    async def get_chats() -> Dict[str, Any]:
        data = await get_bridge().get_chats()
        return data

    @app.post("/syncChat/{contact_id}")
    async def sync_chat(contact_id: str) -> Dict[str, Any]:
        bridge = get_bridge()
        snapshot = await bridge.get_room_by_contact(contact_id)
        await bridge.sync_chat(snapshot)
        return {}

    @app.api_route("/getBroadcasts", methods=["GET", "POST"])
    async def get_broadcasts() -> Dict[str, Any]:
        return {"broadcastList": []}

    @app.api_route("/getContacts", methods=["GET", "POST"])
    async def get_contacts() -> Dict[str, Any]:
        return await get_bridge().get_contacts()

    @app.api_route("/getGroups", methods=["GET", "POST"])
    async def get_groups() -> Dict[str, Any]:
        return await get_bridge().get_groups()

    async def serve_profile(contact_id: str, is_group: bool) -> Response:
        media = await get_bridge().get_profile_media(contact_id, is_group=is_group)
        if not media:
            raise HTTPException(status_code=404, detail="Profile image not available")
        data, mimetype = media
        return Response(content=data, media_type=mimetype)

    @app.api_route("/getProfileImg/{contact_id}", methods=["GET", "POST"])
    async def get_profile_img(contact_id: str):
        return await serve_profile(contact_id, is_group=False)

    @app.api_route("/getGroupImg/{contact_id}", methods=["GET", "POST"])
    async def get_group_img(contact_id: str):
        return await serve_profile(contact_id, is_group=True)

    async def hash_profile(contact_id: str, is_group: bool) -> PlainTextResponse:
        try:
            media = await get_bridge().get_profile_media(contact_id, is_group=is_group)
        except Exception:
            media = None
        if not media:
            return PlainTextResponse("null")
        data, _ = media
        digest = hashlib.md5(data).hexdigest()
        return PlainTextResponse(digest)

    @app.api_route("/getProfileImgHash/{contact_id}", methods=["GET", "POST"])
    async def get_profile_hash(contact_id: str):
        return await hash_profile(contact_id, is_group=False)

    @app.api_route("/getGroupImgHash/{contact_id}", methods=["GET", "POST"])
    async def get_group_hash(contact_id: str):
        return await hash_profile(contact_id, is_group=True)

    @app.api_route("/getGroupInfo/{contact_id}", methods=["GET", "POST"])
    async def get_group_info(contact_id: str):
        snapshot = await get_bridge().get_room_by_contact(contact_id)
        if not snapshot.is_group:
            raise HTTPException(status_code=400, detail="Contact is not a group")
        return get_bridge().serialize_room(snapshot)

    @app.api_route("/getChatMessages/{contact_id}", methods=["GET", "POST"])
    async def get_chat_messages(contact_id: str, request: Request):
        bridge = get_bridge()
        snapshot = await bridge.get_room_by_contact(contact_id)
        is_light = bool_from_query(request.query_params.get("isLight"))
        limit = 100 if is_light else 400
        current = bridge.messages.get_room(snapshot.room_id)
        if len(current) < limit:
            await bridge.fetch_messages(snapshot, limit=limit)
        messages = [record.payload for record in bridge.messages.get_room(snapshot.room_id)]
        if limit:
            messages = messages[-limit:]
        return {"chatMessages": messages, "fromNumber": contact_id}

    @app.post("/setTypingStatus/{contact_id}")
    async def set_typing(contact_id: str, request: Request):
        bridge = get_bridge()
        snapshot = await bridge.get_room_by_contact(contact_id)
        is_voice = bool_from_query(request.query_params.get("isVoiceNote"))
        await bridge.set_typing(snapshot, is_voice_note=is_voice)
        return {}

    @app.post("/clearState/{contact_id}")
    async def clear_state(contact_id: str, request: Request):
        bridge = get_bridge()
        snapshot = await bridge.get_room_by_contact(contact_id)
        await bridge.clear_typing(snapshot)
        return {}

    @app.post("/seenBroadcast/{message_id}")
    async def seen_broadcast(message_id: str):
        return {}

    @app.api_route("/getAudioData/{audio_id}", methods=["GET", "POST"])
    async def get_audio(audio_id: str):
        audio_bytes = await get_bridge().audio_as_mp3(audio_id)
        return Response(content=audio_bytes, media_type="audio/mpeg")

    @app.api_route("/getMediaData/{media_id}", methods=["GET", "POST"])
    async def get_media(media_id: str):
        data, mimetype = await get_bridge().download_media(media_id)
        if mimetype.startswith("video/"):
            mov_bytes = await get_bridge().video_as_quicktime(media_id, data=data)
            return Response(content=mov_bytes, media_type="video/quicktime")
        return Response(content=data, media_type=mimetype)

    @app.api_route("/getVideoThumbnail/{media_id}", methods=["GET", "POST"])
    async def get_video_thumbnail(media_id: str):
        data, mimetype = await get_bridge().download_media(media_id)
        if not mimetype.startswith("video/"):
            raise HTTPException(status_code=404, detail="Not a video attachment")
        thumb = await get_bridge().video_thumbnail(media_id, data=data)
        return Response(content=thumb, media_type="image/png")

    @app.post("/sendMessage/{contact_id}")
    async def send_message(contact_id: str, payload: Dict[str, Any]):
        bridge = get_bridge()
        snapshot = await bridge.get_room_by_contact(contact_id)
        reply_to = payload.get("replyTo")
        if payload.get("sendAsVoiceNote") and payload.get("mediaBase64"):
            audio_bytes = b64_to_bytes(payload["mediaBase64"])
            await bridge.send_voice_note(snapshot, audio_bytes, reply_to=reply_to)
        elif payload.get("sendAsPhoto") and payload.get("mediaBase64"):
            image_bytes = b64_to_bytes(payload["mediaBase64"])
            mimetype = payload.get("mimeType", "image/jpeg")
            await bridge.send_image_message(
                snapshot,
                image_bytes=image_bytes,
                mimetype=mimetype,
                caption=payload.get("messageText"),
                reply_to=reply_to,
            )
        elif payload.get("messageText"):
            await bridge.send_text_message(snapshot, payload["messageText"], reply_to=reply_to)
        else:
            raise HTTPException(status_code=400, detail="Unsupported message payload")
        return {"response": "ok"}

    @app.post("/setMute/{contact_id}/{mute_level}")
    async def set_mute(contact_id: str, mute_level: int, request: Request):
        await get_bridge().set_mute(contact_id, mute_level)
        return {"response": "ok"}

    @app.post("/setBlock/{contact_id}")
    async def set_block(contact_id: str):
        blocked = await get_bridge().toggle_block(contact_id)
        return {"response": "ok", "blocked": blocked}

    @app.post("/deleteChat/{contact_id}")
    async def delete_chat(contact_id: str):
        snapshot = await get_bridge().get_room_by_contact(contact_id)
        await get_bridge().delete_chat_history(snapshot)
        return {"response": "ok"}

    @app.post("/readChat/{contact_id}")
    async def read_chat(contact_id: str):
        snapshot = await get_bridge().get_room_by_contact(contact_id)
        await get_bridge().mark_read(snapshot)
        return {"response": "ok"}

    @app.post("/leaveGroup/{group_id}")
    async def leave_group(group_id: str):
        snapshot = await get_bridge().get_room_by_contact(group_id)
        await get_bridge().leave_room(snapshot)
        return {"response": "ok"}

    @app.api_route("/getQuotedMessage/{message_id}", methods=["GET", "POST"])
    async def get_quoted_message(message_id: str):
        record = await get_bridge().get_message_record(message_id)
        payload = record.payload
        quoted = payload.get("_data", {}).get("quotedMsg")
        if not quoted:
            raise HTTPException(status_code=404, detail="Quoted message not available")
        response = {
            "originalMessage": payload.get("body"),
            "quotedMessage": {
                "id": payload.get("_data", {}).get("quotedStanzaID"),
                "body": quoted.get("body"),
                "type": quoted.get("type"),
            },
        }
        return response

    @app.post("/setStatusInfo/{status}")
    async def set_status(status: str):
        await get_bridge().set_status_message(status)
        return {"response": "ok"}

    @app.post("/deleteMessage/{message_id}/{everyone}")
    async def delete_message(message_id: str, everyone: int):
        record = await get_bridge().get_message_record(message_id)
        snapshot = await get_bridge().get_room_by_contact(record.contact_id)
        await get_bridge().delete_message(snapshot, message_id)
        return {"response": "ok"}

    @app.post("/createRoom")
    async def create_room(payload: Dict[str, Any]):
        bridge = get_bridge()
        name = payload.get("name")
        invitees = payload.get("invitees", [])
        if not name:
            raise HTTPException(status_code=400, detail="Room name is required")
        room_id = await bridge.create_room(name, invitees)
        return {"room_id": room_id}

    return app
