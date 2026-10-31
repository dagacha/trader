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

"""Test the pure direct-send lane controls and transaction encoding."""

from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any

import pytest
from eth_abi import decode, encode
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.chain_discovery import CREATION_TOPIC
from packages.valory.skills.fresh_watch_abci.lane import (
    RPCStats,
    SlotPoller,
    build_safe_calldata,
    buy_result,
    creation_filter,
    execution_ceiling,
    fixed_transaction,
    funding_balances,
    lane_decision,
    pool_balances_call,
)
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, decide


def test_slot_poller_aligns_steps_and_resyncs() -> None:
    """Poll the next slot, step on null, and resync after six seconds."""
    poller = SlotPoller(offset=0.3, step=0.25)
    assert poller.request(100) == ("eth_blockNumber", [])
    poller.height("0x14", 100)
    assert poller.request(100) == ("eth_getBlockByNumber", ["0x14", False])
    head = {"number": "0x14", "timestamp": "0x64", "hash": "0xabc"}
    assert poller.block(head, 100.1) == head
    assert poller.request(105.29) is None
    assert poller.request(105.3) == ("eth_getBlockByNumber", ["0x15", False])
    assert poller.block(None, 105.3) is None
    assert poller.request(105.54) is None
    assert poller.request(105.55) == ("eth_getBlockByNumber", ["0x15", False])
    poller.block(None, 111.3)
    assert poller.request(111.3) == ("eth_blockNumber", [])


def test_creation_filter_is_hash_scoped_and_creator_filtered() -> None:
    """Ask for only this block and the two padded creator topics."""
    creators = ["0x" + "12" * 20, "0x" + "34" * 20]
    assert creation_filter("0xabc", "0x" + "56" * 20, creators) == {
        "blockHash": "0xabc",
        "address": "0x" + "56" * 20,
        "topics": [
            CREATION_TOPIC,
            ["0x" + "00" * 12 + "12" * 20, "0x" + "00" * 12 + "34" * 20],
        ],
    }


def test_rpc_stats_pause_and_detect_percentiles() -> None:
    """Pause after five errors and report measured block detection latency."""
    stats = RPCStats(started_at=0)
    for now in range(5):
        stats.called(now, error=True)
    assert not stats.ready(63.99)
    assert stats.ready(64)
    stats.called(64)
    stats.detected(100, 100.2)
    stats.detected(105, 105.4)
    assert stats.report(599) is None
    report = stats.report(600)
    assert report is not None
    assert "calls=6 errors=5" in report
    assert "p50_detect_s=0.200" in report
    assert "p90_detect_s=0.400" in report
    assert stats.report(600) is None


def test_execution_ceiling_keeps_fast_integer_results() -> None:
    """Preserve fast's exact fraction comparison and wei rounding."""

    assert execution_ceiling(10**18, 2 * 10**18, 0.9, 0.5, 0.9, 0.1, 0.03) == (
        0.6,
        1666666666666666667,
    )
    assert execution_ceiling(10**18, 2 * 10**18, 0.95, 0.91, 0.93, 0.2, 0.03) == (
        0.92,
        1086956521739130435,
    )
    assert execution_ceiling(10**18, 2 * 10**18, 0.99, 0.91, 0.93, 0.2, 0.03) == (
        0.93,
        1075268817204301076,
    )
    assert execution_ceiling(10**18, 2 * 10**18, 0.9, 0.5, 0.5, 0.2, 0.03) is None


def decision_inputs(tmp_path: Any) -> Any:
    """Supply current evidence and a new binary still-hold market."""

    values = asdict(LightningConfig())
    values.update(
        lightning_lane=True,
        lightning_rpc_url="https://key.invalid",
        lightning_lane_tip_gwei=550.0,
        lightning_still_hold=True,
        lightning_lane_max_age_s=6,
    )
    cfg = SimpleNamespace(**values)
    fast = FastConfig()
    cache = BaseRateCache(tmp_path / "rate.json")
    cache.data = dict(
        n=100,
        rate=0.95,
        refreshed_at=1000,
        template_id=2,
        window_days=21,
        creators=sorted(fast.creators),
    )
    market = dict(
        id="0x" + "12" * 20,
        title="Will it still hold?",
        creator=fast.creators[0],
        creationTimestamp=1000,
        openingTimestamp=2000,
        outcomes=["Yes", "No"],
        question={"templateId": 2},
        fee=0,
    )
    return dict(
        market=market,
        balances=[7 * 10**18] * 2,
        now=1000,
        fast=fast,
        lightning=cfg,
        cache=cache,
        trade_count=0,
        max_trades=10,
        placement_min_edge=0.03,
    )


