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

"""Check the independent lane against its runtime safety boundaries."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from eth_abi import encode
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)

MODULE = "packages.valory.skills.fresh_watch_abci.lane_behaviour"
OWNER = "0x" + "12" * 20
SAFE = "0x" + "34" * 20
MARKET = "0x" + "56" * 20
TOKEN = "0x" + "78" * 20


def lane(tmp_path: Any, **changes: Any) -> Any:
    """Use real storage and replace only external I/O."""
    from packages.valory.skills.fresh_watch_abci.lane_behaviour import (
        LightningLaneBehaviour,
    )

    settings = dict(
        asdict(LightningConfig()),
        lightning_lane=True,
        lightning_rpc_url="https://rpc.test/private-key",
        lightning_poll_offset_s=0.3,
        lightning_poll_step_s=0.25,
        lightning_lane_tip_gwei=550,
        lightning_lane_tip_cap_gwei=1000,
        lightning_lane_gas_limit=400000,
        lightning_lane_max_age_s=6,
        lightning_lane_inflight_timeout_s=60,
        lightning_lane_max_per_night=3,
    )
    settings.update(changes)

    def construct(**kwargs: Any) -> Any:
        kwargs["skill_context"].shared_state = {}
        kwargs["skill_context"].params.lightning = SimpleNamespace(**settings)
        return LightningLaneBehaviour(**kwargs)

    b = make(construct, tmp_path)
    b.context.params.lightning = SimpleNamespace(**settings)
    b.context.agent_address = OWNER
    b.context.default_ledger_id = "ethereum"
    b.context.state.chatui_config.trading_strategy = "kelly_criterion"
    b.context.params.multisend_address = "0x" + "90" * 20
    b.context.params.wxdai_contract_address = TOKEN
    b.context.params.mech_chain_id = "gnosis"
    saved_shared = b.context.shared_state
    b.context.shared_state = saved_shared if isinstance(saved_shared, dict) else {}
    b.context.state.round_sequence.current_round = type("FreshWatchRound", (), {})()
    b.sleep = MagicMock(side_effect=lambda delay: done(None))
    return b


def test_lane_off_has_no_rpc_or_consensus(tmp_path: Any) -> None:
    """Leave the existing background loop alone with the flag off."""
    b = lane(tmp_path, lightning_lane=False)
    b._rpc = MagicMock()
    b.send_a2a_transaction = MagicMock()
    run(b.async_act())
    b._rpc.assert_not_called()
    b.send_a2a_transaction.assert_not_called()


@pytest.mark.parametrize("url,tip", [("", 550), ("https://rpc.test", 0)])
def test_lane_requires_url_and_positive_fixed_tip(
    tmp_path: Any, url: str, tip: float
) -> None:
    """Refuse an incomplete opt-in and log the reason once."""
    b = lane(tmp_path, lightning_rpc_url=url, lightning_lane_tip_gwei=tip)
    b._rpc = MagicMock()
    run(b.async_act())
    run(b.async_act())
    b._rpc.assert_not_called()
    assert b.context.logger.info.call_count == 1


@pytest.mark.parametrize(
    "round_name,offchain,preparing,allowed",
    [
        ("FreshWatchRound", True, False, True),
        ("FastDecisionRound", True, False, True),
        ("DecisionRequestRound", True, False, True),
        ("TradeCountRound", True, False, True),
        ("MechOnlySelectionRound", True, False, True),
        ("MechRequestRound", True, False, True),
        ("MechResponseRound", True, False, True),
        ("MechRequestRound", False, False, False),
        ("MechResponseRound", False, False, False),
        ("BetPlacementRound", True, False, False),
        ("RedeemRound", True, False, False),
        ("CallCheckpointRound", True, False, False),
        ("RandomnessTransactionSubmissionRound", True, False, False),
        ("FreshWatchRound", True, True, False),
    ],
)
def test_lane_round_exclusion(
    tmp_path: Any,
    round_name: str,
    offchain: bool,
    preparing: bool,
    allowed: bool,
) -> None:
    """Use an explicit whitelist and exclude every Safe preparation."""
    b = lane(tmp_path)
    b.context.params.use_offchain = offchain
    b.context.state.round_sequence.current_round = type(round_name, (), {})()
    b.context.shared_state["lightning_lane_fsm_preparing"] = preparing
    assert b._round_allowed() is allowed


@pytest.mark.parametrize(
    "threshold,owner,allowed", [(1, True, True), (2, True, False), (1, False, False)]
)
def test_startup_checks_safe_owner_and_threshold(
    tmp_path: Any, threshold: int, owner: bool, allowed: bool
) -> None:
    """Fail closed for a Safe that cannot use this owner's signature."""
    b = lane(tmp_path)
    b._rpc = MagicMock(
        side_effect=[
            done("0x" + encode(["uint256"], [threshold]).hex()),
            done("0x" + encode(["bool"], [owner]).hex()),
        ]
    )
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.safe_contract_address = SAFE
        assert run(b._startup()) is allowed
    assert b._rpc.call_args_list[0].args[0] == "eth_call"
    assert all(call.args[0] == "eth_call" for call in b._rpc.call_args_list)
    assert "private-key" not in str(b.context.logger.method_calls)


