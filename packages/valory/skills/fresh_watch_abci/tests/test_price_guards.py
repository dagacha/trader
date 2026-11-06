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

"""Reject pools that no longer support the requested trade."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastDecisionBehaviour,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lane import lane_decision
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.models import FastParams
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, decide
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane import decision_inputs
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MODULE,
    lane,
)
from packages.valory.skills.market_manager_abci.bets import Bet


@pytest.mark.parametrize(
    "price,yes_only,reason",
    [
        (0.45, True, "still_hold_price_floor"),
        (0.65, True, ""),
        (0.60, True, ""),
        (0.45, False, ""),
    ],
)
def test_still_hold_live_price_floor(price: float, yes_only: bool, reason: str) -> None:
    """Apply the floor to live YES prices only for the YES-only route."""
    result = decide(
        "Will it still hold?",
        0.93,
        [int((1 - price) * 10**24), int(price * 10**24)],
        [price, 1 - price],
        FastConfig(),
        0,
        yes_only=yes_only,
    )
    assert result.reason == reason
    assert (result.stake == 0) if reason else (result.stake > 0)


@pytest.mark.parametrize(
    "requested,live,side,limit,reason",
    [
        (0.70, 0.45, 0, 0.15, "price_dropped"),
        (0.55, 0.45, 0, 0.15, ""),
        (0.60, 0.45, 0, 0.15, ""),
        (0.70, 0.45, 1, 0.15, "price_dropped"),
        (0.70, 0.45, 0, 1.0, ""),
    ],
)
def test_price_drop_guard(
    requested: float, live: float, side: int, limit: float, reason: str
) -> None:
    """Compare request and live prices on the selected side, with tolerance."""
    prices = [live, 1 - live] if side == 0 else [1 - live, live]
    requests = [requested, 1 - requested] if side == 0 else [1 - requested, requested]
    cfg = replace(FastConfig(), prior_weight=1, max_price_drop=limit)
    result = decide(
        "An event?",
        0.93 if side == 0 else 0.07,
        [int(prices[1] * 10**24), int(prices[0] * 10**24)],
        requests,
        cfg,
        0,
    )
    assert result.side == side
    assert result.reason == reason
    assert (result.stake == 0) if reason else (result.stake > 0)


def test_floor_precedes_price_drop() -> None:
    """Name the floor first when both live-price guards fail."""
    result = decide(
        "Will it still hold?",
        0.93,
        [55 * 10**22, 45 * 10**22],
        [0.70, 0.30],
        FastConfig(),
        0,
        yes_only=True,
    )
    assert result.reason == "still_hold_price_floor"
    assert result.stake == 0


@pytest.mark.parametrize(
    "price,reversed_order,expected",
    [
        (0.40, False, "market"),
        (0.70, False, "lightning_decision"),
        (0.40, True, "market"),
        (0.55, False, "lightning_decision"),
    ],
)
def test_direct_selection_requires_pool_agreement(
    tmp_path: Any, price: float, reversed_order: bool, expected: str
) -> None:
    """Send cheap pools to the normal request path with one reserve read."""
    b = make(FreshWatchBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    b.context.params.lightning = cfg
    BaseRateCache(tmp_path / "lightning_base_rate.json").update(
        [{**raw_market(), "payouts": [1, 0]}], 1050, cfg
    )
    row = raw_market()
    if reversed_order:
        row["outcomes"] = ["No", "Yes"]
    b._fetch_fresh = lambda *args: done([row])
    b._get_pool_balances = MagicMock(
        side_effect=lambda *args: done([int((1 - price) * 10**24), int(price * 10**24)])
    )
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools={"superforcaster"})
        ]
        selection = run(b.select())
    assert selection["lane"] == expected
    b._get_pool_balances.assert_called_once()
    calls = [
        call.args
        for call in b.context.logger.info.call_args_list
        if call.args[0]
        == "[LIGHTNING_STILL_HOLD] skipped_direct market=%s yes_price=%s"
    ]
    if price < 0.55 and not reversed_order:
        assert calls == [
            (
                "[LIGHTNING_STILL_HOLD] skipped_direct market=%s yes_price=%s",
                row["id"],
                price,
            )
        ]
    else:
        assert calls == []


@pytest.mark.parametrize(
    "price,reason", [(0.40, "entry_price"), (0.55, "still_hold_price_floor")]
)
def test_lane_entry_price(tmp_path: Any, price: float, reason: str) -> None:
    """Accept the entry threshold, then apply the shared decision guards."""
    inputs = decision_inputs(tmp_path)
    inputs["fast"] = FastConfig()
    inputs["lightning"].lightning_still_hold_min_entry_price = 0.55
    inputs["balances"] = [int((1 - price) * 10**24), int(price * 10**24)]
    decision, bounds, actual = lane_decision(**inputs)
    assert actual == reason
    assert bounds is None
    if reason == "entry_price":
        assert decision is None
    else:
        assert decision is not None
        assert decision.stake == 0


@pytest.mark.parametrize("name", ["still_hold_min_price", "max_price_drop"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_fast_price_guard_validation(tmp_path: Any, name: str, value: float) -> None:
    """Reject price controls outside zero to one."""
    from pathlib import Path

    import yaml

    args = yaml.safe_load((Path(__file__).parents[1] / "skill.yaml").read_text())[
        "models"
    ]["params"]["args"]
    args["store_path"] = str(tmp_path)
    args[name] = value
    with pytest.raises(ValueError, match=name):
        FastParams(name="params", skill_context=MagicMock(), **args)


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_lightning_entry_price_validation(value: float) -> None:
    """Reject an unbounded direct-entry threshold."""
    with pytest.raises(ValueError, match="probability controls"):
        LightningConfig(lightning_still_hold_min_entry_price=value)


@pytest.mark.parametrize("reason", ["still_hold_price_floor", "price_dropped"])
def test_fast_decision_logs_price_guard(tmp_path: Any, reason: str) -> None:
    """Retain the shared strategy reason in the FAST_DECISION record."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    if reason == "price_dropped":
        b.context.params.fast = replace(b.context.params.fast, still_hold_min_price=0)
    row = raw_market()
    row.pop("condition")
    row.pop("question", None)
    bet = Bet(**row, market="omen_subgraph")
    values = dict(
        question_template_id=2,
        requested_at=1050,
        request_prices=[0.70, 0.30],
        lightning_probability=0.95,
        lightning_rule="still_hold_phrase",
        lightning_preview_title=None,
        lightning_preview_request=False,
    )
    b._get_pool_balances = lambda *args: done([55 * 10**22, 45 * 10**22])
    b.finish_behaviour = lambda *args: done(None)
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ),
    ):
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        run(b.async_act())
    records = [
        json.loads(call.args[1])
        for call in b.context.logger.info.call_args_list
        if call.args[0] == "[FAST_DECISION] %s"
    ]
    assert records[-1]["reason"] == reason
    assert records[-1]["stake"] == 0


