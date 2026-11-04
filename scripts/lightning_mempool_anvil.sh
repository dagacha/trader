#!/usr/bin/env bash
# Copyright 2026 Valory AG
# Licensed under the Apache License, Version 2.0.
# Run the call-order proof on a local chain. No fork or key is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/lightning-mempool.XXXXXX")
anvil_pid=''
cleanup() {
    if [[ -n "$anvil_pid" ]]; then kill "$anvil_pid" 2>/dev/null || true; fi
    rm -rf "$work_dir"
}
trap cleanup EXIT
npm_config_cache="$work_dir/npm" npx --yes --package solc@0.8.30 solcjs \
    --bin --abi --optimize -o "$work_dir/contracts" scripts/fixtures/lightning_mempool.sol
/opt/homebrew/bin/anvil --silent --no-mining --order fifo --chain-id 100 \
    --port 18546 >"$work_dir/anvil.log" 2>&1 &
anvil_pid=$!
export LIGHTNING_ANVIL_ARTIFACTS="$work_dir/contracts"
PYTHONPATH=. .venv/bin/python - <<'PY'
"""Prove Safe MultiSend call order on a disposable local chain."""
import json
import os
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD

from packages.valory.skills.fresh_watch_abci.lane import build_safe_calldata, buy_result

w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:18546"))
for _ in range(100):
    if w3.is_connected():
        break
    time.sleep(0.05)
assert w3.is_connected(), "Local Anvil did not start"
assert w3.eth.chain_id == 100
funder = w3.eth.accounts[0]
signer = Account.create()
zero = "0x" + "00" * 20


def rpc(method, params):
    result = w3.provider.make_request(method, params)
    assert "error" not in result, result
    return result.get("result")


def mined(tx):
    receipt = w3.eth.wait_for_transaction_receipt(tx)
    assert receipt.status == 1
    return receipt


def deploy(abi, bytecode, args=()):
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    receipt = mined(contract.constructor(*args).transact({"from": funder, "gas": 6000000}))
    return w3.eth.contract(address=receipt.contractAddress, abi=abi)


def artifact(name):
    return json.loads(Path("packages/valory/contracts", name).read_text())


def small(name):
    prefix = Path(os.environ["LIGHTNING_ANVIL_ARTIFACTS"],
                  "scripts_fixtures_lightning_mempool_sol_" + name)
    return deploy(json.loads(prefix.with_suffix(".abi").read_text()),
                  prefix.with_suffix(".bin").read_text())


rpc("evm_setAutomine", [True])
mined(w3.eth.send_transaction({"from": funder, "to": signer.address, "value": 100 * 10**18}))
safe_artifact = artifact("gnosis_safe/build/GnosisSafe_V1_3_0.json")
singleton = deploy(safe_artifact["abi"], safe_artifact["bytecode"], (bytes(32),))
proxy_artifact = artifact("gnosis_safe_proxy_factory/build/ProxyFactory_V1_3_0.json")
proxy_factory = deploy(proxy_artifact["abi"], proxy_artifact["bytecode"])
initializer = singleton.functions.setup(
    [signer.address], 1, zero, b"", zero, zero, 0, zero
)._encode_transaction_data()
receipt = mined(proxy_factory.functions.createProxyWithNonce(
    singleton.address, bytes.fromhex(initializer[2:]), 1
).transact({"from": funder, "gas": 500000}))
safe = proxy_factory.events.ProxyCreation().process_receipt(receipt, errors=DISCARD)[0]["args"]["proxy"]
multisend_artifact = artifact("multisend/build/MultiSend.json")
multisend = deploy(multisend_artifact["abi"], multisend_artifact["bytecode"])
collateral = small("Collateral")
factory = small("Factory")
mined(w3.eth.send_transaction({"from": funder, "to": safe, "value": 20 * 10**18}))
rpc("evm_setAutomine", [False])
amount = 10**18
fees = {"maxPriorityFeePerGas": 10**9, "maxFeePerGas": 2 * 10**9}
results = {}

for sequence, name in enumerate(("after_creation", "before_creation", "wrong_address", "dropped_creation", "ceiling_revert"), 1):
    salt = sequence.to_bytes(32, "big")
    predicted = factory.functions.predict(salt, collateral.address).call()
    target = predicted
    if name == "wrong_address":
        target = factory.functions.predict((sequence + 100).to_bytes(32, "big"), collateral.address).call()
    assert len(w3.eth.get_code(target)) == 0
    balance = collateral.functions.balanceOf(safe).call()
    minimum = amount * (3 if name == "ceiling_revert" else 1)
    data, _ = build_safe_calldata(signer.address, multisend.address, collateral.address,
                                  target, amount, minimum, balance, w3.eth.get_balance(safe))
    unsigned = dict(type=2, chainId=100, nonce=w3.eth.get_transaction_count(signer.address),
                    gas=400000, to=safe, value=0, data=data, **fees)
    signed = signer.sign_transaction(unsigned)

    def creation():
        return factory.functions.create(salt, collateral.address).transact(
            {"from": funder, "gas": 1000000, **fees})

    creation_tx = None
    if name == "before_creation":
        shot_tx = w3.eth.send_raw_transaction(signed.raw_transaction)
        creation_tx = creation()
    else:
        creation_tx = creation()
        if name == "dropped_creation":
            rpc("anvil_dropTransaction", [Web3.to_hex(creation_tx)])
            creation_tx = None
        shot_tx = w3.eth.send_raw_transaction(signed.raw_transaction)
    rpc("evm_mine", [])
    shot = w3.eth.get_transaction_receipt(shot_tx)
    created = w3.eth.get_transaction_receipt(creation_tx) if creation_tx else None
    logs = json.loads(Web3.to_json(shot))["logs"]
    fill = buy_result(logs, target)
    actual_balance = collateral.functions.balanceOf(safe).call()
    allowance = collateral.functions.allowance(safe, target).call()
    if name == "after_creation":
        assert shot.status == 1 and fill and fill["buyer"] == safe.lower()
        assert created.blockNumber == shot.blockNumber
        assert shot.transactionIndex == created.transactionIndex + 1
        assert allowance == 0
    elif name == "ceiling_revert":
        assert shot.status == 0 and fill is None
        assert actual_balance == balance
        assert allowance == 0
    else:
        # A CALL to empty code succeeds. Only the buy event proves a fill.
        assert shot.status == 1 and fill is None
        assert allowance == amount
        assert actual_balance >= balance
        if name == "before_creation":
            assert shot.blockNumber == created.blockNumber
            assert shot.transactionIndex < created.transactionIndex
        if name == "dropped_creation":
            assert len(w3.eth.get_code(predicted)) == 0
    results[name] = dict(status=shot.status, result="mined" if fill else "reverted" if not shot.status else "no_fill",
                         gas_used=shot.gasUsed, buy_events=int(fill is not None),
                         index=shot.transactionIndex, creation_index=created.transactionIndex if created else None,
                         block=shot.blockNumber, allowance=allowance)
print(json.dumps(results, indent=2))
PY