def test_idle_nonce_cache_requires_read_after_last_fsm_settlement(
    tmp_path: Any,
) -> None:
    """Refresh the pending nonce before a later lane transaction can use it."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_eoa_nonce"] = {"nonce": 7, "read_at": 1000}
    b.context.shared_state["lightning_lane_last_fsm_settlement"] = 1001
    b._rpc = MagicMock(
        side_effect=[
            done("0x8"),
            done(hex(10**20)),
            done(hex(10**20)),
            done("0x" + encode(["uint256"], [10**20]).hex()),
        ]
    )
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=1002),
    ):
        sd.return_value.safe_contract_address = SAFE
        run(b._refresh_idle(1002))
        run(b._refresh_idle(1003))
    assert b.context.shared_state["lightning_lane_eoa_nonce"] == {
        "nonce": 8,
        "read_at": 1002,
    }
    assert b._rpc.call_count == 4
    assert b._rpc.call_args_list[0].args == (
        "eth_getTransactionCount",
        [OWNER, "pending"],
    )
    assert not any("TransactionBy" in call.args[0] for call in b._rpc.call_args_list)


def test_rpc_uses_http_client_and_redacts_rpc_errors(tmp_path: Any) -> None:
    """Send raw JSON-RPC without exposing a keyed endpoint in logs."""
    b = lane(tmp_path)
    b._build_http_request_message = MagicMock(return_value=(MagicMock(), MagicMock()))
    b._do_request = MagicMock(
        return_value=done(
            SimpleNamespace(
                status_code=200, body=json.dumps({"result": "0x9"}).encode()
            )
        )
    )
    assert run(b._rpc("eth_blockNumber", [])) == "0x9"
    request = b._build_http_request_message.call_args.kwargs
    assert request["url"] == "https://rpc.test/private-key"
    assert json.loads(request["content"])["method"] == "eth_blockNumber"
    assert request["method"] == "POST"
    assert "private-key" not in str(b.context.logger.method_calls)


def prepared(b: Any) -> None:
    """Set the idle caches required for a direct shot."""
    b.context.shared_state["lightning_lane_eoa_nonce"] = {"nonce": 7, "read_at": 1001}
    b._balances = {"eoa": 10**20, "native": 10**20, "wrapped": 10**20}
    b._balance_at = 1001
    b._shots = 0
    b._night = "1970-01-01"


def test_send_reserves_inflight_before_both_broadcasts(tmp_path: Any) -> None:
    """Use a fixed tip and fan out without waiting for the ledger result."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path)
    prepared(b)
    raw = bytes.fromhex("abcdef")
    tx_hash = "0x" + keccak(raw).hex()
    response = SimpleNamespace(
        performative=SigningMessage.Performative.SIGNED_TRANSACTION,
        signed_transaction=SignedTransaction(
            "ethereum", {"raw_transaction": "0x" + raw.hex(), "hash": tx_hash}
        ),
    )
    b.wait_for_message = MagicMock(return_value=done(response))
    b._send_transaction_signing_request = MagicMock()
    seen = []
    b._broadcast = lambda signed, raw_tx: seen.append(
        dict(b.context.shared_state["lightning_lane_inflight"])
    )
    market = raw_market()
    market.update(id=MARKET, collateralToken=TOKEN)
    decision = SimpleNamespace(stake=4.0, price=0.6, p_hat=0.95)
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=1002),
    ):
        sd.return_value.safe_contract_address = SAFE
        assert run(
            b._send(
                market,
                decision,
                (0.9, 4444444444444444445),
                {"baseFeePerGas": "0x1", "number": "0x2"},
                1002,
            )
        )
    tx = b._send_transaction_signing_request.call_args.args[0].body
    assert tx["maxPriorityFeePerGas"] == 550 * 10**9
    assert tx["maxFeePerGas"] == 550 * 10**9 + 2
    assert tx["nonce"] == 7
    assert tx["chainId"] == 100
    assert seen[0]["tx"] == tx_hash
    assert seen[0]["market"] == MARKET
    assert b._shots == 1
    b.wait_for_message.assert_called_once()


