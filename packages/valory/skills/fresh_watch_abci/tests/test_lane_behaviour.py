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


def reply_on_send(b: Any, response: Any, signing: bool = False) -> Any:
    """Deliver a connection reply through the real request callback."""
    dialogue = MagicMock()
    dialogue.dialogue_label.dialogue_reference = ("reply", "")
    b.context.requests.request_id_to_callback = {}

    def deliver(_message: Any) -> None:
        value = response() if callable(response) else response
        callback = b.context.requests.request_id_to_callback.pop("reply")
        callback(value, b)

    if signing:
        b.context.signing_dialogues.create.return_value = (MagicMock(), dialogue)
        b.context.decision_maker_message_queue.put_nowait.side_effect = deliver
    else:
        b.context.outbox.put_message.side_effect = lambda **kwargs: deliver(
            kwargs["message"]
        )
    return dialogue


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
    dialogue = reply_on_send(
        b, SimpleNamespace(status_code=200, body=json.dumps({"result": "0x9"}).encode())
    )
    b._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
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
    reply_on_send(b, response, signing=True)
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
    tx = b.context.signing_dialogues.create.call_args.kwargs["raw_transaction"].body
    assert tx["maxPriorityFeePerGas"] == 550 * 10**9
    assert tx["maxFeePerGas"] == 550 * 10**9 + 2
    assert tx["nonce"] == 7
    assert tx["chainId"] == 100
    assert seen[0]["tx"] == tx_hash
    assert seen[0]["market"] == MARKET
    assert b._shots == 1
    b.context.decision_maker_message_queue.put_nowait.assert_called_once()


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


def test_nonce_too_low_keeps_inflight_without_counting(tmp_path: Any) -> None:
    """Keep exclusion until the originating transaction can be reconciled."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = {
        "tx": "0xtx",
        "market": MARKET,
        "eoa_nonce": 7,
        "sent_at": 1000,
    }
    b._handle_send_result({"error": {"message": "nonce too low"}}, "0xtx")
    assert b.context.shared_state["lightning_lane_inflight"]["tx"] == "0xtx"
    assert b.context.shared_state["lightning_lane_inflight"]["reconcile"] is True
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


def test_confirmed_replacement_rechecks_receipt_before_clearing(tmp_path: Any) -> None:
    """Release after a consumed confirmed nonce and a second absent receipt."""
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
        ("eth_getTransactionReceipt", True),
    ]
    assert "lightning_lane_eoa_nonce" not in b.context.shared_state
    assert "dropped_replaced" in str(b.context.logger.info.call_args)
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
    tx = b.context.signing_dialogues.create.call_args.kwargs["raw_transaction"].body
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
    """Release a restored pre-sign lock after its nonce is confirmed consumed."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info["tx"] = None
    b.context.shared_state["lightning_lane_inflight"] = info
    b._rpc = MagicMock(return_value=done("0x8"))
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
    b._rpc.assert_called_once_with("eth_getTransactionCount", [OWNER, "latest"])
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert "lightning_lane_eoa_nonce" not in b.context.shared_state


def test_nonce_drop_log_keeps_market_and_required_result_fields(tmp_path: Any) -> None:
    """Report an unmined drop with the same result fields as a revert."""
    b = lane(tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = settled(b, tmp_path)
    b._rpc = MagicMock(side_effect=[done(None), done("0x8"), done(None)])
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
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
    b.context.shared_state["lightning_lane_inflight"] = settled(b, tmp_path)
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

    def signed_response() -> Any:
        clock["now"] = after_signing
        return SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        )

    reply_on_send(b, signed_response, signing=True)
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


def test_pending_shot_keeps_lock_after_timeout(tmp_path: Any) -> None:
    """A pending nonce cannot prove that the signed Safe call has finished."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._rpc = MagicMock(side_effect=[done(None), done("0x7")])
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
    assert b.context.shared_state.get("lightning_lane_inflight") is info
    b._rpc.assert_any_call("eth_getTransactionCount", [OWNER, "latest"])
    assert "status=pending" in str(b.context.logger.info.call_args)


def test_consumed_nonce_rechecks_originating_receipt(tmp_path: Any) -> None:
    """Record a shot mined between the first receipt and latest nonce reads."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    b._rpc = MagicMock(side_effect=[done(None), done("0x8"), done(receipt)])
    b._complete = MagicMock()
    block = {"timestamp": hex(1060), "number": "0x2"}
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt(block))
    b._complete.assert_called_once_with(info, receipt, block)
    assert b._rpc.call_args_list[2].args == ("eth_getTransactionReceipt", [info["tx"]])


