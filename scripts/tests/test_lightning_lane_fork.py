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

"""Check fork measurement reports without a live Gnosis fork."""

import json
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3

# isort: split
from scripts import lightning_lane_fork as fork


@pytest.mark.parametrize("cli", [False, True])
def test_fork_reports_mined_and_reverted_shots(
    cli: bool, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report signed fork receipts and preserve balances after a revert."""
    safe = "0x" + "34" * 20
    funder = "0x" + "12" * 20
    deployment, singleton, factory, safe_contract, fpmm, wrapped = [
        MagicMock() for _ in range(6)
    ]
    singleton.address = "0x" + "56" * 20
    singleton.functions.setup.return_value = SimpleNamespace(
        _encode_transaction_data=lambda: "0x1234"
    )
    factory.events.ProxyCreation.return_value.process_receipt.return_value = [
        {"args": {"proxy": safe}}
    ]
    safe_contract.functions.getThreshold.return_value.call.return_value = 1
    safe_contract.functions.isOwner.return_value.call.return_value = True
    fpmm.address = Web3.to_checksum_address(fork.MARKET)
    fpmm.functions.calcBuyAmount.return_value.call.return_value = 6 * 10**18
    fpmm.events.FPMMBuy.return_value.process_receipt.return_value = [
        {
            "args": {
                "outcomeIndex": 0,
                "investmentAmount": 4 * 10**18,
                "outcomeTokensBought": 6 * 10**18,
            }
        }
    ]
    wrapped.functions.balanceOf.return_value.call.return_value = 0
    wrapped.functions.allowance.return_value.call.return_value = 0
    with patch.object(fork, "Web3") as web3_class:
        web3_class.to_checksum_address.side_effect = Web3.to_checksum_address
        web3_class.from_wei.side_effect = Web3.from_wei
        eth = web3_class.return_value.eth
        eth.chain_id, eth.accounts, eth.block_number = 100, [funder], 48997578
        eth.contract.side_effect = [
            deployment,
            singleton,
            factory,
            safe_contract,
            fpmm,
            wrapped,
        ]
        eth.get_balance.return_value = 20 * 10**18
        eth.get_transaction_count.side_effect = [0, 1]
        eth.get_block.return_value = {"baseFeePerGas": 1}
        hashes = [HexBytes("0x" + value * 32) for value in ("ab", "cd")]
        eth.send_raw_transaction.side_effect = hashes
        receipts = [
            SimpleNamespace(
                status=status, gasUsed=100000, effectiveGasPrice=550 * 10**9 + 2
            )
            for status in (1, 0)
        ]
        eth.wait_for_transaction_receipt.side_effect = [
            SimpleNamespace(status=1),
            SimpleNamespace(status=1, contractAddress=singleton.address),
            SimpleNamespace(status=1),
            SimpleNamespace(status=1),
            *receipts,
        ]
        if cli:
            monkeypatch.setattr(
                sys, "argv", [str(fork.__file__), "--rpc", "http://localhost:18545"]
            )
            with patch("web3.Web3", web3_class):
                runpy.run_path(str(fork.__file__), run_name="__main__")
            result = json.loads(capsys.readouterr().out)
        else:
            result = fork.run("http://localhost:18545")
        assert result == {
            "fork_block": 48997577,
            "market": fork.MARKET,
            "safe": safe,
            **{
                label: dict(
                    tx=tx_hash.hex(),
                    status=receipt.status,
                    gas_used=100000,
                    effective_gas_price=550 * 10**9 + 2,
                    fee_xdai="0.0550000000002",
                    wrapped_shortfall=4 * 10**18,
                )
                for label, tx_hash, receipt in zip(
                    ("mined", "reverted"), hashes, receipts
                )
            },
        }
        signed = [
            TypedTransaction.from_bytes(HexBytes(call.args[0])).as_dict()
            for call in eth.send_raw_transaction.call_args_list
        ]
        assert [tx["nonce"] for tx in signed] == [0, 1]
        assert all(tx["chainId"] == 100 and tx["to"].hex() == safe[2:] for tx in signed)
        assert signed[0]["data"] != signed[1]["data"]
        transfers = [call.args[0] for call in eth.send_transaction.call_args_list]
        assert [tx["value"] for tx in transfers] == [100 * 10**18, 20 * 10**18]
        assert transfers[1]["to"] == safe
        assert eth.get_transaction_count.call_count == 2


def test_fork_refuses_remote_endpoint() -> None:
    """Reject a remote endpoint before it can send a transaction."""
    with patch.object(fork, "Web3") as web3_class:
        with pytest.raises(ValueError, match="local Anvil"):
            fork.run("https://gnosis.example")
        web3_class.assert_not_called()


def test_fork_refuses_wrong_chain() -> None:
    """Reject another local chain before funding a disposable signer."""
    with patch.object(fork, "Web3") as web3_class:
        web3_class.return_value.eth.chain_id = 1
        with pytest.raises(ValueError, match="chain ID 100"):
            fork.run("http://127.0.0.1:18545")
        web3_class.return_value.eth.send_transaction.assert_not_called()