def test_broadcast_sends_http_and_ledger_without_response_wait(tmp_path: Any) -> None:
    """Put both requests in the outbox before any response can block the shot."""
    b = lane(tmp_path)
    dialogue = MagicMock()
    b._build_http_request_message = MagicMock(return_value=("http request", dialogue))
    b._get_request_nonce_from_dialogue = MagicMock(return_value="direct")
    b._send_transaction_request = MagicMock()
    b.context.ledger_api_dialogues.create.return_value = ("ledger request", MagicMock())
    b.context.requests.request_id_to_callback = {}
    b._broadcast(SimpleNamespace(signed_transaction="signed"), "0x1234")
    assert b.context.outbox.put_message.call_count == 2
    assert (
        b.context.outbox.put_message.call_args_list[0].kwargs["message"]
        == "http request"
    )
    assert (
        b.context.outbox.put_message.call_args_list[1].kwargs["message"]
        == "ledger request"
    )
    b.context.ledger_api_dialogues.create.assert_called_once()
    assert "direct" in b.context.requests.request_id_to_callback


def test_nonce_too_low_drops_inflight_without_counting(tmp_path: Any) -> None:
    """Return the market to the foreground when the pending nonce has moved."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = {
        "tx": "0xtx",
        "market": MARKET,
        "eoa_nonce": 7,
        "sent_at": 1000,
    }
    b._handle_send_result({"error": {"message": "nonce too low"}})
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert "lightning_lane_eoa_nonce" not in b.context.shared_state
    assert b.durable_trade_count() == 0


def settled(b: Any, tmp_path: Any) -> dict:
    """Queue a real Bet row and describe its signed lane transaction."""
    row = raw_market()
    row.update(id=MARKET, collateralToken=TOKEN)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [row]
    queue.asked = {MARKET: 1000}
    queue.save()
    return dict(
        tx="0xfeed",
        market=MARKET,
        eoa_nonce=7,
        sent_at=1001,
        row=row,
        amount=4 * 10**18,
        probability=0.95,
    )


def test_mined_receipt_updates_all_bet_bookkeeping_once(tmp_path: Any) -> None:
    """Store investments, attribution, activity and MAX_TRADES exactly once."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {
        "status": "0x1",
        "blockNumber": "0x2",
        "transactionIndex": "0x0",
        "gasUsed": "0x3a980",
        "effectiveGasPrice": hex(550 * 10**9),
        "logs": [],
    }
    with (
        patch(MODULE + ".time.time", return_value=1005),
        patch(
            MODULE + ".buy_result",
            return_value={"tokens": 7 * 10**18, "amount": 4 * 10**18},
        ),
    ):
        b._complete(info, receipt, {"timestamp": hex(1005)})
        b._complete(info, receipt, {"timestamp": hex(1005)})
    b.read_bets()
    assert len(b.bets) == 1
    assert b.bets[0].invested_amount == 4 * 10**18
    assert b.bets[0].strategy_vote == 0
    assert b.bets[0].processed_timestamp == 1005
    assert b.bets[0].strategy is not None
    assert b.durable_trade_count() == 1
    assert b.read_counted_placement_keys() == {"omen:0xfeed"}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    assert queue.pending == []
    assert queue.asked == {}
    trades = json.loads((tmp_path / "activity_goal_trades.json").read_text())
    assert len(trades) == 1
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert MARKET in b.context.shared_state["lightning_lane_bet_markets"]


