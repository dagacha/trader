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

"""Check pending creation calls against historical Gnosis transactions."""

import dataclasses
import importlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from eth_abi import decode, encode
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.lightning import (
    LightningConfig,
    still_hold_rule,
)
from packages.valory.skills.market_manager_abci.bets import Bet

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "create2_fixture.json").read_text())
HISTORY = json.loads((FIXTURES / "create2_transactions.json").read_text())
TRANSACTIONS = HISTORY["transactions"]
FACTORY = LightningConfig().omen_fpmm_factory
MULTISEND = "0xa238cbeb142c10ef7ad8442c6d1f9e89e07e7761"
SAFE_TYPES = [
    "address",
    "uint256",
    "bytes",
    "uint8",
    "uint256",
    "uint256",
    "uint256",
    "address",
    "address",
    "bytes",
]
FACTORY_TYPES = [
    "uint256",
    "address",
    "address",
    "bytes32[]",
    "uint256",
    "uint256",
    "uint256[]",
]
QUESTION_TYPES = ["uint256", "string", "address", "uint32", "uint32", "uint256"]
SEEN = 1793665800.125


def subject() -> Any:
    """Load the pure decoder in each test, including the red run."""
    return importlib.import_module("packages.valory.skills.fresh_watch_abci.mempool")


def unpack(tx: Dict[str, Any]) -> tuple[List[Any], List[List[Any]]]:
    """Read entries with an independent fixture encoder."""
    outer = list(decode(SAFE_TYPES, bytes.fromhex(tx["input"][10:])))
    packed = decode(["bytes"], outer[2][4:])[0]
    entries = []
    while packed:
        length = int.from_bytes(packed[53:85], "big")
        entries.append(
            [
                packed[0],
                "0x" + packed[1:21].hex(),
                int.from_bytes(packed[21:53], "big"),
                packed[85 : 85 + length],
            ]
        )
        packed = packed[85 + length :]
    return outer, entries


def repack(
    tx: Dict[str, Any], outer: List[Any], entries: List[List[Any]]
) -> Dict[str, Any]:
    """Encode one changed historical transaction."""
    packed = b"".join(
        bytes([operation])
        + bytes.fromhex(target[2:])
        + value.to_bytes(32, "big")
        + len(data).to_bytes(32, "big")
        + data
        for operation, target, value, data in entries
    )
    outer[2] = bytes.fromhex("8d80ff0a") + encode(["bytes"], [packed])
    return dict(tx, input="0x6a761202" + encode(SAFE_TYPES, outer).hex())


def valid_tx(**changes: Any) -> Dict[str, Any]:
    """Use an explicit equal hint for strict pending qualification."""
    tx = TRANSACTIONS[0].copy()
    outer, entries = unpack(tx)
    values = list(decode(FACTORY_TYPES, entries[3][3][4:]))
    values[6] = [1, 1]
    entries[3][3] = bytes.fromhex("82e90fa0") + encode(FACTORY_TYPES, values)
    return dict(repack(tx, outer, entries), **changes)


@pytest.mark.parametrize("row,tx", list(zip(ROWS, TRANSACTIONS)))
def test_decode_historical_calldata(row: Dict[str, Any], tx: Dict[str, Any]) -> None:
    """Decode all 108 historical Safe, MultiSend, factory and question calls."""
    result = subject().decode_calls(tx, MULTISEND, FACTORY)
    assert result is not None
    assert result.salt_nonce == row["salt_nonce"]
    assert result.question == row["question"]
    assert result.template_id == row["template_id"]
    assert result.opening_ts == row["opening_ts"]
    assert result.fee == row["fee_wei"]
    assert result.initial_funds == row["initial_funds_wei"]
    assert result.creator == row["creator_safe"]
    assert result.distribution_hint == ()


@pytest.mark.parametrize("row,tx", list(zip(ROWS, TRANSACTIONS)))
def test_create2_historical_addresses(row: Dict[str, Any], tx: Dict[str, Any]) -> None:
    """Prove the source-derived CREATE2 formula on all 108 real addresses."""
    module = subject()
    call = module.decode_calls(tx, MULTISEND, FACTORY)
    assert module.predict_market(call, FACTORY) == row["market"]


def test_still_hold_fixture_count() -> None:
    """Keep the existing phrase rule and report the actual final-night count."""
    module = subject()
    calls = [module.decode_calls(tx, MULTISEND, FACTORY) for tx in TRANSACTIONS[-36:]]
    assert len(calls) == 36
    assert (
        sum(
            bool(still_hold_rule(call.question, call.creator, LightningConfig()))
            for call in calls
        )
        == 30
    )