def test_pending_timeout_log_is_limited_to_once_per_minute(tmp_path: Any) -> None:
    """Keep watching each block while keeping the timeout log sparse."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._rpc = MagicMock(
        side_effect=lambda method, _params: done(
            None if method == "eth_getTransactionReceipt" else "0x7"
        )
    )
    for now in (1062, 1090, 1122):
        with patch(MODULE + ".time.time", return_value=now):
            run(b._watch_receipt({"timestamp": hex(int(now))}))
    pending = [
        call
        for call in b.context.logger.info.call_args_list
        if "status=pending" in call.args[0]
    ]
    assert len(pending) == 2
    assert b.context.shared_state.get("lightning_lane_inflight") is info


@pytest.mark.parametrize("signing", [False, True])
def test_silent_lane_wait_expires_on_scheduler_ticks(
    tmp_path: Any, signing: bool
) -> None:
    """End raw reads and signing when no message reaches the scheduler."""
    import datetime

    from packages.valory.skills.abstract_round_abci.behaviour_utils import (
        AsyncBehaviour,
    )
    from packages.valory.skills.fresh_watch_abci.lane_behaviour import LaneRPCError

    b = lane(tmp_path)
    prepared(b)
    b.setup = MagicMock()
    b.sleep = lambda delay: AsyncBehaviour.sleep(b, delay)
    b.context.requests.request_id_to_callback = {}
    dialogue = MagicMock()
    dialogue.dialogue_label.dialogue_reference = ("silent", "")
    b._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
    b.context.signing_dialogues.create.return_value = (MagicMock(), dialogue)
    results = []
    market = raw_market()
    market.update(id=MARKET, collateralToken=TOKEN)

    def act() -> Any:
        try:
            if signing:
                result = yield from b._send(
                    market,
                    SimpleNamespace(stake=4.0, price=0.6, p_hat=0.95),
                    (0.9, 4444444444444444445),
                    {"baseFeePerGas": "0x1", "number": "0x2"},
                    1002,
                )
            else:
                result = yield from b._rpc("eth_blockNumber", [])
        except LaneRPCError as exc:
            result = exc
        results.append(result)

    b.async_act_wrapper = act
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1)

    class Clock(datetime.datetime):
        """Advance local sleeps on ordinary scheduler ticks."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the current scheduler time."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
        patch(MODULE + ".time.time", return_value=1002),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.safe_contract_address = SAFE
        b.act()
        for tick in range(1, 20):
            seconds[0] = tick * 0.2
            b.act()
            if results:
                break
    assert results, "The scheduler must expire a wait without a response"
    assert results == [False] if signing else isinstance(results[0], LaneRPCError)
    assert "lightning_lane_inflight" not in b.context.shared_state
    callback = b.context.requests.request_id_to_callback.pop("silent")
    callback(None, b)


@pytest.mark.parametrize("transport", ["http", "ledger"])
def test_nonce_error_reconciles_the_originating_shot(
    tmp_path: Any, transport: str
) -> None:
    """A duplicate send error must still record a purchase mined by either path."""
    from packages.valory.protocols.ledger_api import LedgerApiMessage

    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    b.context.requests.request_id_to_callback = {}
    b._build_http_request_message = MagicMock(return_value=("http", MagicMock()))
    b.context.ledger_api_dialogues.create.return_value = ("ledger", MagicMock())
    b._get_request_nonce_from_dialogue = MagicMock(side_effect=["http", "ledger"])
    b._broadcast(SimpleNamespace(signed_transaction="signed"), "0xabcdef")
    callback = b.context.requests.request_id_to_callback[transport]
    if transport == "http":
        response = SimpleNamespace(body=b'{"error":{"message":"nonce too low"}}')
    else:
        response = SimpleNamespace(
            performative=LedgerApiMessage.Performative.ERROR, message="nonce too low"
        )
    callback(response, None)
    assert b.context.shared_state.get("lightning_lane_inflight") is info
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    b._rpc = MagicMock(
        side_effect=[done(receipt), done({"timestamp": "0x3ed", "number": "0x2"})]
    )
    with patch(MODULE + ".time.time", return_value=1005):
        run(b.async_act())
    assert b.durable_trade_count() == 1
    assert b.read_counted_placement_keys() == {"omen:0xfeed"}
    assert b._rpc.call_args_list[0].args == ("eth_getTransactionReceipt", ["0xfeed"])


@pytest.mark.parametrize("transport", ["http", "ledger"])
def test_old_broadcast_callback_cannot_change_a_new_shot(
    tmp_path: Any, transport: str
) -> None:
    """A late error for shot A must not release shot B's nonce lock."""
    from packages.valory.protocols.ledger_api import LedgerApiMessage

    b = lane(tmp_path)
    old = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = old
    b.context.requests.request_id_to_callback = {}
    b._build_http_request_message = MagicMock(return_value=("http", MagicMock()))
    b.context.ledger_api_dialogues.create.return_value = ("ledger", MagicMock())
    b._get_request_nonce_from_dialogue = MagicMock(side_effect=["http", "ledger"])
    b._broadcast(SimpleNamespace(signed_transaction="signed"), "0xabcdef")
    response = SimpleNamespace(
        body=b'{"error":{"message":"nonce too low"}}',
        performative=LedgerApiMessage.Performative.ERROR,
        message="nonce too low",
    )
    b.context.requests.request_id_to_callback[transport](response, None)
    assert old.get("reconcile") is True
    new = dict(old, tx="0xnew", eoa_nonce=8)
    new.pop("reconcile")
    b.context.shared_state["lightning_lane_inflight"] = new
    b.context.shared_state["lightning_lane_eoa_nonce"] = {"nonce": 9, "read_at": 1005}
    b.context.requests.request_id_to_callback[transport](response, None)
    assert b.context.shared_state.get("lightning_lane_inflight") == new
    assert b.context.shared_state.get("lightning_lane_eoa_nonce") == {
        "nonce": 9,
        "read_at": 1005,
    }
    assert "reconcile" not in new