def test_reverted_receipt_leaves_market_to_fsm(tmp_path: Any) -> None:
    """Count shots, but never count reverted transactions as trades."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {
        "status": "0x0",
        "transactionIndex": "0x1",
        "gasUsed": hex(100000),
        "effectiveGasPrice": hex(550 * 10**9),
        "logs": [],
    }
    b._complete(info, receipt, {"timestamp": hex(1005)})
    assert b.durable_trade_count() == 0
    assert FreshQueue(tmp_path / "fresh_watch.json").pending[0]["id"] == MARKET
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert "status=reverted" in str(b.context.logger.method_calls)
    assert "0.055" in str(b.context.logger.method_calls)


def test_receipt_timeout_refreshes_nonce_before_clearing(tmp_path: Any) -> None:
    """Allow the foreground to resume only after a fresh pending nonce read."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    observed = []

    def rpc(method: str, params: Any) -> Any:
        observed.append((method, "lightning_lane_inflight" in b.context.shared_state))
        return done(None if method == "eth_getTransactionReceipt" else "0x8")

    b._rpc = rpc
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
    assert observed == [
        ("eth_getTransactionReceipt", True),
        ("eth_getTransactionCount", True),
    ]
    assert b.context.shared_state["lightning_lane_eoa_nonce"]["nonce"] == 8
    assert "lightning_lane_inflight" not in b.context.shared_state


def test_fresh_entry_clears_finished_fsm_preparation(tmp_path: Any) -> None:
    """A completed FSM cycle must release its background preparation marker."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_fsm_preparing"] = True
    b._rpc = MagicMock(
        side_effect=[done("0x8"), done("0x10"), done("0x20"), done("0x30")]
    )
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.safe_contract_address = SAFE
        run(b._refresh_idle(1002))
    assert not b.context.shared_state.get("lightning_lane_fsm_preparing")
    assert b.context.shared_state["lightning_lane_eoa_nonce"]["nonce"] == 8


def test_receipt_uses_mined_block_timestamp_after_a_missed_slot(tmp_path: Any) -> None:
    """Log the transaction's actual age if detection skips a block."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {
        "status": "0x0",
        "blockNumber": "0x2",
        "gasUsed": "0x1",
        "effectiveGasPrice": "0x1",
    }
    b._rpc = MagicMock(
        side_effect=[done(receipt), done({"timestamp": hex(1005), "number": "0x2"})]
    )
    b._complete = MagicMock()
    run(b._watch_receipt({"timestamp": hex(1010), "number": "0x3"}))
    assert b._complete.call_args.args[2]["timestamp"] == hex(1005)
    assert b._rpc.call_args_list[1].args == ("eth_getBlockByNumber", ["0x2", False])


def test_restart_restores_pending_lock_and_nightly_shot_count(tmp_path: Any) -> None:
    """Retain the nonce lock and shot budget across a process restart."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._night, b._shots = "1970-01-01", 3
    b._persist()
    restarted = lane(tmp_path)
    assert restarted.context.shared_state["lightning_lane_inflight"] == info
    assert (restarted._night, restarted._shots) == ("1970-01-01", 3)


def test_receipt_retry_after_bet_store_does_not_repeat_investment(
    tmp_path: Any,
) -> None:
    """Retry a partial durable update after the bet file was written."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {
        "status": "0x1",
        "gasUsed": "0x1",
        "effectiveGasPrice": "0x1",
        "logs": [],
    }
    with (
        patch(MODULE + ".record_trade", side_effect=OSError("disk unavailable")),
        pytest.raises(OSError),
    ):
        b._complete(info, receipt, {"timestamp": hex(1005)})
    b._complete(info, receipt, {"timestamp": hex(1005)})
    b.read_bets()
    assert b.bets[0].invested_amount == 4 * 10**18
    assert b.durable_trade_count() == 1


def test_flag_off_does_not_read_or_restore_the_lane_journal(tmp_path: Any) -> None:
    """An existing pending lane file must have no effect while disabled."""
    path = tmp_path / "lightning_lane_state.json"
    path.write_text(
        json.dumps({"night": "1970-01-01", "shots": 3, "inflight": {"tx": "0x123"}})
    )
    with patch(
        "pathlib.Path.read_text", side_effect=AssertionError("disabled journal read")
    ):
        b = lane(tmp_path, lightning_lane=False)
    assert "lightning_lane_inflight" not in b.context.shared_state


