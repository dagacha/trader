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

"""Test durable exact-title question previews."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.fresh_watch_abci.preview import (
    PreviewMixin,
    PreviewStore,
    preview_night,
    preview_questions,
)

TITLE = "Will Bitcoin exceed $110,000 by 30 October 2026?"


def test_saved_delivery_preserves_exact_titles() -> None:
    """Keep the exact title from a saved delivery file."""
    fixture = Path(__file__).parent / "fixtures" / "question_preview.json"
    payload = json.loads(fixture.read_text())
    assert preview_questions(payload) == [
        "Will Bruno Fernandes's status as Manchester United's men's first-team "
        "captain still hold on November 2, 2026, according to the most recent "
        "captaincy announcements and player profiles on Manchester United's "
        "official website?"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"result": "bad"},
        {"result": {"questions": [None, {}, {"question": 42}]}},
    ],
)
def test_malformed_delivery(payload: object) -> None:
    """Ignore malformed question data without inventing a title."""
    assert preview_questions(payload) == []


def test_exact_match_and_restart(tmp_path: Path) -> None:
    """An answer attaches only to the exact market title after restart."""
    path = tmp_path / "preview.json"
    store = PreviewStore(path)
    assert store.ingest(
        {"result": {"questions": [{"question": TITLE}]}}, 1000, 1010
    ) == [TITLE]
    assert store.next_title(1010) == TITLE
    entry = store.reserve(TITLE, 1020, "night", 15)
    assert entry is not None
    assert entry["requested_at"] == 1020
    assert PreviewStore(path).reserve(TITLE, 1021, "night", 15) is None
    store.answer(TITLE, 0.91, 1040)
    store = PreviewStore(path)
    assert store.match(TITLE.lower(), 1050) is None
    assert store.match(TITLE + " ", 1050) is None
    matched = store.match(TITLE, 1050)
    assert matched is not None
    assert matched["p_yes"] == 0.91
    assert PreviewStore(path).entries[TITLE]["market_seen_at"] == 1050


def test_pending_answer_can_attach_to_market(tmp_path: Path) -> None:
    """A market can wait for a reserved preview answer."""
    store = PreviewStore(tmp_path / "preview.json")
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1000, "night", 15)
    matched = store.match(TITLE, 1005)
    assert matched is not None
    assert matched["answered_at"] is None
    store.answer(TITLE, 0.93, 1010)
    matched = store.match(TITLE, 1011)
    assert matched is not None
    assert matched["p_yes"] == 0.93


def test_expiry_reports_one_fee_loss(tmp_path: Path) -> None:
    """Drop an orphan after 15 minutes and report its paid request once."""
    path = tmp_path / "preview.json"
    store = PreviewStore(path)
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1010, "night", 15)
    store.answer(TITLE, 0.91, 1040)
    assert store.match(TITLE, 1910) is None
    assert store.expire(1910)[0]["title"] == TITLE
    assert PreviewStore(path).expire(1911) == []


def test_cap_survives_restart_and_resets_each_night(tmp_path: Path) -> None:
    """Keep each night's paid preview count after expiry and restart."""
    path = tmp_path / "preview.json"
    store = PreviewStore(path)
    store.ingest(
        {
            "result": {
                "questions": [{"question": TITLE}, {"question": "Other question?"}]
            }
        },
        1000,
        1000,
    )
    assert store.reserve(TITLE, 1000, "night1", 1) is not None
    store = PreviewStore(path)
    assert store.reserve("Other question?", 1001, "night1", 1) is None
    assert store.reserve("Other question?", 1002, "night2", 1) is not None


def test_still_hold_is_not_sent_for_preview(tmp_path: Path) -> None:
    """Skip still-hold questions and deduplicate exact titles."""
    store = PreviewStore(tmp_path / "preview.json")
    payload = {
        "result": {
            "questions": [
                {"question": "Will it STILL HOLD tomorrow?"},
                {"question": TITLE},
                {"question": TITLE},
            ]
        }
    }
    assert store.ingest(payload, 1000, 1000) == [TITLE]
    assert store.ingest(payload, 1001, 1001) == []


def test_night_window_across_midnight() -> None:
    """Use the window's start date for a cap across midnight."""
    assert preview_night(86400 + 3600, "22:00-05:30") == "1970-01-01"
    assert preview_night(86400 + 23 * 3600, "22:00-05:30") == "1970-01-02"


def run(generator: Any) -> Any:
    """Run a finite cooperative call."""
    while True:
        try:
            next(generator)
        except StopIteration as result:
            return result.value


