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
from eth_abi.packed import encode_packed
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.lane import lane_decision
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
    still_hold_rule,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig
from packages.valory.skills.market_manager_abci.bets import Bet

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "create2_fixture.json").read_text())
HISTORY = json.loads((FIXTURES / "create2_transactions.json").read_text())
TRANSACTIONS = HISTORY["transactions"]
FACTORY = LightningConfig().omen_fpmm_factory
MULTISEND = "0xA238CBeb142c10Ef7Ad8442C6D1f9E89e07e7761"
CONDITIONAL_TOKENS = "0xCeAfDD6bc0bEF976fdCd1112955828E00543c0Ce"
REALITIO_PROXY = "0xAB16D643bA051C11962DA645f74632d3130c81E2"
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
PREPARE_TYPES = ["address", "bytes32", "uint256"]
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


def test_realitio_selector_matches_deployed_abi() -> None:
    """Use the six-argument askQuestion ABI from deployed Realitio v2.1."""
    module = subject()
    signature = "askQuestion(" + ",".join(module.QUESTION_TYPES) + ")"
    assert keccak(text=signature)[:4] == bytes.fromhex("762c38fd")


@pytest.mark.parametrize("change", ["oracle", "conditional_tokens", "prepare_target"])
def test_settlement_must_use_configured_contracts(change: str) -> None:
    """Reject other settlement contracts even with valid question and condition IDs."""
    tx = TRANSACTIONS[0]
    outer, entries = unpack(tx)
    preparation = list(decode(PREPARE_TYPES, entries[2][3][4:]))
    factory = list(decode(FACTORY_TYPES, entries[3][3][4:]))
    question_id = preparation[1]
    other = "0x" + "11" * 20
    if change == "oracle":
        preparation[0] = other
    elif change == "conditional_tokens":
        factory[1] = other
        entries[2][1] = other
    else:
        entries[2][1] = other
    factory[3] = [keccak(encode_packed(PREPARE_TYPES, preparation))]
    entries[2][3] = entries[2][3][:4] + encode(PREPARE_TYPES, preparation)
    entries[3][3] = entries[3][3][:4] + encode(FACTORY_TYPES, factory)
    changed = repack(tx, outer, entries)
    _, repacked = unpack(changed)
    actual = decode(PREPARE_TYPES, repacked[2][3][4:])
    assert actual[1] == question_id
    assert decode(FACTORY_TYPES, repacked[3][3][4:])[3] == (
        keccak(encode_packed(PREPARE_TYPES, actual)),
    )
    module = subject()
    assert (
        module.decode_calls(
            changed, MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY
        )
        is None
    )
    assert (
        module.decode_pending(
            changed,
            LightningConfig(),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
        )
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        "factory_condition",
        "prepare_question",
        "prepare_question_and_condition",
        "missing_prepare",
        "duplicate_prepare",
        "nonbinary_prepare",
        "prepare_operation",
        "prepare_target",
        "question_target",
        "question_arbitrator",
        "question_timeout",
        "question_opening",
        "question_nonce",
    ],
)
def test_question_and_pool_must_share_the_batch_condition(change: str) -> None:
    """Reject real calldata when the question and pool link is broken."""
    tx = TRANSACTIONS[0]
    outer, entries = unpack(tx)
    if change == "factory_condition":
        _, other = unpack(TRANSACTIONS[1])
        values = list(decode(FACTORY_TYPES, entries[3][3][4:]))
        other_values = decode(FACTORY_TYPES, other[3][3][4:])
        assert values[3] != other_values[3]
        assert decode(PREPARE_TYPES, other[2][3][4:])[2] == 2
        values[3] = other_values[3]
        entries[3][3] = entries[3][3][:4] + encode(FACTORY_TYPES, values)
    elif change in {
        "prepare_question",
        "prepare_question_and_condition",
        "nonbinary_prepare",
    }:
        values = list(decode(PREPARE_TYPES, entries[2][3][4:]))
        values[2 if change == "nonbinary_prepare" else 1] = (
            3 if change == "nonbinary_prepare" else keccak(text="other question")
        )
        entries[2][3] = entries[2][3][:4] + encode(PREPARE_TYPES, values)
        if change == "prepare_question_and_condition":
            factory = list(decode(FACTORY_TYPES, entries[3][3][4:]))
            factory[3] = [keccak(encode_packed(PREPARE_TYPES, values))]
            entries[3][3] = entries[3][3][:4] + encode(FACTORY_TYPES, factory)
    elif change == "missing_prepare":
        entries.pop(2)
    elif change == "duplicate_prepare":
        entries.append(entries[2])
    elif change == "prepare_operation":
        entries[2][0] = 1
    elif change in {"prepare_target", "question_target"}:
        entries[2 if change == "prepare_target" else 1][1] = "0x" + "11" * 20
    else:
        index = {
            "question_arbitrator": 2,
            "question_timeout": 3,
            "question_opening": 4,
            "question_nonce": 5,
        }[change]
        values = list(decode(QUESTION_TYPES, entries[1][3][4:]))
        values[index] = "0x" + "11" * 20 if index == 2 else values[index] + 1
        entries[1][3] = entries[1][3][:4] + encode(QUESTION_TYPES, values)
    changed = repack(tx, outer, entries)
    module = subject()
    assert (
        module.decode_calls(
            changed, MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY
        )
        is None
    )
    assert (
        module.decode_pending(
            changed,
            LightningConfig(),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
        )
        is None
    )