def test_fresh_entry_keeps_termination_preparation_locked(tmp_path: Any) -> None:
    """A background termination Safe transaction must retain its lock."""
    b = lane(tmp_path)
    b.context.shared_state.update(
        lightning_lane_fsm_preparing=True,
        lightning_lane_fsm_preparing_round="background_round",
    )
    b._rpc = MagicMock()
    run(b._refresh_idle(1002))
    assert b.context.shared_state["lightning_lane_fsm_preparing"]
    b._rpc.assert_not_called()


def test_journal_does_not_rewrite_unchanged_state_each_tick(tmp_path: Any) -> None:
    """Keep the slot loop free of repeated disk writes while state is idle."""
    b = lane(tmp_path)
    with patch(MODULE + ".write_json_atomically") as write:
        b._persist()
        b._persist()
    write.assert_called_once()


@pytest.mark.parametrize("funding", [True, False])
def test_creation_receipt_reaches_signed_send_in_one_tick(
    tmp_path: Any, funding: bool
) -> None:
    """Use the chain fixture, local quote and local encoding in one lane step."""
    from pathlib import Path

    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage
    from packages.valory.skills.fresh_watch_abci.chain_discovery import creation_details

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
    )
    creation = fixture["logs"][-1]
    market = creation_details(creation)
    created = fixture["blockTimestamp"]
    b = lane(tmp_path, lightning_still_hold=True)
    prepared(b)
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
    logs = fixture["logs"].copy()
    if funding:
        logs.append(
            dict(
                address=market["id"],
                topics=[
                    "0x"
                    + keccak(text="FPMMFundingAdded(address,uint256[],uint256)").hex()
                ],
                data="0x"
                + encode(
                    ["uint256[]", "uint256"], [[7 * 10**18, 7 * 10**18], 7 * 10**18]
                ).hex(),
            )
        )
    responses = [done({"logs": logs})]
    if not funding:
        responses.append(
            done("0x" + encode(["uint256[]"], [[7 * 10**18, 7 * 10**18]]).hex())
        )
    b._rpc = MagicMock(side_effect=responses)
    b.wait_for_message = MagicMock(
        return_value=done(
            SimpleNamespace(
                performative=SigningMessage.Performative.SIGNED_TRANSACTION,
                signed_transaction=SignedTransaction(
                    "ethereum", {"raw_transaction": "0xabcdef"}
                ),
            )
        )
    )
    b._send_transaction_signing_request = MagicMock()
    b._broadcast = MagicMock()
    block = {
        "number": hex(creation["blockNumber"]),
        "hash": "0x" + "ab" * 32,
        "timestamp": hex(created),
        "baseFeePerGas": "0x1",
    }
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=created + 0.4),
    ):
        sd.return_value.safe_contract_address = SAFE
        sd.return_value.previous_checkpoint = None
        sd.return_value.successful_trade_count = 0
        run(b._market(creation, block, created + 0.3))
        assert (
            b.context.shared_state["lightning_lane_inflight"]["market"] == market["id"]
        )
        assert b._rpc.call_count == 1 + int(not funding)
        run(b._market(creation, block, created + 0.3))
    assert b._rpc.call_count == 1 + int(not funding)
    assert b._shots == 1
    tx = b._send_transaction_signing_request.call_args.args[0].body
    assert tx["maxPriorityFeePerGas"] == 550 * 10**9
    assert tx["data"].startswith("0x6a761202")
    b._broadcast.assert_called_once()


def test_missing_receipt_title_stays_with_fsm(tmp_path: Any) -> None:
    """Do not perform a subgraph title fallback inside the direct lane."""
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
    )
    creation = fixture["logs"][-1]
    b = lane(tmp_path, lightning_still_hold=True)
    b._rpc = MagicMock(return_value=done({"logs": [creation]}))
    b._send = MagicMock()
    run(
        b._market(
            creation,
            {
                "timestamp": hex(fixture["blockTimestamp"]),
                "hash": "0xblock",
                "number": hex(creation["blockNumber"]),
            },
            fixture["blockTimestamp"] + 0.3,
        )
    )
    b._send.assert_not_called()
    assert b._rpc.call_count == 1