def test_preview_disabled_has_no_io(tmp_path: Path) -> None:
    """Leave the fast lane unchanged while preview is off."""
    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(lightning_preview=False), store_path=tmp_path
    )
    mixin._chain_rpc = MagicMock(side_effect=AssertionError("unexpected RPC"))
    assert run(mixin.preview_poll(1000)) is None
    mixin._chain_rpc.assert_not_called()
    assert not (tmp_path / "lightning_preview.json").exists()


def test_failed_answer_uses_normal_market_request(tmp_path: Path) -> None:
    """A failed preview must not block the market's usual request."""
    path = tmp_path / "preview.json"
    store = PreviewStore(path)
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1000, "night", 1)
    store.answer_failed(TITLE, 1020)
    assert PreviewStore(path).match(TITLE, 1030) is None
    assert PreviewStore(path).reserve(TITLE, 1030, "night", 1) is None


@pytest.mark.parametrize(
    "tool,expected", [("propose-question", TITLE), ("other-tool", None)]
)
def test_chain_delivery_uses_creator_tool_and_gateway(
    tmp_path: Path, tool: str, expected: object
) -> None:
    """Read matched creator requests and deliveries with cooperative I/O."""
    from eth_abi import encode
    from web3._utils.validation import validate_address

    from packages.valory.skills.fresh_watch_abci.preview import (
        DELIVERY_TOPIC,
        PROPOSER,
        REQUEST_TOPIC,
    )

    creator = "0x89c5cc945dd550bcffb72fe42bff002429f46fec"
    marketplace = "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
    request_id = bytes.fromhex("33" * 32)
    request_digest = bytes.fromhex("11" * 32)
    delivery_digest = bytes.fromhex("22" * 32)
    request_log = {
        "topics": [
            REQUEST_TOPIC,
            "0x" + PROPOSER[2:].zfill(64),
            "0x" + creator[2:].zfill(64),
        ],
        "data": "0x"
        + encode(
            ["uint256", "bytes32[]", "bytes[]"], [1, [request_id], [request_digest]]
        ).hex(),
        "blockNumber": "0xbe",
    }
    delivery_log = {
        "topics": [DELIVERY_TOPIC],
        "data": "0x"
        + encode(
            ["bytes32", "uint256", "bytes"], [request_id, 1, delivery_digest]
        ).hex(),
        "blockNumber": "0xc3",
    }
    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(lightning_preview=True, lightning_creators=[creator]),
        fast=SimpleNamespace(in_window=lambda now: True),
        mech_marketplace_config=SimpleNamespace(mech_marketplace_address=marketplace),
        ipfs_address="https://gateway.autonolas.tech/ipfs/",
        store_path=tmp_path,
    )
    mixin.context = SimpleNamespace(logger=MagicMock())
    calls = []
    urls = []
    metadata = json.loads(
        (
            Path(__file__).parent / "fixtures" / "question_preview_metadata.json"
        ).read_text()
    )

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        calls.append((method, kwargs))
        if method == "get_block":
            yield from ()
            return {"number": 200, "timestamp": 1000}
        validate_address(kwargs["filter_params"]["address"])
        if kwargs["filter_params"]["address"] == marketplace:
            yield from ()
            return [request_log]
        yield from ()
        return [delivery_log]

    def http(**kwargs: Any) -> Generator[None, None, Any]:
        urls.append(kwargs["url"])
        body: Any = (
            {**metadata, "tool": tool}
            if len(urls) == 1
            else {"result": {"questions": [{"question": TITLE}]}}
        )
        yield from ()
        return SimpleNamespace(status_code=200, body=json.dumps(body).encode())

    mixin._chain_rpc = rpc
    mixin.get_http_response = http
    run(mixin.preview_poll(1010))
    store = PreviewStore(tmp_path / "lightning_preview.json")
    assert store.next_title(1010) == expected
    assert store.last_block == 200
    assert calls[1][1]["filter_params"]["topics"] == [
        REQUEST_TOPIC,
        "0x" + PROPOSER[2:].zfill(64),
        ["0x" + creator[2:].zfill(64)],
    ]
    assert (
        urls[0]
        == "https://gateway.autonolas.tech/ipfs/f01701220"
        + "11" * 32
        + "/metadata.json"
    )
    if expected:
        assert urls[
            1
        ] == "https://gateway.autonolas.tech/ipfs/f01701220" + "22" * 32 + "/" + str(
            int.from_bytes(request_id, "big")
        )
        assert store.entries[TITLE]["delivered_at"] == 1000
    calls.clear()
    run(mixin.preview_poll(1011))
    assert len(calls) == 1


