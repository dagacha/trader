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

"""Tests for the lane's stdlib websocket source."""

import asyncio
import base64
import hashlib
import json
import struct
import threading
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci import mempool_ws
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.mempool import CREATOR_PAIRS

# pylint: disable=protected-access

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> bytes:
    """Accept a websocket handshake from a local client."""
    request = await reader.readuntil(b"\r\n\r\n")
    headers = dict(
        line.split(b": ", 1) for line in request.split(b"\r\n")[1:] if b": " in line
    )
    accept = base64.b64encode(
        hashlib.sha1(
            headers[b"Sec-WebSocket-Key"] + GUID.encode(), usedforsecurity=False
        ).digest()
    )
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    await writer.drain()
    return request


async def client_frame(reader: asyncio.StreamReader) -> Tuple[int, bytes]:
    """Read and unmask one client frame."""
    header = await reader.readexactly(2)
    assert header[1] & 128
    size = header[1] & 127
    if size == 126:
        size = struct.unpack(">H", await reader.readexactly(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4)
    body = await reader.readexactly(size)
    return header[0] & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(body))


def frame(payload: bytes, opcode: int = 1, fin: bool = True) -> bytes:
    """Encode one server frame."""
    size = len(payload)
    length = (
        bytes([size])
        if size < 126
        else (
            b"\x7e" + struct.pack(">H", size)
            if size < 65536
            else b"\x7f" + struct.pack(">Q", size)
        )
    )
    return bytes([(128 if fin else 0) | opcode]) + length + payload