@pytest.mark.parametrize("row,tx", list(zip(ROWS, TRANSACTIONS)))
def test_decode_historical_calldata(row: Dict[str, Any], tx: Dict[str, Any]) -> None:
    """Decode all 108 historical Safe, MultiSend, factory and question calls."""
    result = subject().decode_calls(
        tx, MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY
    )
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
    call = module.decode_calls(
        tx, MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY
    )
    assert module.predict_market(call, FACTORY) == row["market"]


def test_still_hold_fixture_count() -> None:
    """Keep the existing phrase rule and report the actual final-night count."""
    module = subject()
    calls = [
        module.decode_calls(tx, MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY)
        for tx in TRANSACTIONS[-36:]
    ]
    assert len(calls) == 36
    assert (
        sum(
            bool(still_hold_rule(call.question, call.creator, LightningConfig()))
            for call in calls
        )
        == 30
    )


@pytest.mark.parametrize(
    "hint,accepted",
    [
        ([], True),
        ([1, 1], True),
        ([0, 0], False),
        ([2, 2], False),
        ([1], False),
        ([1, 2], False),
        ([1, 1, 1], False),
    ],
)
def test_pending_distribution_hints(hint: List[int], accepted: bool) -> None:
    """Accept only empty hints and the explicit equal binary hint."""
    tx = TRANSACTIONS[0]
    outer, entries = unpack(tx)
    values = list(decode(FACTORY_TYPES, entries[3][3][4:]))
    values[6] = hint
    entries[3][3] = entries[3][3][:4] + encode(FACTORY_TYPES, values)
    candidate = subject().decode_pending(
        repack(tx, outer, entries),
        LightningConfig(),
        MULTISEND,
        CONDITIONAL_TOKENS,
        REALITIO_PROXY,
        SEEN,
    )
    assert (candidate is not None) == accepted
    if candidate is not None:
        assert candidate.market["outcomeTokenAmounts"] == [7 * 10**18] * 2


@pytest.mark.parametrize(
    "tip,enabled,skip_reason",
    [
        pytest.param(0.0, False, "zero_tip", id="default-controls"),
        pytest.param(1.0, False, "still_hold_off", id="positive-tip-only"),
        pytest.param(1.0, True, "", id="enabled-shot-controls"),
    ],
)
def test_real_pending_transactions_reach_lane(
    tmp_path: Path, tip: float, enabled: bool, skip_reason: str
) -> None:
    """Use real calldata with default guards and with shot controls enabled."""
    cfg = LightningConfig(
        lightning_lane=True,
        lightning_rpc_url="https://rpc.invalid",
        lightning_lane_tip_gwei=tip,
        lightning_still_hold=enabled,
    )
    fast = FastConfig()
    cache = BaseRateCache(tmp_path / "rate.json")
    rejected, decoded, still_hold, shots = [], 0, 0, 0
    for row, tx in zip(ROWS, TRANSACTIONS):
        now = row["opening_ts"] - 86400 + 3600
        assert now < row["opening_ts"]
        assert fast.in_window(now)
        cache.data = dict(
            n=100,
            rate=0.946,
            refreshed_at=now,
            template_id=2,
            window_days=cfg.lightning_base_rate_window_days,
            creators=sorted(cfg.lightning_creators),
        )
        assert cache.usable(now, cfg) == 0.946
        try:
            metadata = subject().question_metadata(
                row["template_id"], row["question"], row["opening_ts"]
            )
        except ValueError:
            metadata = None
        candidate = subject().decode_pending(
            tx, cfg, MULTISEND, CONDITIONAL_TOKENS, REALITIO_PROXY, now
        )
        if not metadata:
            assert candidate is None
            rejected.append(row["market"])
            continue
        assert candidate is not None, row["market"]
        assert candidate.market["id"] == row["market"]
        assert candidate.market["outcomeTokenAmounts"] == [row["initial_funds_wei"]] * 2
        decoded += 1
        if not still_hold_rule(metadata["title"], row["creator_safe"], cfg):
            continue
        still_hold += 1
        decision, bounds, reason = lane_decision(
            candidate.market,
            candidate.market["outcomeTokenAmounts"],
            now,
            fast,
            cfg,
            cache,
            trade_count=0,
            max_trades=3,
            placement_min_edge=fast.min_edge,
        )
        assert reason == skip_reason, row["market"]
        if skip_reason:
            assert decision is None
            assert bounds is None
        else:
            assert decision is not None, row["market"]
            assert decision.stake > 0, row["market"]
            assert bounds is not None, row["market"]
            shots += 1
    assert rejected == ["0x15c1a4842ae2295304e0ec19c8d02981c0b20d61"]
    assert (decoded, still_hold) == (107, 83)
    assert shots == (0 if skip_reason else 83)


