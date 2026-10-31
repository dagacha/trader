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

"""Share the RPC outage pause across lane and foreground recovery reads."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    MODULE,
    lane,
    reply_on_send,
    run,
    settled,
)
from packages.valory.skills.trader_abci.tests.test_lightning_lane_guard import (
    make_guard,
)


def pending_lane(tmp_path: Any) -> Any:
    """Send five failed receipt reads through the real lane callbacks."""
    behaviour = lane(tmp_path)
    info = settled(behaviour, tmp_path)
    behaviour.context.shared_state["lightning_lane_inflight"] = info
    behaviour.context.shared_state["lightning_lane_eoa_nonce"] = {"nonce": 7}
    dialogue = reply_on_send(
        behaviour, SimpleNamespace(status_code=503, body=b'{"result":null}')
    )
    behaviour._build_http_request_message = MagicMock(
        return_value=(MagicMock(), dialogue)
    )
    for now in range(1000, 1005):
        with patch(MODULE + ".time.time", return_value=now):
            run(behaviour.async_act())
        assert behaviour.context.shared_state["lightning_lane_inflight"] is info
    assert behaviour._stats.paused_until == 1064
    assert (behaviour._stats.calls, behaviour._stats.errors) == (5, 5)
    return behaviour


def test_pending_lane_waits_for_rpc_pause_before_recovery(tmp_path: Any) -> None:
    """Hold the lock without RPC until expiry, then book the mined receipt."""
    behaviour = pending_lane(tmp_path)
    shared = behaviour.context.shared_state
    info = shared["lightning_lane_inflight"]
    for now in (1004.5, 1005.0, 1063.999):
        with patch(MODULE + ".time.time", return_value=now):
            run(behaviour.async_act())
        assert behaviour._build_http_request_message.call_count == 5
        assert behaviour.context.outbox.put_message.call_count == 5
        assert shared["lightning_lane_inflight"] is info
        assert shared["lightning_lane_eoa_nonce"] == {"nonce": 7}
        assert (behaviour._stats.calls, behaviour._stats.errors) == (5, 5)
        assert behaviour._stats.paused_until == 1064

    responses = iter(
        [
            {"status": "0x1", "blockNumber": "0x2", "logs": []},
            {"number": "0x2", "timestamp": hex(1064)},
        ]
    )
    reply_on_send(
        behaviour,
        lambda: SimpleNamespace(
            status_code=200, body=json.dumps({"result": next(responses)}).encode()
        ),
    )
    with patch(MODULE + ".time.time", return_value=1064):
        run(behaviour.async_act())
    assert behaviour._build_http_request_message.call_count == 7
    assert behaviour.context.outbox.put_message.call_count == 7
    assert "lightning_lane_inflight" not in shared
    assert "lightning_lane_eoa_nonce" not in shared
    assert behaviour.durable_trade_count() == 1
    assert behaviour.read_counted_placement_keys() == {"omen:0xfeed"}


def test_foreground_guard_respects_lane_rpc_pause(tmp_path: Any) -> None:
    """Use the lane's pause before the guard sends a timeout nonce read."""
    behaviour = pending_lane(tmp_path)
    guard = make_guard()
    guard.context.shared_state = behaviour.context.shared_state
    guard.context.default_ledger_id = "ethereum"
    guard.context.requests = SimpleNamespace(request_id_to_callback={})
    guard.context.outbox = MagicMock()
    guard.sleep = lambda delay: iter([None])
    state = {"nonce": "0x7"}

    def response() -> Any:
        method = json.loads(
            guard._build_http_request_message.call_args.kwargs["content"]
        )["method"]
        result = state["nonce"] if method == "eth_getTransactionCount" else None
        return SimpleNamespace(
            status_code=200,
            body=json.dumps({"result": result}).encode(),
        )

    dialogue = reply_on_send(guard, response)
    guard._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
    waiting = guard.wait_for_lightning_lane()
    for now in (1062, 1063.999):
        with patch("time.time", return_value=now):
            assert next(waiting) is None
        guard._build_http_request_message.assert_not_called()
        guard.context.outbox.put_message.assert_not_called()
        assert "lightning_lane_inflight" in guard.context.shared_state
        assert (behaviour._stats.calls, behaviour._stats.errors) == (5, 5)
        assert behaviour._stats.paused_until == 1064

    state["nonce"] = "0x8"
    with patch("time.time", return_value=1064):
        assert list(waiting) == []
    assert guard._build_http_request_message.call_count == 2
    assert guard.context.outbox.put_message.call_count == 2
    assert "lightning_lane_inflight" not in guard.context.shared_state


def test_guard_stops_second_read_if_lane_pauses_during_nonce(tmp_path: Any) -> None:
    """Recheck the shared pause after an outstanding guard read returns."""
    behaviour = pending_lane(tmp_path)
    guard = make_guard()
    guard.context.shared_state = behaviour.context.shared_state
    guard.context.default_ledger_id = "ethereum"
    guard.context.requests = SimpleNamespace(request_id_to_callback={})
    guard.context.outbox = MagicMock()
    guard.sleep = lambda delay: iter([None])

    def response() -> Any:
        behaviour._stats.paused_until = 1124
        result: Any = (
            "0x8"
            if guard._build_http_request_message.call_count == 1
            else {"status": "0x1", "blockNumber": "0x2"}
        )
        return SimpleNamespace(
            status_code=200, body=json.dumps({"result": result}).encode()
        )

    dialogue = reply_on_send(guard, response)
    guard._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
    with patch("time.time", return_value=1064):
        waiting = guard.wait_for_lightning_lane()
        assert next(waiting) is None
    waiting.close()
    assert guard._build_http_request_message.call_count == 1
    assert guard.context.outbox.put_message.call_count == 1
    assert "lightning_lane_inflight" in guard.context.shared_state


def test_saved_terminal_receipt_completes_during_rpc_pause(tmp_path: Any) -> None:
    """Finish saved receipt bookkeeping with no request while RPC is paused."""
    behaviour = pending_lane(tmp_path)
    info = behaviour.context.shared_state["lightning_lane_inflight"]
    info.update(
        receipt={"status": "0x1", "blockNumber": "0x2", "logs": []},
        receipt_block={"number": "0x2", "timestamp": hex(1005)},
    )
    behaviour._build_http_request_message.reset_mock()
    behaviour.context.outbox.put_message.reset_mock()
    with patch(MODULE + ".time.time", return_value=1005):
        run(behaviour.async_act())
    behaviour._build_http_request_message.assert_not_called()
    behaviour.context.outbox.put_message.assert_not_called()
    assert not behaviour._stats.ready(1005)
    assert (behaviour._stats.calls, behaviour._stats.errors) == (5, 5)
    assert "lightning_lane_inflight" not in behaviour.context.shared_state
    assert "lightning_lane_eoa_nonce" not in behaviour.context.shared_state
    assert behaviour.durable_trade_count() == 1
    assert behaviour.read_counted_placement_keys() == {"omen:0xfeed"}
    behaviour.read_bets()
    assert behaviour.bets[0].invested_amount == info["amount"]
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    assert queue.pending == []
    assert queue.asked == {}
    assert MARKET in behaviour.context.shared_state["lightning_lane_bet_markets"]
    assert len(json.loads((tmp_path / "activity_goal_trades.json").read_text())) == 1
