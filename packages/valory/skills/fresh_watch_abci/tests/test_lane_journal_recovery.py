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

"""Keep Safe preparation blocked until a failed journal read is recovered."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from aea.helpers.transaction.base import SignedTransaction
from aea_ledger_ethereum.ethereum import SignedTransactionTranslator
from eth_account import Account

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MODULE,
    SAFE,
    done,
    lane,
    prepared,
    reply_on_send,
    run,
    settled,
)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("saved_receipt", [False, True])
def test_journal_read_failure_blocks_guard_until_reconciled(
    tmp_path: Any, enabled: bool, saved_receipt: bool
) -> None:
    """Retry journal reads and release Safe preparation after bookkeeping."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    block = {"timestamp": hex(1005), "number": "0x2"}
    if saved_receipt:
        info.update(receipt=receipt, receipt_block=block)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._night, b._shots = "1970-01-01", 3
    b._persist()
    journal = tmp_path / "lightning_lane_state.json"
    original = journal.read_bytes()
    read_text = Path.read_text
    failed: list[bool] = []

    def fail_journal(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == journal:
            failed.append(True)
            raise PermissionError("journal read unavailable")
        return read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", new=fail_journal):
        restarted = lane(tmp_path, lightning_lane=enabled)
        restarted.sleep = lambda _delay: iter([None])
        restarted._rpc = MagicMock(side_effect=AssertionError("unloaded journal"))
        waiting = restarted.wait_for_lightning_lane(reserve=True)
        assert next(waiting) is None
        assert "lightning_lane_fsm_preparing" not in restarted.context.shared_state
        run(restarted.async_act())
        run(restarted.async_act())
        assert failed == [True, True, True]
        assert next(waiting) is None
        assert journal.read_bytes() == original
        assert restarted.durable_trade_count() == 0
        restarted._rpc.assert_not_called()

    with patch(MODULE + ".time.time", return_value=1005):
        if not saved_receipt:
            restarted._rpc = MagicMock(return_value=done(None))
            run(restarted.async_act())
            assert restarted.context.shared_state["lightning_lane_inflight"] == info
            assert next(waiting) is None
            assert journal.read_bytes() == original
        restarted._rpc = MagicMock(side_effect=[done(receipt), done(block)])
        run(restarted.async_act())
        assert run(waiting) is None

    assert "lightning_lane_inflight" not in restarted.context.shared_state
    assert restarted.context.shared_state.get("lightning_lane_fsm_preparing") is (
        True if enabled else None
    )
    assert restarted._shots == 3
    assert restarted.durable_trade_count() == 1
    assert restarted.read_counted_placement_keys() == {"omen:0xfeed"}
    restarted.read_bets()
    assert restarted.bets[0].invested_amount == info["amount"]
    assert restarted._rpc.call_count == (0 if saved_receipt else 2)


@pytest.mark.parametrize("now", [1003, 1062])
def test_restart_after_signed_journal_write_rebroadcasts_once(
    tmp_path: Any, now: int
) -> None:
    """Recover a crash before broadcast, including a shot past its age limit."""
    b = lane(tmp_path)
    prepared(b)
    market = settled(b, tmp_path)["row"]
    signed_body = SignedTransactionTranslator.to_dict(
        Account.sign_transaction(
            {
                "type": 2,
                "chainId": 100,
                "nonce": 7,
                "gas": 400000,
                "maxFeePerGas": 550 * 10**9 + 2,
                "maxPriorityFeePerGas": 550 * 10**9,
                "to": SAFE,
                "value": 0,
            },
            "0x" + "01" * 32,
        )
    )
    raw = signed_body["raw_transaction"]
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction("ethereum", signed_body),
        ),
        signing=True,
    )
    b._broadcast = MagicMock(side_effect=RuntimeError("process stopped"))
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=1002),
        pytest.raises(RuntimeError, match="process stopped"),
    ):
        sd.return_value.safe_contract_address = SAFE
        run(
            b._send(
                market,
                SimpleNamespace(stake=4.0, price=0.6, p_hat=0.95),
                (0.9, 4444444444444444445),
                {"number": "0x2", "baseFeePerGas": "0x1", "gasLimit": "0x1000000"},
                1002,
            )
        )
    saved = json.loads((tmp_path / "lightning_lane_state.json").read_text())
    assert saved["inflight"]["signed_transaction"] == signed_body
    restarted = lane(tmp_path, lightning_lane=False)
    info = restarted.context.shared_state["lightning_lane_inflight"]
    restarted._rpc = MagicMock(side_effect=[done(None), done("0x7")])
    restarted._request = MagicMock(return_value=("http send", MagicMock()))
    restarted.context.ledger_api_dialogues.create.return_value = (
        "ledger send",
        MagicMock(),
    )
    restarted.context.requests.request_id_to_callback = {}
    restarted._get_request_nonce_from_dialogue = MagicMock(
        side_effect=["http", "ledger"]
    )
    with patch(MODULE + ".time.time", return_value=now):
        run(restarted.async_act())
        assert restarted.context.shared_state["lightning_lane_inflight"] is info
        restarted._request.assert_called_once_with("eth_sendRawTransaction", [raw])
        ledger = restarted.context.ledger_api_dialogues.create.call_args.kwargs
        restored_body = ledger["signed_transaction"].body
        assert (
            SignedTransactionTranslator.from_dict(
                restored_body
            ).raw_transaction.to_0x_hex()
            == raw
        )
        assert restored_body == signed_body
        assert ledger["signed_transaction"].body["raw_transaction"] == raw
        assert [
            call.kwargs["message"]
            for call in restarted.context.outbox.put_message.call_args_list
        ] == ["http send", "ledger send"]
        restarted._receive_send(
            SimpleNamespace(body=b'{"error":{"message":"already known"}}'), info["tx"]
        )
        restarted._receive_ledger_send(
            SimpleNamespace(
                performative=LedgerApiMessage.Performative.ERROR,
                message="already known",
            ),
            info["tx"],
        )
        restarted._rpc = MagicMock(
            side_effect=lambda method, _params: done(
                "0x7" if method == "eth_getTransactionCount" else None
            )
        )
        run(restarted.async_act())
        assert restarted.context.outbox.put_message.call_count == 2
        assert restarted._shots == 1
        assert restarted._stats.errors == 0
        assert restarted.durable_trade_count() == 0
        logs = str(restarted.context.logger.method_calls)
        assert ("status=late" in logs) is (now == 1062)
        assert "private-key" not in logs
        receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
        block = {"number": "0x2", "timestamp": hex(now)}
        restarted._rpc = MagicMock(side_effect=[done(receipt), done(block)])
        run(restarted.async_act())
        assert "lightning_lane_inflight" not in restarted.context.shared_state
        assert run(restarted.wait_for_lightning_lane(reserve=True)) is None
    assert restarted.durable_trade_count() == 1
    assert restarted.read_counted_placement_keys() == {"omen:" + info["tx"]}
    assert restarted.context.outbox.put_message.call_count == 2
    assert restarted._shots == 1