def test_pending_row_and_fees_are_complete() -> None:
    """Build a real Bet row and preserve both creator fee fields."""
    module = subject()
    candidate = module.decode_pending(
        TRANSACTIONS[0],
        LightningConfig(),
        MULTISEND,
        CONDITIONAL_TOKENS,
        REALITIO_PROXY,
        SEEN,
    )
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
        subject().decode_pending(
            dict(TRANSACTIONS[0], **change),
            LightningConfig(),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
        )
        is None
    )


@pytest.mark.parametrize(
    "field",
    ["type", "maxPriorityFeePerGas", "maxFeePerGas", "hash", "input", "from", "to"],
)
def test_missing_pending_field_is_ignored(field: str) -> None:
    """Require complete EIP-1559 and creation data."""
    tx = TRANSACTIONS[0].copy()
    tx.pop(field)
    assert (
        subject().decode_pending(
            tx, LightningConfig(), MULTISEND, CONDITIONAL_TOKENS, REALITIO_PROXY, SEEN
        )
        is None
    )


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
        ("question", 0, 0),
        ("question", 1, 'Will it still hold?␟"No","Yes"'),
        ("question", 1, 'Will it still hold?␟"Yes","No","Maybe"'),
        ("question", 1, "Will it still hold?"),
        ("question", 1, 'Bad "quote␟"Yes","No"'),
    ],
)
def test_strict_creation_fields(field: str, index: int, value: Any) -> None:
    """Require the exact supported Safe, market and question forms."""
    tx = TRANSACTIONS[0].copy()
    outer, entries = unpack(tx)
    if field == "outer":
        outer[index] = value
    else:
        entry = entries[3 if field == "factory" else 1]
        types = FACTORY_TYPES if field == "factory" else QUESTION_TYPES
        values = list(decode(types, entry[3][4:]))
        values[index] = value
        entry[3] = entry[3][:4] + encode(types, values)
        if field == "question":
            # Keep the link valid to test the metadata checks after it.
            template, question, arbitrator, timeout, opening, nonce = values
            content = keccak(
                encode_packed(
                    ["uint256", "uint32", "string"], [template, opening, question]
                )
            )
            question_id = keccak(
                encode_packed(
                    ["bytes32", "address", "uint32", "address", "uint256"],
                    [content, arbitrator, timeout, tx["to"], nonce],
                )
            )
            prepare = list(decode(PREPARE_TYPES, entries[2][3][4:]))
            prepare[1] = question_id
            entries[2][3] = entries[2][3][:4] + encode(PREPARE_TYPES, prepare)
            factory = list(decode(FACTORY_TYPES, entries[3][3][4:]))
            factory[3] = [keccak(encode_packed(PREPARE_TYPES, prepare))]
            entries[3][3] = entries[3][3][:4] + encode(FACTORY_TYPES, factory)
    assert (
        subject().decode_pending(
            repack(tx, outer, entries),
            LightningConfig(),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
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
    tx = TRANSACTIONS[0].copy()
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
    assert (
        subject().decode_pending(
            tx, LightningConfig(), MULTISEND, CONDITIONAL_TOKENS, REALITIO_PROXY, SEEN
        )
        is None
    )


def test_configured_factory_and_creator_are_required() -> None:
    """Read the configured factory and Safe allowlist for each pending call."""
    module = subject()
    tx = TRANSACTIONS[0].copy()
    assert (
        module.decode_pending(
            tx,
            LightningConfig(lightning_creators=[ROWS[1]["creator_safe"]]),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
        )
        is None
    )
    assert (
        module.decode_pending(
            tx,
            LightningConfig(omen_fpmm_factory="0x" + "11" * 20),
            MULTISEND,
            CONDITIONAL_TOKENS,
            REALITIO_PROXY,
            SEEN,
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
    tx = dict(TRANSACTIONS[0], type=2, maxPriorityFeePerGas=123, maxFeePerGas=456)
    tx["from"] = tx["from"].upper().replace("0X", "0x")
    tx["to"] = tx["to"].upper().replace("0X", "0x")
    candidate = subject().decode_pending(
        tx,
        LightningConfig(),
        MULTISEND.upper(),
        CONDITIONAL_TOKENS,
        REALITIO_PROXY,
        SEEN,
    )
    assert candidate is not None
    assert (candidate.priority_fee, candidate.max_fee) == (123, 456)


def test_create2_uses_every_constructor_input() -> None:
    """Keep the source formula sensitive to constructor data and caller."""
    module = subject()
    call = module.decode_calls(
        TRANSACTIONS[0], MULTISEND, FACTORY, CONDITIONAL_TOKENS, REALITIO_PROXY
    )
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