def test_lane_logs_price_floor(tmp_path: Any) -> None:
    """Retain the strategy reason in the lane decision log."""
    from pathlib import Path

    from eth_abi import encode

    from packages.valory.skills.fresh_watch_abci.chain_discovery import creation_details

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/chain_creation.json").read_text()
    )
    b = lane(tmp_path, lightning_still_hold=True)
    b.context.shared_state["lightning_lane_fsm_preparing"] = False
    b._send = MagicMock(return_value=done(None))
    cfg = b.context.params.lightning
    BaseRateCache(tmp_path / "lightning_base_rate.json").update(
        [
            {
                **raw_market(),
                "id": str(index),
                "creationTimestamp": fixture["blockTimestamp"] - 60,
                "payouts": [1, 0],
            }
            for index in range(cfg.lightning_base_rate_min_n)
        ],
        fixture["blockTimestamp"],
        cfg,
    )
    b._rpc = MagicMock(
        side_effect=[
            done({"logs": fixture["logs"]}),
            done("0x" + encode(["uint256[]"], [[45 * 10**22, 55 * 10**22]]).hex()),
        ]
    )
    block = {
        "timestamp": hex(fixture["blockTimestamp"]),
        "hash": "0xblock",
        "number": "0x2",
    }
    with patch(MODULE + ".time.time", return_value=fixture["blockTimestamp"] + 0.4):
        run(b._market(fixture["logs"][-1], block, fixture["blockTimestamp"] + 0.3))
    b._send.assert_not_called()
    assert b.context.logger.info.call_args.args == (
        "[LIGHTNING_LANE] market=%s reason=%s",
        creation_details(fixture["logs"][-1])["id"],
        "still_hold_price_floor",
    )


def test_fee_cannot_rescue_a_pool_below_the_floor() -> None:
    """Keep the floor on the pool price before the execution fee."""
    result = decide(
        "Will it still hold?",
        0.93,
        [41 * 10**22, 59 * 10**22],
        [0.59, 0.41],
        FastConfig(),
        5 * 10**16,
        yes_only=True,
    )
    assert result.price > 0.60
    assert result.reason == "still_hold_price_floor"
    assert result.stake == 0


def test_zero_floor_allows_cheap_still_hold_yes() -> None:
    """Allow operators to turn off the floor."""
    result = decide(
        "Will it still hold?",
        0.93,
        [55 * 10**22, 45 * 10**22],
        [0.45, 0.55],
        replace(FastConfig(), still_hold_min_price=0),
        0,
        yes_only=True,
    )
    assert result.reason == ""
    assert result.stake > 0