def test_seen_market_answer_expires_without_orphan_fee(tmp_path: Path) -> None:
    """A seen market does not keep the cached answer alive after 15 minutes."""
    store = PreviewStore(tmp_path / "preview.json")
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1000, "night", 1)
    store.match(TITLE, 1005)
    store.answer(TITLE, 0.91, 1010)
    assert store.match(TITLE, 1900) is None
    assert store.expire(1900) == []
    assert store.entries == {}


def test_consumed_answer_cannot_replay_on_same_title(tmp_path: Path) -> None:
    """Use each preview answer once, even for markets with the same title."""
    store = PreviewStore(tmp_path / "preview.json")
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1000, "night", 1)
    assert store.consume(TITLE, 1005) is None
    store.answer(TITLE, 0.91, 1010)
    consumed = store.consume(TITLE, 1011)
    assert consumed is not None
    assert consumed["p_yes"] == 0.91
    assert PreviewStore(store.path).match(TITLE, 1012) is None


@pytest.mark.parametrize("block", [None, {}, {"number": "invalid"}])
def test_malformed_preview_rpc_becomes_recoverable_error(
    tmp_path: Path, block: Any
) -> None:
    """Bad provider data must use the caller's normal RPC error handling."""
    from packages.valory.skills.fresh_watch_abci.chain_discovery import ChainRPCError

    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(lightning_preview=True),
        fast=SimpleNamespace(in_window=lambda now: True),
        store_path=tmp_path,
    )
    mixin.context = SimpleNamespace(logger=MagicMock())

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return block

    mixin._chain_rpc = rpc
    with pytest.raises(ChainRPCError):
        run(mixin.preview_poll(1000))


def test_malformed_preview_event_becomes_recoverable_error(tmp_path: Path) -> None:
    """Bad event bytes must not terminate optional preview discovery."""
    from packages.valory.skills.fresh_watch_abci.chain_discovery import ChainRPCError
    from packages.valory.skills.fresh_watch_abci.preview import PROPOSER, REQUEST_TOPIC

    creator = "0x89c5cc945dd550bcffb72fe42bff002429f46fec"
    malformed = {
        "topics": [
            REQUEST_TOPIC,
            "0x" + PROPOSER[2:].zfill(64),
            "0x" + creator[2:].zfill(64),
        ],
        "data": "0x",
    }
    replies = iter([{"number": 100}, [malformed], []])
    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(lightning_preview=True, lightning_creators=[creator]),
        fast=SimpleNamespace(in_window=lambda now: True),
        mech_marketplace_config=SimpleNamespace(
            mech_marketplace_address="0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
        ),
        store_path=tmp_path,
    )
    mixin.context = SimpleNamespace(logger=MagicMock())

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return next(replies)

    mixin._chain_rpc = rpc
    with pytest.raises(ChainRPCError):
        run(mixin.preview_poll(1000))


def test_preview_overnight_gap_keeps_recent_scan_window(tmp_path: Path) -> None:
    """Skip old blocks after the off-window pause to stay within RPC limits."""
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.last_block = 100
    store.save()
    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(
            lightning_preview=True,
            lightning_creators=["0x89c5cc945dd550bcffb72fe42bff002429f46fec"],
        ),
        fast=SimpleNamespace(in_window=lambda now: True),
        mech_marketplace_config=SimpleNamespace(
            mech_marketplace_address="0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
        ),
        store_path=tmp_path,
    )
    mixin.context = SimpleNamespace(logger=MagicMock())
    scans = []

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        if method == "get_block":
            return {"number": 14000}
        scans.append(kwargs["filter_params"])
        return []

    mixin._chain_rpc = rpc
    run(mixin.preview_poll(100000))
    assert len(scans) == 2
    assert all(scan["fromBlock"] == 13820 for scan in scans)
    assert PreviewStore(store.path).last_block == 14000


def test_missing_questions_and_stale_delivery_are_ignored(tmp_path: Path) -> None:
    """Do not queue a delivery without questions or after its time limit."""
    assert preview_questions({"result": {}}) == []
    store = PreviewStore(tmp_path / "preview.json")
    assert (
        store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1900) == []
    )
    assert store.entries == {}
    assert not store.path.exists()


