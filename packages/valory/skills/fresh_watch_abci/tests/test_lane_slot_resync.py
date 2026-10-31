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

"""Keep slot scans at the current head after the lane stops polling."""

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.lane import SlotPoller
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import done, run
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MODULE,
    lane,
)


@pytest.fixture
def scanner(tmp_path: Any) -> Any:
    """Use real lane scheduling with a five-second chain and no creations."""
    b = lane(tmp_path)
    b.context.params.fast = replace(b.params.fast, active_window_utc="00:00-05:30")
    b._checked = b._safe_ok = True
    b._refresh_idle = MagicMock(side_effect=lambda now: done(None))
    clock = [1000.0]

    def rpc(method: str, params: Any) -> Any:
        if method == "eth_blockNumber":
            return done(hex(int(clock[0] // 5)))
        if method == "eth_getBlockByNumber":
            height = int(params[0], 16)
            return done(
                {
                    "number": hex(height),
                    "timestamp": hex(height * 5),
                    "hash": hex(height),
                }
            )
        if method == "eth_getTransactionCount":
            return done("0x8")
        return done(None if method == "eth_getTransactionReceipt" else [])

    b._rpc = MagicMock(side_effect=rpc)
    return b, clock


def test_slot_poller_resyncs_after_two_overdue_slots() -> None:
    """Discard an overdue wanted block before any historical block read."""
    poller = SlotPoller()
    poller.height("0xc8", 1000)
    poller.block({"number": "0xc8", "timestamp": hex(1000)}, 1000)
    assert poller.request(poller.next_at + 10) == (
        "eth_getBlockByNumber",
        ["0xc9", False],
    )
    assert poller.request(poller.next_at + 10.01) == ("eth_blockNumber", [])
    assert poller.wanted is None
    poller.height("0xdc", 1100)
    assert poller.request(1100) == ("eth_getBlockByNumber", ["0xdc", False])


def test_one_background_lane_resyncs_across_active_windows(scanner: Any) -> None:
    """Start the next active window at its head without old blocks or logs."""
    b, clock = scanner
    clock[0] = 19795
    with patch(MODULE + ".time.time", side_effect=lambda: clock[0]):
        run(b.async_act())
        run(b.async_act())
        assert b._poller.wanted == 3960
        b._rpc.reset_mock()
        clock[0] = 20000
        run(b.async_act())
        b._rpc.assert_not_called()
        inactive_poller = b._poller
        clock[0] = 86400
        run(b.async_act())
        run(b.async_act())
    assert [call.args[0] for call in b._rpc.call_args_list] == [
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getLogs",
    ]
    assert b._rpc.call_args_list[1].args[1] == [hex(17280), False]
    assert b._rpc.call_args_list[2].args[1][0]["blockHash"] == hex(17280)
    assert inactive_poller is None


def test_lane_resyncs_after_long_receipt_wait(scanner: Any) -> None:
    """Resume at the current head after a pending shot releases its nonce."""
    b, clock = scanner
    with patch(MODULE + ".time.time", side_effect=lambda: clock[0]):
        run(b.async_act())
        run(b.async_act())
        b.context.shared_state["lightning_lane_inflight"] = {
            "tx": "0xold",
            "eoa_nonce": 7,
            "sent_at": 1001,
        }
        clock[0] = 1001
        run(b.async_act())
        assert "lightning_lane_inflight" in b.context.shared_state
        clock[0] = 1100
        run(b.async_act())
        assert "lightning_lane_inflight" not in b.context.shared_state
        b._rpc.reset_mock()
        run(b.async_act())
        run(b.async_act())
    assert [call.args[0] for call in b._rpc.call_args_list] == [
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getLogs",
    ]
    assert b._rpc.call_args_list[1].args[1] == ["0xdc", False]
    assert b._rpc.call_args_list[2].args[1][0]["blockHash"] == "0xdc"


def test_lane_resyncs_after_rpc_error_pause(scanner: Any) -> None:
    """Resume at the current head after five errors pause RPC reads."""
    b, clock = scanner
    with patch(MODULE + ".time.time", side_effect=lambda: clock[0]):
        run(b.async_act())
        run(b.async_act())
        for _ in range(5):
            b._stats.called(1001, error=True)
        b._rpc.reset_mock()
        clock[0] = 1020
        run(b.async_act())
        b._rpc.assert_not_called()
        clock[0] = 1061
        run(b.async_act())
        run(b.async_act())
    assert [call.args[0] for call in b._rpc.call_args_list] == [
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getLogs",
    ]
    assert b._rpc.call_args_list[1].args[1] == ["0xd4", False]
    assert b._rpc.call_args_list[2].args[1][0]["blockHash"] == "0xd4"