def test_pending_row_and_fees_are_complete() -> None:
    """Build a real Bet row and preserve both creator fee fields."""
    module = subject()
    candidate = module.decode_pending(valid_tx(), LightningConfig(), MULTISEND, SEEN)
    assert candidate is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.seen_at = 0
    assert candidate.seen_at == SEEN
    assert candidate.creation_tx == ROWS[0]["creation_tx"]
    assert candidate.priority_fee == int(TRANSACTIONS[0]["maxPriorityFeePerGas"], 16)
    assert candidate.max_fee == int(TRANSACTIONS[0]["maxFeePerGas"], 16)
    assert candidate.salt_nonce == ROWS[0]["salt_nonce"]
    assert candidate.initial_funds == ROWS[0]["initial_funds_wei"]
    row = candidate.market.copy()
    assert row["creator"] == ROWS[0]["creator_safe"]
    assert (
        row["condition"]["id"]
        == "0x7bd4b7ef84ce80771e644c569f1a7d8141182ad371d05b8d1a19596963967bc1"
    )
    assert row["outcomeTokenAmounts"] == [7 * 10**18] * 2
    assert row["outcomeTokenMarginalPrices"] == [0.5, 0.5]
    assert row["scaledLiquidityMeasure"] == 7
    assert row["fee"] == ROWS[0]["fee_wei"]
    assert row["creationTimestamp"] == int(SEEN)
    condition = row.pop("condition")
    row.pop("conditional_tokens")
    row.pop("question")
    bet = Bet(**row, condition_id=condition["id"], market="omen_subgraph")
    assert bet.id == ROWS[0]["market"]
    assert bet.title == ROWS[0]["question"].split("␟")[0]
    assert bet.outcomes == ["Yes", "No"]


@pytest.mark.parametrize(
    "change",
    [
        {"from": "0x" + "11" * 20},
        {"to": "0x" + "11" * 20},
        {"to": ROWS[1]["creator_safe"]},
        {"input": "0x12345678"},
        {"input": "0x6a761202"},
        {"input": "not hex"},
        {"input": None},
        {"from": None},
        {"to": None},
        {"type": "0x1"},
        {"type": None},
        {"maxPriorityFeePerGas": None},
        {"maxFeePerGas": None},
        {"maxPriorityFeePerGas": "0x3b9aca19"},
        {"maxPriorityFeePerGas": -1},
        {"maxFeePerGas": -1},
        {"maxFeePerGas": float("inf")},
        {"maxPriorityFeePerGas": float("inf")},
        {"type": float("inf")},
        {"hash": None},
    ],
)
def test_invalid_pending_transaction_is_ignored(change: Dict[str, Any]) -> None:
    """Ignore unsupported transactions without raising in the websocket thread."""
    assert (
        subject().decode_pending(valid_tx(**change), LightningConfig(), MULTISEND, SEEN)
        is None
    )


@pytest.mark.parametrize(
    "field",
    ["type", "maxPriorityFeePerGas", "maxFeePerGas", "hash", "input", "from", "to"],
)
def test_missing_pending_field_is_ignored(field: str) -> None:
    """Require complete EIP-1559 and creation data."""
    tx = valid_tx()
    tx.pop(field)
    assert subject().decode_pending(tx, LightningConfig(), MULTISEND, SEEN) is None


@pytest.mark.parametrize(
    "field,index,value",
    [
        ("outer", 0, "0x" + "11" * 20),
        ("outer", 3, 0),
        ("factory", 2, "0x" + "11" * 20),
        ("factory", 3, []),
        ("factory", 3, [b"\x01" * 32, b"\x02" * 32]),
        ("factory", 5, 0),
        ("factory", 6, [2, 1]),
        ("factory", 6, []),
        ("question", 0, 0),
        ("question", 1, 'Will it still hold?␟"No","Yes"'),
        ("question", 1, 'Will it still hold?␟"Yes","No","Maybe"'),
        ("question", 1, "Will it still hold?"),
        ("question", 1, 'Bad "quote␟"Yes","No"'),
    ],
)
def test_strict_creation_fields(field: str, index: int, value: Any) -> None:
    """Require the exact supported Safe, market and question forms."""
    tx = valid_tx()
    outer, entries = unpack(tx)
    if field == "outer":
        outer[index] = value
    else:
        entry = entries[3 if field == "factory" else 1]
        types = FACTORY_TYPES if field == "factory" else QUESTION_TYPES
        values = list(decode(types, entry[3][4:]))
        values[index] = value
        entry[3] = entry[3][:4] + encode(types, values)
    assert (
        subject().decode_pending(
            repack(tx, outer, entries), LightningConfig(), MULTISEND, SEEN
        )
        is None
    )