def test_lane_decision_matches_still_hold_fsm_yes_sizing(tmp_path: Any) -> None:
    """Use the base-rate YES route and the same strategy controls as the FSM."""

    inputs = decision_inputs(tmp_path)
    decision, bounds, reason = lane_decision(**inputs)
    expected = decide(
        "Will it still hold?",
        0.95,
        inputs["balances"],
        [0.5, 0.5],
        replace(inputs["fast"], prior_weight=1.0, max_price=0.93),
        0,
        yes_only=True,
    )
    assert decision == expected
    assert decision.side == 0
    assert decision.stake == 4
    assert bounds is not None
    assert bounds[1] > 0
    assert reason == ""


def test_zero_global_trade_cap_allows_lane_shots(tmp_path: Any) -> None:
    """Treat the service's zero trade cap as unlimited."""
    inputs = decision_inputs(tmp_path)
    inputs.update(max_trades=0, trade_count=100)
    decision, bounds, reason = lane_decision(**inputs)
    assert reason == ""
    assert decision is not None and decision.stake > 0
    assert bounds is not None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("title", "not_still_hold"),
        ("creator", "not_still_hold"),
        ("template", "unsupported_question"),
        ("outcomes", "unsupported_question"),
        ("base_rate", "base_rate"),
        ("cap", "trade_cap"),
        ("age", "market_age"),
        ("future", "market_age"),
        ("window", "outside_window"),
        ("off", "disabled"),
        ("rpc", "disabled"),
        ("tip", "zero_tip"),
        ("still_off", "still_hold_off"),
        ("ceiling", "ceiling"),
        ("invalid_price", "invalid_price"),
    ],
)
def test_lane_decision_rejects_ineligible_shots(
    tmp_path: Any, change: str, reason: str
) -> None:
    """Fail closed when the question, evidence, budget, age, or controls fail."""

    inputs = decision_inputs(tmp_path)
    market = inputs["market"]
    cfg = inputs["lightning"]
    if change == "title":
        market["title"] = "Will it still holder?"
    elif change == "creator":
        market["creator"] = "0x" + "ab" * 20
    elif change == "template":
        market["question"]["templateId"] = 0
    elif change == "outcomes":
        market["outcomes"] = ["No", "Yes"]
    elif change == "base_rate":
        inputs["cache"].data["n"] = 1
    elif change == "cap":
        inputs["trade_count"] = 10
    elif change == "age":
        inputs["now"] = 1006.01
    elif change == "future":
        inputs["now"] = 999
    elif change == "window":
        inputs["fast"] = replace(inputs["fast"], active_window_utc="10:00-11:00")
    elif change == "off":
        cfg.lightning_lane = False
    elif change == "rpc":
        cfg.lightning_rpc_url = ""
    elif change == "tip":
        cfg.lightning_lane_tip_gwei = 0
    elif change == "still_off":
        cfg.lightning_still_hold = False
    elif change == "ceiling":
        inputs["placement_min_edge"] = 0.5
    elif change == "invalid_price":
        inputs["balances"] = [0, 0]
    decision, bounds, actual = lane_decision(**inputs)
    assert actual == reason
    assert bounds is None