@pytest.mark.asyncio
async def test_handshake_and_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local server accepts the handshake and all client length forms."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    received: List[Any] = []

    async def server(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received.append(await handshake(reader, writer))
        for _ in range(3):
            received.append(await client_frame(reader))
        writer.close()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    real_connect = asyncio.open_connection

    async def local_connect(
        *_args: Any, **_kwargs: Any
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await real_connect("127.0.0.1", port)

    with patch.object(mempool_ws.asyncio, "open_connection", local_connect):
        ws = mempool_ws.WebSocket("wss://example.test/secret?key=secret")
        await ws.connect()
        for size in (5, 126, 65536):
            await ws.send("x" * size)
        await asyncio.sleep(0.01)
        await ws.close()
    listener.close()
    await listener.wait_closed()
    assert b"GET /secret?key=secret HTTP/1.1" in received[0]
    assert received[1:] == [(1, b"x" * n) for n in (5, 126, 65536)]


@pytest.mark.asyncio
async def test_fragmented_frames_and_ping() -> None:
    """A ping between text fragments gets a masked pong."""
    ws = mempool_ws.WebSocket("wss://example.test")
    ws.reader = asyncio.StreamReader()
    ws.writer = MagicMock(drain=AsyncMock())
    ws.reader.feed_data(
        frame(b"first", fin=False)
        + frame(b"ping", opcode=9)
        + frame(b"ignored", opcode=10)
        + frame(b"second", opcode=0)
    )
    assert await ws.recv() == "firstsecond"
    output = asyncio.StreamReader()
    output.feed_data(ws.writer.write.call_args[0][0])
    assert await client_frame(output) == (10, b"ping")


@pytest.mark.asyncio
async def test_proxy_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy path uses CONNECT before TLS, including on Python 3.10."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    reader = MagicMock()
    reader.readuntil = AsyncMock(side_effect=[b"HTTP/1.1 200 OK\r\n\r\n", b"bad"])
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    loop = MagicMock(start_tls=AsyncMock(return_value="tls-transport"))
    with (
        patch.object(
            mempool_ws.asyncio,
            "open_connection",
            AsyncMock(return_value=(reader, writer)),
        ) as connect,
        patch.object(mempool_ws.asyncio, "get_running_loop", return_value=loop),
    ):
        ws = mempool_ws.WebSocket("wss://example.test:9443/private")
        with pytest.raises(ConnectionError):
            await ws.connect()
    connect.assert_awaited_once_with("proxy.test", 8080)
    assert (
        writer.write.call_args_list[0]
        .args[0]
        .startswith(b"CONNECT example.test:9443 HTTP/1.1\r\n")
    )
    assert writer._transport == "tls-transport"
    loop.start_tls.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_and_resubscribe() -> None:
    """A dropped connection gets both subscriptions again after backoff."""
    source = mempool_ws.MempoolSource(
        "wss://example.test/private", LightningConfig(), "0xmultisend", MagicMock()
    )
    sockets = [MagicMock(), MagicMock()]
    for ws in sockets:
        ws.connect = AsyncMock()
        ws.send = AsyncMock()
        ws.close = AsyncMock()
    sockets[0].recv = AsyncMock(side_effect=ConnectionError("private URL"))

    async def stop_receiving() -> str:
        source.stop()
        return json.dumps({"id": 1, "result": "pending"})

    sockets[1].recv = AsyncMock(side_effect=stop_receiving)
    with (
        patch.object(mempool_ws, "WebSocket", side_effect=sockets),
        patch.object(mempool_ws.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        await source._listen()
    sleep.assert_awaited_once_with(3)
    for ws in sockets:
        assert [
            json.loads(call.args[0])["params"] for call in ws.send.call_args_list
        ] == [
            ["newPendingTransactions", True],
            ["newHeads"],
        ]
        ws.close.assert_awaited_once()
    assert "private URL" not in str(source.logger.mock_calls)


def test_queue_filters_and_head_priority() -> None:
    """Only known pairs reach decoding; heads keep their local arrival time."""
    source = mempool_ws.MempoolSource(
        "wss://example.test", LightningConfig(), "0xmultisend", MagicMock(), 1
    )
    sender, target = next(iter(CREATOR_PAIRS.items()))
    transaction = {"from": sender, "to": target, "input": "0x6a761202"}

    def notification(result: Any, subscription: str = "pending") -> Dict[str, Any]:
        return {"params": {"subscription": subscription, "result": result}}

    subscriptions = {"pending": 1, "heads": 2}
    with patch.object(mempool_ws, "decode_pending", return_value="candidate") as decode:
        message: Any
        for message in (
            [],
            {},
            notification("0xhash"),
            notification(transaction, "unknown"),
            notification(dict(transaction, **{"from": "0xunknown"})),
            notification(dict(transaction, to="0xunknown")),
            notification(dict(transaction, input="0xother")),
            notification(dict(transaction, input=None)),
        ):
            source._message(message, 10, subscriptions)
        decode.assert_not_called()
        source._message(notification(transaction), 11, subscriptions)
        source._message(notification(transaction), 12, subscriptions)
        source._message(
            notification({"number": "0x1", "timestamp": "0xa"}, "heads"),
            13,
            subscriptions,
        )
        source._message(notification({}, "heads"), 14, subscriptions)
        assert source.take_head() == {
            "number": "0x1",
            "timestamp": "0xa",
            "_seen_at": 13,
        }
        assert source.take_head() is None
        assert source.drain() == ["candidate"]
        assert not source.drain()
        decode.return_value = None
        source._message(notification(transaction), 15, subscriptions)
        assert not source.drain()


def test_thread_lifecycle() -> None:
    """Stop cancels the thread's loop without joining from the lane."""
    source = mempool_ws.MempoolSource(
        "wss://example.test", LightningConfig(), "0xmultisend", MagicMock()
    )
    ready = threading.Event()

    async def wait_until_cancelled() -> None:
        ready.set()
        await asyncio.Future()

    with patch.object(source, "_listen", wait_until_cancelled):
        source.start()
        assert ready.wait(2)
        thread = source._thread
        assert thread is not None
        assert thread.daemon
        source.start()
        assert source._thread is thread
        source.stop()
        thread.join(2)
        assert not thread.is_alive()
        assert source._loop is None
        assert source._task is None
    source.stop()
    source._loop, source._task = MagicMock(), MagicMock()
    source._loop.call_soon_threadsafe.side_effect = RuntimeError("closed")
    source.stop()
    empty = mempool_ws.MempoolSource("", LightningConfig(), "", MagicMock())
    empty.start()
    assert empty._thread is None


@pytest.mark.parametrize("url", ["http://example.test", "wss://"])
def test_requires_tls_endpoint(url: str) -> None:
    """Plain or incomplete URLs cannot open a websocket."""
    with pytest.raises(ValueError, match="wss endpoint"):
        mempool_ws.WebSocket(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [126, 65536])
async def test_large_server_frame(size: int) -> None:
    """Read both extended server frame lengths."""
    ws = mempool_ws.WebSocket("wss://example.test")
    ws.reader = asyncio.StreamReader()
    ws.reader.feed_data(frame(b"x" * size))
    assert await ws.recv() == "x" * size
    await ws.close()
    with pytest.raises(ConnectionError):
        await ws.send("closed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        b"\xc1\x00",  # Reserved bits.
        b"\x81\x80",  # A server must not mask its frames.
        b"\x81\x7f" + struct.pack(">Q", mempool_ws.MAX_MESSAGE + 1),
        frame(b"", opcode=9, fin=False),
        frame(b"x" * 126, opcode=9),
        frame(b"", opcode=8),
        frame(b"", opcode=2),
        frame(b"", opcode=0),
        frame(b"a", fin=False) + frame(b"b"),
    ],
)
async def test_rejects_invalid_server_frames(wire: bytes) -> None:
    """Reject invalid frame headers, control frames, and fragments."""
    ws = mempool_ws.WebSocket("wss://example.test")
    ws.reader = asyncio.StreamReader()
    ws.reader.feed_data(wire)
    with pytest.raises(ConnectionError):
        await ws.recv()


@pytest.mark.asyncio
async def test_fragmented_message_size_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound the whole message, including multiple small fragments."""
    monkeypatch.setattr(mempool_ws, "MAX_MESSAGE", 3)
    ws = mempool_ws.WebSocket("wss://example.test")
    ws.reader = asyncio.StreamReader()
    ws.reader.feed_data(frame(b"ab", fin=False) + frame(b"cd", opcode=0))
    with pytest.raises(ConnectionError):
        await ws.recv()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proxy", "response"),
    [
        ("socks://proxy.test", b""),
        ("http://proxy.test", b"HTTP/1.1 403 Denied\r\n\r\n"),
    ],
)
async def test_proxy_failure_is_private(
    monkeypatch: pytest.MonkeyPatch, proxy: str, response: bytes
) -> None:
    """Proxy failure text is not included in the raised error."""
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("SSL_CERT_FILE", "/private/cert.pem")
    reader = MagicMock(readuntil=AsyncMock(return_value=response))
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())
    context = MagicMock()
    with (
        patch.object(mempool_ws.ssl, "create_default_context", return_value=context),
        patch.object(
            mempool_ws.asyncio,
            "open_connection",
            AsyncMock(return_value=(reader, writer)),
        ),
    ):
        ws = mempool_ws.WebSocket("wss://example.test/private")
        with pytest.raises(ConnectionError) as error:
            await ws.connect()
        await ws.close()
    context.load_verify_locations.assert_called_once_with("/private/cert.pem")
    assert "private" not in str(error.value)
    assert "Denied" not in str(error.value)


@pytest.mark.asyncio
async def test_subscriptions_ignore_bad_messages_and_keep_heads() -> None:
    """Malformed messages do not end the stream or hide a valid head."""
    source = mempool_ws.MempoolSource(
        "wss://example.test", LightningConfig(), "", MagicMock()
    )
    messages = iter(
        [
            "bad json",
            json.dumps({"id": 1, "result": "pending"}),
            json.dumps({"id": 2, "result": "heads"}),
            json.dumps({"params": {"subscription": [], "result": {}}}),
            json.dumps(
                {
                    "params": {
                        "subscription": "heads",
                        "result": {"number": "0x1", "timestamp": "0xa"},
                    }
                }
            ),
        ]
    )

    async def receive() -> str:
        try:
            return next(messages)
        except StopIteration:
            source.stop()
            return "null"

    ws = MagicMock(
        connect=AsyncMock(),
        send=AsyncMock(),
        close=AsyncMock(),
        recv=AsyncMock(side_effect=receive),
    )
    with patch.object(mempool_ws, "WebSocket", return_value=ws):
        await source._listen()
    head = source.take_head()
    assert head is not None
    assert head["number"] == "0x1"
    assert any("resubscribed" in str(call) for call in source.logger.mock_calls)


@pytest.mark.asyncio
async def test_backoff_caps_and_subscription_errors() -> None:
    """Repeated errors use 3, 6, 12, 24, 48, 60, 60 seconds."""
    source = mempool_ws.MempoolSource(
        "wss://example.test/secret", LightningConfig(), "", MagicMock()
    )
    ws = MagicMock(
        connect=AsyncMock(),
        send=AsyncMock(),
        close=AsyncMock(),
        recv=AsyncMock(return_value=json.dumps({"id": 1, "error": "secret URL"})),
    )
    delays: List[int] = []

    async def sleep(delay: int) -> None:
        delays.append(delay)
        if len(delays) == 7:
            source.stop()

    with (
        patch.object(mempool_ws, "WebSocket", return_value=ws),
        patch.object(mempool_ws.asyncio, "sleep", sleep),
    ):
        await source._listen()
    assert delays == [3, 6, 12, 24, 48, 60, 60]
    assert "secret" not in str(source.logger.mock_calls)
    assert "ConnectionError" in str(source.logger.mock_calls)


@pytest.mark.asyncio
async def test_cancelled_stream_closes_socket() -> None:
    """Thread cancellation reaches an active socket immediately."""
    source = mempool_ws.MempoolSource(
        "wss://example.test", LightningConfig(), "", MagicMock()
    )
    ws = MagicMock(
        connect=AsyncMock(side_effect=asyncio.CancelledError()), close=AsyncMock()
    )
    with patch.object(mempool_ws, "WebSocket", return_value=ws):
        with pytest.raises(asyncio.CancelledError):
            await source._listen()
    ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_endpoint_does_not_kill_source() -> None:
    """An invalid endpoint logs its error class and can stop during backoff."""
    source = mempool_ws.MempoolSource(
        "https://example.test/secret", LightningConfig(), "", MagicMock()
    )

    async def stop_backoff(_delay: int) -> None:
        source.stop()

    with patch.object(mempool_ws.asyncio, "sleep", stop_backoff):
        await source._listen()
    assert "ValueError" in str(source.logger.mock_calls)
    assert "secret" not in str(source.logger.mock_calls)


@pytest.mark.asyncio
async def test_mock_server_reconnects_and_restores_both_subscriptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real local stream disconnects, reconnects, and delivers its next head."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    source = mempool_ws.MempoolSource(
        "wss://example.test/secret", LightningConfig(), "", MagicMock()
    )
    sessions: List[List[Dict[str, Any]]] = []

    async def server(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await handshake(reader, writer)
        calls = [json.loads((await client_frame(reader))[1]) for _ in range(2)]
        sessions.append(calls)
        writer.write(frame(json.dumps({"id": 1, "result": "pending"}).encode()))
        writer.write(frame(json.dumps({"id": 2, "result": "heads"}).encode()))
        if len(sessions) == 1:
            writer.write(frame(b"", opcode=8))
        else:
            writer.write(
                frame(
                    json.dumps(
                        {
                            "params": {
                                "subscription": "heads",
                                "result": {"number": "0x2", "timestamp": "0xb"},
                            }
                        }
                    ).encode()
                )
            )
            await writer.drain()
            await asyncio.sleep(0.01)
            source.stop()
        writer.close()

    listener = await asyncio.start_server(server, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    real_connect, real_sleep = asyncio.open_connection, asyncio.sleep

    async def local_connect(
        *_args: Any, **_kwargs: Any
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await real_connect("127.0.0.1", port)

    async def quick_backoff(delay: float) -> None:
        await real_sleep(0 if delay >= 3 else delay)

    with (
        patch.object(mempool_ws.asyncio, "open_connection", local_connect),
        patch.object(mempool_ws.asyncio, "sleep", quick_backoff),
    ):
        await asyncio.wait_for(source._listen(), timeout=2)
    listener.close()
    await listener.wait_closed()
    assert len(sessions) == 2
    for calls in sessions:
        assert [call["params"] for call in calls] == [
            ["newPendingTransactions", True],
            ["newHeads"],
        ]
    head = source.take_head()
    assert head is not None
    assert head["number"] == "0x2"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, TimeoutError(), ConnectionResetError()])
async def test_close_finishes_or_aborts_tls(error: Any) -> None:
    """A slow or broken TLS shutdown cannot leave its socket behind."""
    ws = mempool_ws.WebSocket("wss://example.test")
    writer = MagicMock(wait_closed=AsyncMock(side_effect=error))
    ws.writer = writer
    await ws.close()
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()
    assert writer.transport.abort.call_count == int(error is not None)