@pytest.mark.parametrize("nonce", [6, 7, 8])
@pytest.mark.parametrize("mined", [False, True])
def test_restored_raw_shot_checks_receipt_and_confirmed_nonce(
    tmp_path: Any, nonce: int, mined: bool
) -> None:
    """Submit saved bytes only for an absent receipt and an unchanged nonce."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info["signed_transaction"] = {"raw_transaction": "0xabcdef"}
    b.context.shared_state["lightning_lane_inflight"] = info
    b._persist()
    restarted = lane(tmp_path, lightning_lane=False)
    restarted._broadcast = MagicMock()
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    block = {"number": "0x2", "timestamp": hex(1062)}
    restarted._rpc = MagicMock(
        side_effect=lambda method, _params: done(
            (receipt if mined else None)
            if method == "eth_getTransactionReceipt"
            else block if method == "eth_getBlockByNumber" else hex(nonce)
        )
    )
    with patch(MODULE + ".time.time", return_value=1062):
        run(restarted.async_act())
    assert restarted._broadcast.call_count == int(not mined and nonce == 7)
    assert restarted.durable_trade_count() == int(mined)
    assert ("lightning_lane_inflight" in restarted.context.shared_state) is (
        not mined and nonce <= 7
    )
    if mined:
        restarted._rpc.assert_any_call("eth_getTransactionReceipt", [info["tx"]])
        assert all(
            call.args[0] != "eth_getTransactionCount"
            for call in restarted._rpc.call_args_list
        )