@pytest.mark.parametrize(
    "mode",
    [
        "factory_target",
        "factory_operation",
        "question_operation",
        "missing_question",
        "missing_factory",
        "duplicate_question",
        "duplicate_factory",
        "bad_multisend",
        "short_header",
        "short_data",
    ],
)
def test_invalid_multisend_is_ignored(mode: str) -> None:
    """Reject wrong targets, duplicate entries and truncated packed calls."""
    tx = valid_tx()
    outer, entries = unpack(tx)
    if mode == "factory_target":
        entries[3][1] = "0x" + "11" * 20
    elif mode == "factory_operation":
        entries[3][0] = 1
    elif mode == "question_operation":
        entries[1][0] = 1
    elif mode == "missing_question":
        entries.pop(1)
    elif mode == "missing_factory":
        entries.pop(3)
    elif mode == "duplicate_question":
        entries.append(entries[1])
    elif mode == "duplicate_factory":
        entries.append(entries[3])
    tx = repack(tx, outer, entries)
    if mode in {"bad_multisend", "short_header", "short_data"}:
        outer = list(decode(SAFE_TYPES, bytes.fromhex(tx["input"][10:])))
        if mode == "bad_multisend":
            outer[2] = b"\xff" * 4
        else:
            packed = decode(["bytes"], outer[2][4:])[0]
            packed = packed[:40] if mode == "short_header" else packed[:-1]
            outer[2] = bytes.fromhex("8d80ff0a") + encode(["bytes"], [packed])
        tx["input"] = "0x6a761202" + encode(SAFE_TYPES, outer).hex()
    assert subject().decode_pending(tx, LightningConfig(), MULTISEND, SEEN) is None


def test_configured_factory_and_creator_are_required() -> None:
    """Read the configured factory and Safe allowlist for each pending call."""
    module = subject()
    tx = valid_tx()
    assert (
        module.decode_pending(
            tx,
            LightningConfig(lightning_creators=[ROWS[1]["creator_safe"]]),
            MULTISEND,
            SEEN,
        )
        is None
    )
    assert (
        module.decode_pending(
            tx, LightningConfig(omen_fpmm_factory="0x" + "11" * 20), MULTISEND, SEEN
        )
        is None
    )


@pytest.mark.parametrize(
    "template,question,expected",
    [
        (2, r'Will \"X\" still hold?␟"Yes","No"', 'Will "X" still hold?'),
        (
            2,
            r'Will it st\u0069ll hold?\x1f"Yes","No"'.replace(r"\x1f", "\x1f"),
            "Will it still hold?",
        ),
        (0, 'Will it still hold?␟"Yes","No"', {}),
        (2, 'Will it still hold?␟"No","Yes"', {}),
        (2, "Will it still hold?", None),
    ],
)
def test_question_metadata(template: int, question: str, expected: Any) -> None:
    """Use the receipt parser for JSON escapes, template and outcome order."""
    result = subject().question_metadata(template, question, 123)
    if isinstance(expected, str):
        assert result == dict(
            title=expected,
            openingTimestamp=123,
            outcomes=["Yes", "No"],
            outcomeSlotCount=2,
            question={"templateId": 2},
        )
    else:
        assert result == expected


def test_integer_fees_and_case_normalization() -> None:
    """Accept RPC numbers and checksum addresses without changing fees."""
    tx = valid_tx(type=2, maxPriorityFeePerGas=123, maxFeePerGas=456)
    tx["from"] = tx["from"].upper().replace("0X", "0x")
    tx["to"] = tx["to"].upper().replace("0X", "0x")
    candidate = subject().decode_pending(tx, LightningConfig(), MULTISEND.upper(), SEEN)
    assert candidate is not None
    assert (candidate.priority_fee, candidate.max_fee) == (123, 456)


def test_create2_uses_every_constructor_input() -> None:
    """Keep the source formula sensitive to constructor data and caller."""
    module = subject()
    call = module.decode_calls(TRANSACTIONS[0], MULTISEND, FACTORY)
    original = module.predict_market(call, FACTORY)
    for change in [
        dict(creator=ROWS[1]["creator_safe"]),
        dict(salt_nonce=1),
        dict(fee=0),
        dict(conditional_tokens="0x" + "11" * 20),
        dict(collateral_token="0x" + "11" * 20),
        dict(condition_ids=(keccak(text="other"),)),
    ]:
        assert (
            module.predict_market(dataclasses.replace(call, **change), FACTORY)
            != original
        )
    assert module.predict_market(call, "0x" + "11" * 20) != original