def test_answers_require_a_live_request_and_valid_probability(tmp_path: Path) -> None:
    """Reject unknown, unrequested, expired, and invalid preview answers."""
    store = PreviewStore(tmp_path / "preview.json")
    store.answer("unknown", 0.9, 1000)
    store.answer_failed("unknown", 1000)
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.answer(TITLE, 0.9, 1001)
    store.answer_failed(TITLE, 1001)
    assert store.entries[TITLE]["answered_at"] is None
    assert "failed_at" not in store.entries[TITLE]
    store.reserve(TITLE, 1010, "night", 1)
    store.answer(TITLE, -0.1, 1020)
    store.answer(TITLE, 1.1, 1020)
    store.answer(TITLE, 0.9, 1910)
    assert PreviewStore(store.path).entries[TITLE]["answered_at"] is None
    assert PreviewStore(store.path).entries[TITLE]["p_yes"] is None


@pytest.fixture
def preview_mixin(tmp_path: Path) -> PreviewMixin:
    """Build a preview reader with local state and no external I/O."""
    mixin = PreviewMixin()
    mixin.params = SimpleNamespace(
        lightning=SimpleNamespace(
            lightning_preview=True,
            lightning_creators=["0x89c5cc945dd550bcffb72fe42bff002429f46fec"],
        ),
        fast=SimpleNamespace(in_window=lambda now: True),
        mech_marketplace_config=SimpleNamespace(
            mech_marketplace_address="0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
        ),
        ipfs_address="https://gateway.autonolas.tech/ipfs/",
        store_path=tmp_path,
    )
    mixin.context = SimpleNamespace(logger=MagicMock())
    return mixin


@pytest.mark.parametrize(
    "response",
    [
        None,
        SimpleNamespace(status_code=503, body=b"{}"),
        SimpleNamespace(status_code=200, body=b"invalid"),
        SimpleNamespace(status_code=200, body=None),
    ],
)
def test_gateway_failure_has_no_preview_data(
    preview_mixin: PreviewMixin, response: Any
) -> None:
    """Treat unavailable and invalid gateway responses as retryable misses."""
    calls = []

    def http(**kwargs: Any) -> Generator[None, None, Any]:
        calls.append(kwargs)
        yield from ()
        return response

    preview_mixin.get_http_response = http
    assert run(preview_mixin._preview_json("0x" + "11" * 32)) is None
    assert calls == [
        {
            "method": "GET",
            "url": "https://gateway.autonolas.tech/ipfs/f01701220"
            + "11" * 32
            + "/metadata.json",
        }
    ]


def test_preview_outside_window_has_no_io(preview_mixin: PreviewMixin) -> None:
    """Do not read chain events outside the active window."""
    preview_mixin.params.fast.in_window = lambda now: False
    preview_mixin._chain_rpc = MagicMock(side_effect=AssertionError("unexpected RPC"))
    assert run(preview_mixin.preview_poll(1000)) is None
    preview_mixin._chain_rpc.assert_not_called()
    assert not preview_mixin.preview_store().path.exists()


def test_unrelated_and_removed_events_are_ignored(preview_mixin: PreviewMixin) -> None:
    """Keep tracked requests unchanged when provider events do not qualify."""
    from eth_abi import encode

    from packages.valory.skills.fresh_watch_abci.preview import PROPOSER, REQUEST_TOPIC

    request_id = bytes.fromhex("33" * 32)
    request_key = "0x" + request_id.hex()
    store = preview_mixin.preview_store()
    store.requests[request_key] = {
        "digest": "0x" + "11" * 32,
        "created_at": 1000,
        "tool": "other-tool",
    }
    store.save()
    creator_topic = "0x" + preview_mixin.params.lightning.lightning_creators[0][
        2:
    ].zfill(64)
    topics = [REQUEST_TOPIC, "0x" + PROPOSER[2:].zfill(64), creator_topic]
    request_logs = [
        {"removed": True},
        {"topics": []},
        {"topics": [REQUEST_TOPIC, "0x" + "00" * 32, creator_topic]},
        {"topics": [*topics[:2], "0x" + "00" * 32]},
        {
            "topics": topics,
            "data": encode(
                ["uint256", "bytes32[]", "bytes[]"],
                [1, [bytes.fromhex("44" * 32)], [b"short"]],
            ),
        },
    ]
    delivery_logs = [
        {"removed": True},
        {
            "data": encode(
                ["bytes32", "uint256", "bytes"],
                [bytes.fromhex("55" * 32), 1, bytes.fromhex("22" * 32)],
            )
        },
        {"data": encode(["bytes32", "uint256", "bytes"], [request_id, 1, b"short"])},
    ]
    replies = iter([{"number": 200}, request_logs, delivery_logs])

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return next(replies)

    preview_mixin._chain_rpc = rpc
    preview_mixin.get_http_response = MagicMock(
        side_effect=AssertionError("unexpected HTTP")
    )
    run(preview_mixin.preview_poll(1010))
    latest = preview_mixin.preview_store()
    assert latest.requests == store.requests
    assert latest.last_block == 200
    assert latest.entries == {}
    preview_mixin.get_http_response.assert_not_called()