@pytest.mark.parametrize(
    ("wrapped", "native", "shortfall"), [(100, 0, 0), (40, 60, 60), (0, 100, 100)]
)
def test_safe_calldata_matches_hand_built_fixture(
    wrapped: int, native: int, shortfall: int
) -> None:
    """Encode exact approval and YES buy, with a deposit only for shortfall."""

    owner, multisend, collateral, market = [
        "0x" + value * 20 for value in ["11", "22", "33", "44"]
    ]
    data, value = build_safe_calldata(
        owner, multisend, collateral, market, 100, 167, wrapped, native
    )
    approve = bytes.fromhex("095ea7b3") + encode(["address", "uint256"], [market, 100])
    buy = bytes.fromhex("40993b26") + encode(
        ["uint256", "uint256", "uint256"], [100, 0, 167]
    )
    calls = (
        [] if not shortfall else [(collateral, shortfall, bytes.fromhex("d0e30db0"))]
    ) + [(collateral, 0, approve), (market, 0, buy)]
    packed = b"".join(
        b"\0"
        + bytes.fromhex(target[2:])
        + eth_value.to_bytes(32, "big")
        + len(payload).to_bytes(32, "big")
        + payload
        for target, eth_value, payload in calls
    )
    multi = bytes.fromhex("8d80ff0a") + encode(["bytes"], [packed])
    signature = bytes.fromhex(owner[2:]).rjust(32, b"\0") + bytes(32) + b"\1"
    expected = bytes.fromhex("6a761202") + encode(
        [
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
        ],
        [
            multisend,
            shortfall,
            multi,
            1,
            0,
            0,
            0,
            "0x" + "00" * 20,
            "0x" + "00" * 20,
            signature,
        ],
    )
    assert data == expected
    assert value == shortfall
    assert decode(["uint256", "uint256", "uint256"], buy[4:]) == (100, 0, 167)


def test_safe_calldata_refuses_insufficient_collateral() -> None:
    """Do not prepare a deposit when the Safe cannot fund its shortfall."""

    with pytest.raises(ValueError, match="collateral"):
        build_safe_calldata(
            "0x" + "11" * 20,
            "0x" + "11" * 20,
            "0x" + "11" * 20,
            "0x" + "11" * 20,
            100,
            167,
            40,
            59,
        )


def test_fixed_transaction_uses_only_configured_tip() -> None:
    """Set the fixed priority fee and twice this block's base fee."""

    owner, safe = "0x" + "11" * 20, "0x" + "22" * 20
    tx = fixed_transaction(owner, safe, b"\x12\x34", 9, 7, 400000, 550.25, 10**18)
    assert tx == dict(
        type=2,
        chainId=100,
        nonce=9,
        gas=400000,
        maxPriorityFeePerGas=550250000000,
        maxFeePerGas=550250000014,
        to=safe,
        data="0x1234",
        value=0,
        **{"from": owner},
    )


@pytest.mark.parametrize(
    ("tip", "cap", "balance", "reason"),
    [
        (0, 1000, 10**18, "positive"),
        (1001, 1000, 10**18, "cap"),
        (550, 2001, 10**18, "cap"),
        (550, 1000, 1, "gas"),
    ],
)
def test_fixed_transaction_refuses_zero_tip_cap_and_gas_shortfall(
    tip: float, cap: float, balance: int, reason: str
) -> None:
    """Reject a shot before signing when its fixed fee or budget is invalid."""

    with pytest.raises(ValueError, match=reason):
        fixed_transaction(
            "0x" + "11" * 20, "0x" + "22" * 20, b"", 9, 7, 400000, tip, balance, cap
        )


def test_receipt_funding_and_buy_decoders() -> None:
    """Read only this market's funding and the receipt's indexed buy event."""

    market = "0x" + "11" * 20
    buyer = "0x" + "22" * 20
    funding = dict(
        address=market,
        topics=[
            "0x" + keccak(text="FPMMFundingAdded(address,uint256[],uint256)").hex(),
            "0x" + "00" * 32,
        ],
        data="0x"
        + encode(["uint256[]", "uint256"], [[7 * 10**18] * 2, 7 * 10**18]).hex(),
    )
    assert funding_balances([funding], market) == [7 * 10**18] * 2
    assert funding_balances([funding], buyer) is None
    buy = dict(
        address=market,
        topics=[
            "0x"
            + keccak(text="FPMMBuy(address,uint256,uint256,uint256,uint256)").hex(),
            "0x" + buyer[2:].rjust(64, "0"),
            "0x" + "00" * 32,
        ],
        data="0x"
        + encode(["uint256", "uint256", "uint256"], [4 * 10**18, 0, 6 * 10**18]).hex(),
    )
    result = buy_result([funding, buy], market)
    assert result == dict(
        buyer=buyer,
        amount=4 * 10**18,
        fee=0,
        side=0,
        tokens=6 * 10**18,
        fill_price=2 / 3,
    )
    assert buy_result([buy], buyer) is None


