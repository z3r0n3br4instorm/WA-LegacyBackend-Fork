from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple


logger = logging.getLogger(__name__)


@dataclass(eq=False)
class SocketClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    name: str
    authenticated: bool = False


class NotificationSocketServer:
    def __init__(self, host: str, port: int, server_token: str, client_token: str):
        self.host = host
        self.port = port
        self.server_token = server_token
        self.client_token = client_token
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: Set[SocketClient] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        logger.info("Notification socket listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        async with self._lock:
            for client in list(self._clients):
                client.writer.close()
                await client.writer.wait_closed()
            self._clients.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def broadcast(self, payload: Dict) -> None:
        message = json.dumps(payload)
        async with self._lock:
            for client in list(self._clients):
                if not client.authenticated:
                    continue
                try:
                    client.writer.write(message.encode("utf-8"))
                    await client.writer.drain()
                except Exception as exc:  # pragma: no cover - network errors
                    logger.warning("Failed to notify %s: %s", client.name, exc)
                    await self._drop_client(client)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer: Tuple[str, int] = writer.get_extra_info("peername")
        client = SocketClient(reader=reader, writer=writer, name=f"{peer[0]}:{peer[1]}")
        logger.info("%s connected", client.name)

        async with self._lock:
            self._clients.add(client)

        await self._send_json(client, {"sender": "wspl-server", "token": self.server_token})

        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await self._handle_payload(client, data)
        except asyncio.IncompleteReadError:
            pass
        finally:
            await self._drop_client(client)
            logger.info("%s disconnected", client.name)

    async def _handle_payload(self, client: SocketClient, data: bytes) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from %s", client.name)
            await self._drop_client(client)
            return

        sender = payload.get("sender")
        token = payload.get("token")
        if not client.authenticated:
            if sender == "wspl-client" and token == self.client_token:
                client.authenticated = True
                await self._send_json(client, {"sender": "wspl-server", "response": "ok"})
            else:
                await self._send_json(client, {"sender": "wspl-server", "response": "reject"})
                await self._drop_client(client)
            return

    async def _send_json(self, client: SocketClient, payload: Dict) -> None:
        client.writer.write(json.dumps(payload).encode("utf-8"))
        await client.writer.drain()

    async def _drop_client(self, client: SocketClient) -> None:
        async with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        try:
            client.writer.close()
            await client.writer.wait_closed()
        except Exception:
            pass