def test_slot_tick_scans_exact_block_before_any_idle_refresh(tmp_path: Any) -> None:
    """Run getLogs by hash before idle refresh I/O can consume a new slot."""
    from packages.valory.skills.fresh_watch_abci.lane import SlotPoller

    b = lane(tmp_path)
    b._checked = b._safe_ok = True
    b._poller = SlotPoller()
    b._poller.height("0x2", 1000)
    prepared(b)
    block = {"timestamp": hex(1000), "number": "0x2", "hash": "0xblock"}
    calls = []

    def rpc(method: str, params: Any) -> Any:
        calls.append(method)
        return done(block if method == "eth_getBlockByNumber" else [])

    b._rpc = rpc

    def idle(now: float) -> Any:
        calls.append("idle_refresh")
        return done(None)

    b._refresh_idle = idle
    with patch(MODULE + ".time.time", return_value=1000.3):
        run(b.async_act())
    assert calls[:2] == ["eth_getBlockByNumber", "eth_getLogs"]
    assert b._stats.detections == pytest.approx([0.3])


def test_unsigned_restored_shot_times_out_without_receipt_poll(tmp_path: Any) -> None:
    """Release a restored pre-sign lock after a fresh pending nonce read."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info["tx"] = None
    b.context.shared_state["lightning_lane_inflight"] = info
    b._rpc = MagicMock(return_value=done("0x8"))
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
    b._rpc.assert_called_once_with("eth_getTransactionCount", [OWNER, "pending"])
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert b.context.shared_state["lightning_lane_eoa_nonce"]["nonce"] == 8


def test_nonce_drop_log_keeps_market_and_required_result_fields(tmp_path: Any) -> None:
    """Report an unmined drop with the same result fields as a revert."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = settled(b, tmp_path)
    b._handle_send_result({"error": {"message": "nonce too low"}})
    entry = b.context.logger.info.call_args
    rendered = entry.args[0] % entry.args[1:]
    for field in (
        "market=" + MARKET,
        "tx=0xfeed",
        "status=dropped",
        "gas_used=0",
        "fee_xdai=0",
        "index=unknown",
        "block_age=",
    ):
        assert field in rendered


@pytest.mark.parametrize("body", [b"upstream unavailable", b'{"error": "invalid"}'])
def test_malformed_send_callback_is_bounded_to_lane(tmp_path: Any, body: bytes) -> None:
    """A non-JSON RPC response must not raise from the message handler."""
    b = lane(tmp_path)
    b._build_http_request_message = MagicMock(return_value=("http", MagicMock()))
    b._get_request_nonce_from_dialogue = MagicMock(side_effect=["http", "ledger"])
    b.context.ledger_api_dialogues.create.return_value = ("ledger", MagicMock())
    b.context.requests.request_id_to_callback = {}
    b._broadcast(SimpleNamespace(signed_transaction="signed"), "0x1234")
    callback = b.context.requests.request_id_to_callback["http"]
    callback(SimpleNamespace(body=body), None)
    assert b._stats.errors == 1


@pytest.mark.parametrize("expires", ["window", "market"])
def test_signing_wait_cannot_cross_trade_deadline(tmp_path: Any, expires: str) -> None:
    """Check the active window and market close again after the signing wait."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage
    from packages.valory.skills.fresh_watch_abci.strategy import FastConfig

    b = lane(tmp_path, lightning_lane_max_age_s=60)
    prepared(b)
    market = raw_market()
    market.update(id=MARKET, collateralToken=TOKEN)
    clock = {"now": 1002}
    if expires == "window":
        b.context.params.fast = FastConfig(active_window_utc="00:00-00:17")
        after_signing = 1021
    else:
        market["openingTimestamp"] = 1003
        after_signing = 1004

    def signed_response(**_kwargs: Any) -> Any:
        clock["now"] = after_signing
        return done(
            SimpleNamespace(
                performative=SigningMessage.Performative.SIGNED_TRANSACTION,
                signed_transaction=SignedTransaction(
                    "ethereum", {"raw_transaction": "0xabcdef"}
                ),
            )
        )

    b.wait_for_message = signed_response
    b._send_transaction_signing_request = MagicMock()
    b._broadcast = MagicMock()
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", side_effect=lambda: clock["now"]),
    ):
        sd.return_value.safe_contract_address = SAFE
        sent = run(
            b._send(
                market,
                SimpleNamespace(stake=4.0, price=0.6, p_hat=0.95),
                (0.9, 4444444444444444445),
                {"baseFeePerGas": "0x1", "number": "0x2"},
                1002,
            )
        )
    assert sent is False
    assert "lightning_lane_inflight" not in b.context.shared_state
    b._broadcast.assert_not_called()
    assert b._shots == 0