def test_poll_expires_orphans_and_old_requests(preview_mixin: PreviewMixin) -> None:
    """Report a paid orphan once and drop its old proposal request."""
    store = preview_mixin.preview_store()
    store.ingest({"result": {"questions": [{"question": TITLE}]}}, 1000, 1000)
    store.reserve(TITLE, 1000, "night", 1)
    store.requests["old"] = {"created_at": 1000, "tool": None}
    store.save()
    replies = iter([{"number": 200}, [], [], {"number": 200}])

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return next(replies)

    preview_mixin._chain_rpc = rpc
    run(preview_mixin.preview_poll(1900))
    run(preview_mixin.preview_poll(1901))
    latest = preview_mixin.preview_store()
    assert latest.requests == {}
    assert latest.entries == {}
    preview_mixin.context.logger.info.assert_called_once()
    assert preview_mixin.context.logger.info.call_args.args[-1] == "orphan_fee_loss"


def test_invalid_metadata_retries_before_delivery(preview_mixin: PreviewMixin) -> None:
    """Retry invalid metadata and retain a valid request while delivery is pending."""
    store = preview_mixin.preview_store()
    store.requests["request"] = {
        "digest": "0x" + "11" * 32,
        "created_at": 1000,
        "tool": None,
    }
    store.save()
    replies = iter([{"number": 200}, [], [], {"number": 201}, [], []])
    bodies = iter([[], {"tool": "propose-question"}])
    urls = []

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return next(replies)

    def http(**kwargs: Any) -> Generator[None, None, Any]:
        urls.append(kwargs["url"])
        yield from ()
        return SimpleNamespace(status_code=200, body=json.dumps(next(bodies)).encode())

    preview_mixin._chain_rpc = rpc
    preview_mixin.get_http_response = http
    run(preview_mixin.preview_poll(1010))
    assert preview_mixin.preview_store().requests["request"]["tool"] is None
    run(preview_mixin.preview_poll(1015))
    latest = preview_mixin.preview_store()
    assert latest.requests["request"]["tool"] == "propose-question"
    assert latest.entries == {}
    assert len(urls) == 2
    assert urls[0] == urls[1]


def test_failed_delivery_retries_then_processes_once(
    preview_mixin: PreviewMixin,
) -> None:
    """Keep a failed delivery pending and queue its title once after recovery."""
    request_id = "0x" + "33" * 32
    store = preview_mixin.preview_store()
    store.requests[request_id] = {
        "digest": "0x" + "11" * 32,
        "created_at": 1000,
        "tool": "propose-question",
        "delivery_digest": "0x" + "22" * 32,
        "delivery_block": 195,
    }
    store.save()
    replies = iter(
        [
            {"number": 200},
            [],
            [],
            {"number": 201},
            [],
            [],
            {"timestamp": "0x3e8"},
            {"number": 202},
            [],
            [],
        ]
    )
    responses = iter(
        [
            SimpleNamespace(status_code=503),
            SimpleNamespace(
                status_code=200,
                body=json.dumps({"result": {"questions": [{"question": TITLE}]}}),
            ),
        ]
    )
    urls = []

    def rpc(method: str, **kwargs: Any) -> Generator[None, None, Any]:
        yield from ()
        return next(replies)

    def http(**kwargs: Any) -> Generator[None, None, Any]:
        urls.append(kwargs["url"])
        yield from ()
        return next(responses)

    preview_mixin._chain_rpc = rpc
    preview_mixin.get_http_response = http
    run(preview_mixin.preview_poll(1010))
    assert not preview_mixin.preview_store().requests[request_id].get("processed")
    assert preview_mixin.preview_store().entries == {}
    run(preview_mixin.preview_poll(1015))
    run(preview_mixin.preview_poll(1020))
    latest = preview_mixin.preview_store()
    assert latest.requests[request_id]["processed"] is True
    assert latest.next_title(1020) == TITLE
    assert len(urls) == 2
    assert urls[0] == urls[1]
    preview_mixin.context.logger.info.assert_called_once()