def test_lane_completion_merges_fresh_bets_at_the_store(tmp_path: Any) -> None:
    """Keep another stored market when settlement updates its strategy data."""
    from packages.valory.skills.market_manager_abci.bets import Bet

    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    writer = lane(tmp_path)
    original = b._update_bet_strategy

    def update_strategy(bet: Any) -> None:
        original(bet)
        writer.read_bets()
        row = {
            key: value
            for key, value in raw_market().items()
            if key not in {"condition", "question"}
        }
        row.update(id="another-market", collateralToken=TOKEN)
        row["condition_id"] = "condition"
        other = Bet(**row, market="omen_subgraph")
        other.set_investment_amount(1, 2 * 10**18)
        writer.bets.append(other)
        writer.store_bets()

    b._update_bet_strategy = update_strategy
    b._complete(info, {"status": "0x1", "logs": []}, {"timestamp": hex(1005)})
    b.read_bets()
    by_id = {bet.id: bet for bet in b.bets}
    assert set(by_id) == {MARKET, "another-market"}
    assert by_id["another-market"].invested_amount == 2 * 10**18
    assert by_id[MARKET].invested_amount == 4 * 10**18


def test_old_receipt_recheck_cannot_release_a_new_shot(tmp_path: Any) -> None:
    """A delayed replacement check must still own the shot it clears."""
    b = lane(tmp_path)
    old = settled(b, tmp_path)
    newer = dict(old, tx="0xnew", eoa_nonce=8)
    b.context.shared_state["lightning_lane_inflight"] = old
    calls = []

    def rpc(method: str, params: Any) -> Any:
        calls.append(method)
        if len(calls) == 3:
            b.context.shared_state["lightning_lane_inflight"] = newer
        return done("0x8" if method == "eth_getTransactionCount" else None)

    b._rpc = rpc
    with patch(MODULE + ".time.time", return_value=1062):
        run(b._watch_receipt({"timestamp": hex(1060)}))
    assert len(calls) == 3
    assert b.context.shared_state.get("lightning_lane_inflight") is newer


@pytest.mark.parametrize("failure", ["counter", "activity"])
def test_completion_persistence_retries_on_next_tick(
    tmp_path: Any, failure: str
) -> None:
    """Keep recovery data and retry a failed durable write without a new block."""
    from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
        record_trade,
    )

    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info["reconcile"] = True
    b.context.shared_state["lightning_lane_inflight"] = info
    b._persist()
    b.setup = MagicMock()
    b.async_act_wrapper = b.async_act
    b._checked = b._safe_ok = True
    b._poller = MagicMock()
    b._poller.request.return_value = None
    b._poller.next_at = 1006
    b._refresh_idle = MagicMock(return_value=done(None))
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    block = {"timestamp": hex(1005), "number": "0x2"}
    b._rpc = MagicMock(side_effect=[done(receipt), done(block)])
    original_count = b.record_successful_placement
    failed: list[bool] = []

    def count(key: str) -> Any:
        if failure == "counter" and not failed:
            failed.append(True)
            return None
        return original_count(key)

    def activity(*args: Any) -> None:
        if failure == "activity" and not failed:
            failed.append(True)
            raise OSError("disk unavailable")
        record_trade(*args)

    b.record_successful_placement = count
    with (
        patch(MODULE + ".record_trade", side_effect=activity),
        patch(MODULE + ".time.time", return_value=1005),
    ):
        b.act()
        assert b.context.shared_state.get("lightning_lane_inflight") is info
        saved = json.loads((tmp_path / "lightning_lane_state.json").read_text())
        assert saved["inflight"]["receipt"] == receipt
        assert saved["inflight"]["receipt_block"] == block
        b.act()
    assert b.durable_trade_count() == 1
    assert b.read_counted_placement_keys() == {"omen:0xfeed"}
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert (
        json.loads((tmp_path / "lightning_lane_state.json").read_text())["inflight"]
        is None
    )
    b.read_bets()
    assert b.bets[0].invested_amount == 4 * 10**18
    trades = json.loads((tmp_path / "activity_goal_trades.json").read_text())
    assert len(trades) == 1
    assert trades[0]["placement_key"] == "omen:0xfeed"
    assert b._rpc.call_count == 2
