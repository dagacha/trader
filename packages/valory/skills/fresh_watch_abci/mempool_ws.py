# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Read pending market creations without blocking the behaviour thread."""

import asyncio
import base64
import hashlib
import json
import os
import ssl
import struct
import threading
import time
from queue import Empty, Full, Queue
from typing import Any, Dict, List, Optional, cast
from urllib.parse import urlsplit

from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.mempool import (
    PendingCreation,
    creator_transaction,
    decode_pending,
)

MAX_MESSAGE = 4 * 1024 * 1024
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    """A small RFC 6455 client for JSON text messages over TLS."""

    def __init__(self, url: str) -> None:
        """Keep the endpoint private.

        :param url: websocket endpoint.
        :raises ValueError: if the endpoint is not a wss URL.
        """
        parsed = urlsplit(url)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise ValueError("A wss endpoint is required")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.reader: asyncio.StreamReader
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        """Open TLS, then check the websocket upgrade.

        :raises ConnectionError: if the proxy or websocket upgrade fails.
        """
        context = ssl.create_default_context()
        if os.environ.get("SSL_CERT_FILE"):
            context.load_verify_locations(os.environ["SSL_CERT_FILE"])
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            parsed = urlsplit(proxy)
            if parsed.scheme != "http" or not parsed.hostname:
                raise ConnectionError("Unsupported proxy")
            self.reader, self.writer = await asyncio.open_connection(
                parsed.hostname, parsed.port or 80
            )
            self.writer.write(
                (
                    f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                    f"Host: {self.host}:{self.port}\r\n\r\n"
                ).encode()
            )
            await self.writer.drain()
            header = await self.reader.readuntil(b"\r\n\r\n")
            if header.split(b"\r\n", 1)[0].split()[1:2] != [b"200"]:
                raise ConnectionError("Proxy CONNECT failed")
            loop = asyncio.get_running_loop()
            # Python 3.10 has no StreamWriter.start_tls method.
            writer = cast(Any, self.writer)
            transport = await loop.start_tls(
                writer.transport,
                writer._protocol,  # pylint: disable=protected-access
                context,
                server_side=False,
                server_hostname=self.host,
            )
            writer._transport = transport  # pylint: disable=protected-access
        else:
            self.reader, self.writer = await asyncio.open_connection(
                self.host, self.port, ssl=context, server_hostname=self.host
            )
        key = base64.b64encode(os.urandom(16)).decode()
        self.writer.write(
            (
                f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await self.writer.drain()
        header = await self.reader.readuntil(b"\r\n\r\n")
        lines = header.decode("ascii").split("\r\n")
        fields = {
            name.strip().lower(): value.strip()
            for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
        }
        expected = base64.b64encode(
            hashlib.sha1(
                (key + WEBSOCKET_GUID).encode(), usedforsecurity=False
            ).digest()
        ).decode()
        if (
            lines[0].split()[1:2] != ["101"]
            or fields.get("sec-websocket-accept") != expected
            or fields.get("upgrade", "").lower() != "websocket"
            or "upgrade"
            not in [
                part.strip() for part in fields.get("connection", "").lower().split(",")
            ]
        ):
            raise ConnectionError("Websocket upgrade failed")

    async def _send_frame(self, payload: bytes, opcode: int) -> None:
        """Send a masked frame.

        :param payload: frame body.
        :param opcode: RFC 6455 opcode.
        :raises ConnectionError: if the connection is closed.
        """
        if self.writer is None:
            raise ConnectionError("Websocket is closed")
        size = len(payload)
        length = (
            bytes([128 | size])
            if size < 126
            else (
                b"\xfe" + struct.pack(">H", size)
                if size < 65536
                else b"\xff" + struct.pack(">Q", size)
            )
        )
        mask = os.urandom(4)
        self.writer.write(
            bytes([128 | opcode])
            + length
            + mask
            + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        )
        await self.writer.drain()

    async def send(self, text: str) -> None:
        """Send one JSON text message.

        :param text: encoded message.
        """
        await self._send_frame(text.encode(), 1)

    async def recv(self) -> str:
        """Read text fragments and answer pings.

        :return: the complete text message.
        :raises ConnectionError: if the peer closes or sends an invalid frame.
        """
        chunks = bytearray()
        fragmented = False
        while True:
            header = await self.reader.readexactly(2)
            final, opcode, size = bool(header[0] & 128), header[0] & 15, header[1] & 127
            if header[0] & 112 or header[1] & 128:
                raise ConnectionError("Invalid server frame")
            if size == 126:
                size = struct.unpack(">H", await self.reader.readexactly(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", await self.reader.readexactly(8))[0]
            if size > MAX_MESSAGE or (opcode >= 8 and (not final or size > 125)):
                raise ConnectionError("Invalid frame size")
            payload = await self.reader.readexactly(size)
            if opcode == 8:
                raise ConnectionError("Websocket closed by peer")
            if opcode == 9:
                await self._send_frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != fragmented:
                raise ConnectionError("Invalid text fragment")
            chunks.extend(payload)
            if len(chunks) > MAX_MESSAGE:
                raise ConnectionError("Message is too large")
            if final:
                return chunks.decode()
            fragmented = True

    async def close(self) -> None:
        """Close TLS with a bounded wait in the source thread."""
        if self.writer is not None:
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=1)
            except (asyncio.TimeoutError, OSError):
                self.writer.transport.abort()


class MempoolSource:
    """Keep a bounded candidate queue and a separate latest-head slot."""

    def __init__(
        self,
        url: str,
        cfg: LightningConfig,
        multisend: str,
        logger: Any,
        queue_size: int = 64,
    ) -> None:
        """Prepare a source without opening a socket or starting a thread.

        :param url: private websocket endpoint.
        :param cfg: lightning configuration.
        :param multisend: configured MultiSend address.
        :param logger: skill logger.
        :param queue_size: maximum number of waiting candidates.
        """
        self._url = url
        self.host = urlsplit(url).hostname or ""
        self.cfg, self.multisend, self.logger = cfg, multisend, logger
        self._queue: Queue[PendingCreation] = Queue(maxsize=max(1, queue_size))
        self._head: Optional[Dict[str, Any]] = None
        self._head_lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start one daemon thread; the caller controls the active window."""
        if not self._url or (self._thread is not None and self._thread.is_alive()):
            return
        self.drain()
        self.take_head()
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run, name="lightning-mempool", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Cancel I/O without making the lane wait for the thread."""
        self._stopped.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The thread has already closed its event loop.

    def drain(self) -> List[PendingCreation]:
        """Take the current bounded batch without waiting on the thread.

        :return: decoded candidates.
        """
        batch = []
        for _ in range(self._queue.maxsize):
            try:
                batch.append(self._queue.get_nowait())
            except Empty:
                break
        return batch

    def take_head(self) -> Optional[Dict[str, Any]]:
        """Take the latest head even when the candidate queue is full.

        :return: header, or None when no new header has arrived.
        """
        with self._head_lock:
            head, self._head = self._head, None
        return head

    def _run(self) -> None:
        """Own the event loop for the lifetime of the daemon thread."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._task = loop.create_task(self._listen())
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._task, self._loop = None, None
            loop.close()

    def _message(
        self, message: Any, seen_at: float, subscriptions: Dict[str, int]
    ) -> None:
        """Filter high-rate pending bodies before the full decoder runs.

        :param message: parsed JSON message.
        :param seen_at: first local receipt time.
        :param subscriptions: subscription ids for this connection.
        """
        if not isinstance(message, dict):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        result = params.get("result")
        subscription = params.get("subscription")
        kind = (
            subscriptions.get(subscription) if isinstance(subscription, str) else None
        )
        if not isinstance(result, dict):
            return
        if kind == 2:
            if "timestamp" in result and "number" in result:
                with self._head_lock:
                    self._head = dict(result, _seen_at=seen_at)
            return
        if kind != 1 or not creator_transaction(result):
            return
        candidate = decode_pending(result, self.cfg, self.multisend, seen_at)
        if candidate is not None:
            try:
                self._queue.put_nowait(candidate)
            except Full:
                pass

    async def _listen(self) -> None:
        """Reconnect with bounded backoff and restore both subscriptions."""
        backoff = 3
        while not self._stopped.is_set():
            ws: Optional[WebSocket] = None
            subscriptions: Dict[str, int] = {}
            try:
                ws = WebSocket(self._url)
                await asyncio.wait_for(ws.connect(), timeout=20)
                self.logger.info(
                    "[LIGHTNING_MEMPOOL_WS] connected host=%s subs=0", self.host
                )
                for request_id, params in (
                    (1, ["newPendingTransactions", True]),
                    (2, ["newHeads"]),
                ):
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "eth_subscribe",
                                "params": params,
                            }
                        )
                    )
                while not self._stopped.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    seen_at = time.time()
                    try:
                        message = json.loads(raw)
                        if isinstance(message, dict) and message.get("id") in (1, 2):
                            subscription = message.get("result")
                            if not isinstance(subscription, str):
                                raise ConnectionError("Subscription failed")
                            subscriptions[subscription] = message["id"]
                            if len(subscriptions) == 2:
                                backoff = 3
                                self.logger.info(
                                    "[LIGHTNING_MEMPOOL_WS] resubscribed host=%s subs=2",
                                    self.host,
                                )
                        else:
                            self._message(message, seen_at, subscriptions)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.warning(
                    "[LIGHTNING_MEMPOOL_WS] disconnected host=%s subs=%s error=%s",
                    self.host,
                    len(subscriptions),
                    type(exc).__name__,
                )
            finally:
                if ws is not None:
                    await ws.close()
            if not self._stopped.is_set():
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
