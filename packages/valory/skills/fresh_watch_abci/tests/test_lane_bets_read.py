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

"""Keep stored positions when the lane cannot read the bets."""

import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from aea.helpers.transaction.base import SignedTransaction
from eth_abi import encode

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.skills.fresh_watch_abci.chain_discovery import creation_details
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import done, run
from packages.valory.skills.fresh_watch_abci.tests.test_foreground_lane_races import (
    market_bet,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    MODULE,
    SAFE,
    lane,
    prepared,
    reply_on_send,
    settled,
)


def test_admission_bets_read_failure_does_not_repeat_stored_purchase(
    tmp_path: Any,
) -> None:
    """Decline a failed read after restart, then find the stored purchase."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
    )
    creation = fixture["logs"][-1]
    market = creation_details(creation)
    created = fixture["blockTimestamp"]
    writer = lane(tmp_path)
    bet = market_bet(market["id"])
    bet.set_investment_amount(0, 4 * 10**18)
    writer.bets = [bet]
    writer.store_bets()
    writer.record_successful_placement("omen:previous-purchase")
    path = Path(writer.multi_bets_filepath)
    original_bets = path.read_bytes()

    b = lane(tmp_path, lightning_still_hold=True)
    prepared(b)
    assert not b.bets
    assert not b.context.shared_state.get("lightning_lane_bet_markets")
    (tmp_path / "lightning_base_rate.json").write_text(
        json.dumps(
            dict(
                n=100,
                rate=0.95,
                template_id=2,
                creators=sorted(b.params.lightning.lightning_creators),
                window_days=21,
                refreshed_at=created,
            )
        )
    )
    b._rpc = MagicMock(
        side_effect=lambda method, _params: done(
            {"logs": fixture["logs"]}
            if method == "eth_getTransactionReceipt"
            else "0x" + encode(["uint256[]"], [[7 * 10**18] * 2]).hex()
        )
    )
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    b._broadcast = MagicMock()
    block = {
        "number": hex(creation["blockNumber"]),
        "hash": "0xblock",
        "timestamp": hex(created),
        "baseFeePerGas": "0x1",
        "gasLimit": "0x1000000",
    }
    original_open = builtins.open
    failed: list[bool] = []

    def fail_read(filename: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(filename) == path and mode == "r" and not failed:
            failed.append(True)
            raise OSError("single bets read failure")
        return original_open(filename, mode, *args, **kwargs)

    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=created + 0.4),
    ):
        sd.return_value.safe_contract_address = SAFE
        sd.return_value.previous_checkpoint = None
        sd.return_value.successful_trade_count = 0
        with patch("builtins.open", side_effect=fail_read):
            run(b._market(creation, block, created + 0.3))
        assert failed == [True]
        b.context.signing_dialogues.create.assert_not_called()
        b.context.logger.info.assert_called_once_with(
            "[LIGHTNING_LANE] market=%s reason=bets_read_failed", market["id"]
        )
        run(b._market(creation, block, created + 0.3))
    assert [(stored.id, stored.invested_amount) for stored in b.bets] == [
        (market["id"], 4 * 10**18)
    ]
    b.context.signing_dialogues.create.assert_not_called()
    b.context.decision_maker_message_queue.put_nowait.assert_not_called()
    b._broadcast.assert_not_called()
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert b._shots == 0
    assert b.durable_trade_count() == 1
    assert path.read_bytes() == original_bets
    assert not (tmp_path / "lightning_lane_state.json").exists()


@pytest.mark.parametrize("failure", ["unreadable", "read", "decode", "type"])
def test_completion_keeps_positions_and_journal_after_bets_read_failure(
    tmp_path: Any, failure: str
) -> None:
    """Retry a saved receipt only after all existing positions can be read."""
    b = lane(tmp_path)
    older = market_bet("older-position")
    older.set_investment_amount(1, 2 * 10**18)
    b.bets = [older]
    b.store_bets()
    path = Path(b.multi_bets_filepath)
    original_bets = path.read_bytes()
    info = settled(b, tmp_path)
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    block = {"timestamp": hex(1005), "number": "0x2"}
    info.update(receipt=receipt, receipt_block=block)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._persist()
    journal = tmp_path / "lightning_lane_state.json"
    original_journal = journal.read_bytes()
    b._rpc = MagicMock(side_effect=AssertionError("saved receipt needs no RPC"))
    original_open = builtins.open
    failed: list[bool] = []

    def fail_read(filename: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(filename) == path and mode == "r" and not failed:
            failed.append(True)
            if failure == "unreadable":
                raise PermissionError("bets store is unreadable")
            raise OSError("single bets read failure")
        return original_open(filename, mode, *args, **kwargs)

    if failure in {"unreadable", "read"}:
        failure_context = patch("builtins.open", side_effect=fail_read)
    else:
        error = (
            json.JSONDecodeError("bad bets", "", 0)
            if failure == "decode"
            else TypeError("bad bet fields")
        )
        failure_context = patch("json.load", side_effect=error)
    with failure_context:
        run(b.async_act())
    assert b.context.shared_state.get("lightning_lane_inflight") is info
    assert b.durable_trade_count() == 0
    assert b.read_counted_placement_keys() == set()
    assert journal.read_bytes() == original_journal
    assert not (tmp_path / "activity_goal_trades.json").exists()
    assert FreshQueue(tmp_path / "fresh_watch.json").pending[0]["id"] == MARKET
    assert path.read_bytes() == original_bets
    if failure in {"unreadable", "read"}:
        assert failed == [True]

    run(b.async_act())
    b._complete(info, receipt, block)
    b.read_bets()
    assert {bet.id: bet.invested_amount for bet in b.bets} == {
        "older-position": 2 * 10**18,
        MARKET: info["amount"],
    }
    assert b.durable_trade_count() == 1
    assert b.read_counted_placement_keys() == {"omen:0xfeed"}
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert json.loads(journal.read_text())["inflight"] is None
    assert len(json.loads((tmp_path / "activity_goal_trades.json").read_text())) == 1
    b._rpc.assert_not_called()


@pytest.mark.parametrize("stored", ["absent", "legacy", "multi"])
def test_strict_bets_read_accepts_missing_store_and_legacy_fallback(
    tmp_path: Any, stored: str
) -> None:
    """Treat only absent files as empty or eligible for the legacy fallback."""
    b = lane(tmp_path)
    if stored != "absent":
        b.bets = [market_bet(stored)]
        b.store_bets()
        if stored == "legacy":
            Path(b.multi_bets_filepath).rename(b.bets_filepath)
    b.read_bets(strict=True)
    assert [bet.id for bet in b.bets] == ([] if stored == "absent" else [stored])