def test_pool_balance_call_matches_gnosis_conditional_tokens() -> None:
    """Derive binary position IDs that match Gnosis getCollectionId calls."""

    market = "0xf46f0b6e9e96b466a3563d025577e65648c7daa7"
    conditional = "0xceafdd6bc0bef976fdcd1112955828e00543c0ce"
    collateral = "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
    condition = "0x36d6dcca866420a9c0986cbdbcaf8f84fed09be5985e3455982360e54337c25f"
    # Gnosis fork eth_call getCollectionId(0, condition, 1|2), block 48997577.
    positions = [
        17623024708731671955990245777950589555823001451951223688638223320802371091245,
        112744296842125221410047333035272068902379539123411342598942762315550639092504,
    ]
    to, data = pool_balances_call(market, conditional, collateral, condition)
    assert to.lower() == conditional
    assert (
        data
        == "0x"
        + (
            keccak(text="balanceOfBatch(address[],uint256[])")[:4]
            + encode(["address[]", "uint256[]"], [[market, market], positions])
        ).hex()
    )


@pytest.mark.parametrize("opening", [999, 1000, None])
def test_lane_decision_rejects_closed_or_missing_deadline(
    tmp_path: Any, opening: Any
) -> None:
    """Reject a deadline at or before now, and absent deadline metadata."""
    inputs = decision_inputs(tmp_path)
    if opening is None:
        inputs["market"].pop("openingTimestamp")
    else:
        inputs["market"]["openingTimestamp"] = opening
    decision, bounds, reason = lane_decision(**inputs)
    assert decision is None
    assert bounds is None
    assert reason == "market_closed"


def test_lane_decision_keeps_strategy_rejection(tmp_path: Any) -> None:
    """Keep the strategy's reason when YES costs more than its limit."""
    inputs = decision_inputs(tmp_path)
    inputs["balances"] = [10**18, 99 * 10**18]
    decision, bounds, reason = lane_decision(**inputs)
    assert decision is not None
    assert decision.stake == 0
    assert bounds is None
    assert reason == "max_price"


@pytest.mark.parametrize("amounts", [[7], [7, 7, 7], [0, 7]])
def test_funding_requires_two_positive_reserves(amounts: Any) -> None:
    """Leave unusable funding events to the reserve read fallback."""
    market = "0x" + "11" * 20
    log = dict(
        address=market,
        topics=[
            "0x" + keccak(text="FPMMFundingAdded(address,uint256[],uint256)").hex()
        ],
        data="0x" + encode(["uint256[]", "uint256"], [amounts, 7]).hex(),
    )
    assert funding_balances([log], market) is None


def test_buy_without_tokens_has_no_fill_price() -> None:
    """Reject a buy event with no tokens instead of dividing by zero."""
    market = "0x" + "11" * 20
    log = dict(
        address=market,
        topics=[
            "0x"
            + keccak(text="FPMMBuy(address,uint256,uint256,uint256,uint256)").hex(),
            "0x" + "00" * 32,
            "0x" + "00" * 32,
        ],
        data="0x" + encode(["uint256", "uint256", "uint256"], [4, 0, 0]).hex(),
    )
    assert buy_result([log], market) is None


def test_pool_balance_call_retries_non_residue() -> None:
    """Derive position IDs when the first curve candidate has no square root."""
    market, conditional, collateral = [
        "0x" + value * 20 for value in ("11", "22", "33")
    ]
    # Condition 3 needs two curve candidates for index set 2.
    target, data = pool_balances_call(
        market, conditional, collateral, "0x" + "00" * 31 + "03"
    )
    assert target == conditional
    owners, positions = decode(["address[]", "uint256[]"], bytes.fromhex(data[10:]))
    assert owners == (market, market)
    assert positions == (
        18836107302434001775683108182129277647488155787062377388152193028635158947911,
        100368091131051703796743362756066818936532217385031010946847646001836210833633,
    )
