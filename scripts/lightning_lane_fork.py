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

"""Measure real Safe lane buys on a local Anvil Gnosis fork.

Start Anvil with:
    anvil --fork-url "$GNOSIS_FORK_RPC_URL" --fork-block-number 48997577 \
        --chain-id 100 --port 18545
Run with:
    PYTHONPATH=. .venv/bin/python scripts/lightning_lane_fork.py

All writes go to localhost. No production keys or Safe are used.
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD

from packages.valory.skills.fresh_watch_abci.lane import (
    build_safe_calldata,
    fixed_transaction,
)

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x" + "00" * 20
MARKET = "0xf46f0b6e9e96b466a3563d025577e65648c7daa7"
COLLATERAL = "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"
MULTISEND = "0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761"
FACTORY = "0xa6B71E26C5e0845f74c812102Ca7114b6a896AB2"


def artifact(relative: str) -> Dict[str, Any]:
    """Read an existing compiled contract artifact.

    :param relative: path within the contracts directory.
    :return: contract artifact.
    """
    return json.loads((ROOT / "packages/valory/contracts" / relative).read_text())


def run(endpoint: str) -> Dict[str, Any]:
    """Place a buy and a forced ceiling revert on a disposable fork.

    :param endpoint: local Anvil endpoint.
    :return: measured receipts and balance checks.
    :raises ValueError: if the endpoint is not local or the chain is not Gnosis.
    """
    if urlparse(endpoint).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use a local Anvil endpoint.")
    web3 = Web3(Web3.HTTPProvider(endpoint, request_kwargs={"timeout": 60}))
    if web3.eth.chain_id != 100:
        raise ValueError("Fork Gnosis with chain ID 100.")
    funder = web3.eth.accounts[0]
    signer = Account.create()
    owner = signer.address
    web3.eth.wait_for_transaction_receipt(
        web3.eth.send_transaction({"from": funder, "to": owner, "value": 100 * 10**18})
    )
    safe_artifact = artifact("gnosis_safe/build/GnosisSafe_V1_3_0.json")
    singleton = web3.eth.contract(
        abi=safe_artifact["abi"], bytecode=safe_artifact["bytecode"]
    )
    deployed = web3.eth.wait_for_transaction_receipt(
        singleton.constructor(bytes(32)).transact({"from": funder, "gas": 6000000})
    )
    assert deployed.status == 1
    singleton = web3.eth.contract(
        address=deployed.contractAddress, abi=safe_artifact["abi"]
    )
    initializer = singleton.functions.setup(
        [owner],
        1,
        ZERO,
        b"",
        "0xf48f2B2d2a534e402487b3ee7C18c33Aec0Fe5e4",
        ZERO,
        0,
        ZERO,
    )._encode_transaction_data()
    factory = web3.eth.contract(
        address=FACTORY,
        abi=artifact("gnosis_safe_proxy_factory/build/ProxyFactory_V1_3_0.json")["abi"],
    )
    created = web3.eth.wait_for_transaction_receipt(
        factory.functions.createProxyWithNonce(
            singleton.address, bytes.fromhex(initializer[2:]), web3.eth.block_number
        ).transact({"from": funder, "gas": 500000})
    )
    assert created.status == 1
    safe = factory.events.ProxyCreation().process_receipt(created, errors=DISCARD)[0][
        "args"
    ]["proxy"]
    safe_contract = web3.eth.contract(address=safe, abi=safe_artifact["abi"])
    assert safe_contract.functions.getThreshold().call() == 1
    assert safe_contract.functions.isOwner(owner).call()
    # Seed xDAI only. The mined lane call must wrap its exact shortfall.
    web3.eth.wait_for_transaction_receipt(
        web3.eth.send_transaction({"from": funder, "to": safe, "value": 20 * 10**18})
    )
    fpmm = web3.eth.contract(
        address=Web3.to_checksum_address(MARKET),
        abi=artifact("market_maker/build/FixedProductMarketMaker.json")["abi"],
    )
    erc20_abi = [
        {
            "type": "function",
            "name": "balanceOf",
            "stateMutability": "view",
            "inputs": [{"type": "address", "name": "account"}],
            "outputs": [{"type": "uint256", "name": ""}],
        },
        {
            "type": "function",
            "name": "allowance",
            "stateMutability": "view",
            "inputs": [
                {"type": "address", "name": "owner"},
                {"type": "address", "name": "spender"},
            ],
            "outputs": [{"type": "uint256", "name": ""}],
        },
    ]
    wrapped = web3.eth.contract(address=COLLATERAL, abi=erc20_abi)
    amount = 4 * 10**18
    quoted = fpmm.functions.calcBuyAmount(amount, 0).call()
    assert quoted > amount
    minimum = (amount * 10 + 7 - 1) // 7  # ceil(amount / 0.7)
    assert quoted >= minimum
    result: Dict[str, Any] = {"fork_block": 48997577, "market": MARKET, "safe": safe}
    for label, min_out in (("mined", minimum), ("reverted", 10**30)):
        before_wrapped = wrapped.functions.balanceOf(safe).call()
        before_native = web3.eth.get_balance(safe)
        before_allowance = wrapped.functions.allowance(safe, fpmm.address).call()
        data, shortfall = build_safe_calldata(
            owner,
            MULTISEND,
            COLLATERAL,
            MARKET,
            amount,
            min_out,
            before_wrapped,
            before_native,
        )
        tx = fixed_transaction(
            owner,
            safe,
            data,
            web3.eth.get_transaction_count(owner, "pending"),
            web3.eth.get_block("latest")["baseFeePerGas"],
            400000,
            550,
            web3.eth.get_balance(owner),
        )
        # Sign with a disposable EOA created only for this fork.
        signed = signer.sign_transaction(tx)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        assert receipt.status == (1 if label == "mined" else 0)
        if label == "mined":
            events = fpmm.events.FPMMBuy().process_receipt(receipt, errors=DISCARD)
            assert len(events) == 1
            assert events[0]["args"]["outcomeIndex"] == 0
            assert events[0]["args"]["investmentAmount"] == amount
            assert events[0]["args"]["outcomeTokensBought"] >= minimum
            assert wrapped.functions.allowance(safe, fpmm.address).call() == 0
        else:
            assert wrapped.functions.balanceOf(safe).call() == before_wrapped
            assert web3.eth.get_balance(safe) == before_native
            assert (
                wrapped.functions.allowance(safe, fpmm.address).call()
                == before_allowance
            )
        result[label] = {
            "tx": tx_hash.hex(),
            "status": receipt.status,
            "gas_used": receipt.gasUsed,
            "effective_gas_price": receipt.effectiveGasPrice,
            "fee_xdai": str(
                Web3.from_wei(receipt.gasUsed * receipt.effectiveGasPrice, "ether")
            ),
            "wrapped_shortfall": shortfall,
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default="http://127.0.0.1:18545")
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.rpc), indent=2))
