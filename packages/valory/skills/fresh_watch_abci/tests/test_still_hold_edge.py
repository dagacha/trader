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

"""Keep the still-hold edge separate from other strategy bounds."""

# pylint: disable=protected-access

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import yaml

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastBetPlacementBehaviour,
    FastDecisionBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lane import lane_decision
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.models import FastParams
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, decide
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane import decision_inputs
from packages.valory.skills.market_manager_abci.bets import Bet, PredictionResponse


@pytest.mark.parametrize(
    "minimum,edge,yes_only,reason",
    [
        (0.04, 0.04, True, ""),
        (0.04, 0.0399, True, "min_edge"),
        (0.08, 0.0799, True, "min_edge"),
        (0.08, 0.08, True, ""),
        (0.08, 0.09, True, ""),
        (0.08, 0.04, False, ""),
        (0.0, 0.01, True, ""),
    ],
)
def test_still_hold_min_edge(
    minimum: float, edge: float, yes_only: bool, reason: str
) -> None:
    """Apply the resolved edge only to the YES-only strategy."""
    cfg = replace(FastConfig(), prior_weight=1, still_hold_min_edge=minimum)
    result = decide(
        "Will it still hold?",
        0.80 + edge,
        [2 * 10**29, 8 * 10**29],
        [0.80, 0.20],
        cfg,
        0,
        yes_only,
    )
    assert result.reason == reason
    assert (result.stake == 0) if reason else (result.stake > 0)


@pytest.mark.parametrize("value", [-0.01, 1.0, 1.01])
def test_still_hold_edge_validation(tmp_path: Any, value: float) -> None:
    """Reject an edge outside the supported range."""
    args = yaml.safe_load((Path(__file__).parents[1] / "skill.yaml").read_text())[
        "models"
    ]["params"]["args"]
    args.update(store_path=str(tmp_path), still_hold_min_edge=value)
    with pytest.raises(ValueError, match="0 <= still_hold_min_edge < 1"):
        FastParams(name="params", skill_context=MagicMock(), **args)


@pytest.mark.parametrize("minimum", [0.0, 0.04, 0.08, 0.999])
def test_still_hold_edge_startup(tmp_path: Any, minimum: float) -> None:
    """Read and log the edge once when the parameter model starts."""
    args = yaml.safe_load((Path(__file__).parents[1] / "skill.yaml").read_text())[
        "models"
    ]["params"]["args"]
    assert args["still_hold_min_edge"] == FastConfig().still_hold_min_edge == 0.04
    args.update(store_path=str(tmp_path), still_hold_min_edge=minimum)
    context = MagicMock()
    params = FastParams(name="params", skill_context=context, **args)
    assert params.fast.still_hold_min_edge == minimum
    records = [
        call.args
        for call in context.logger.info.call_args_list
        if call.args[0] == "[FAST_CONFIG] still_hold_min_edge=%s"
    ]
    assert records == [("[FAST_CONFIG] still_hold_min_edge=%s", minimum)]


@pytest.mark.parametrize(
    "minimum,kind,ceiling",
    [
        (0.04, "direct", 0.82),
        (0.08, "direct", 0.78),
        (0.08, "mech", 0.78),
        (0.08, "ordinary", 0.82),
        (0.08, "disabled", 0.82),
    ],
)
def test_still_hold_placement_ceiling(
    tmp_path: Any, minimum: float, kind: str, ceiling: float
) -> None:
    """Leave the price margin while keeping the configured placement edge."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, still_hold_min_edge=minimum)
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=kind != "disabled"
    )
    row = raw_market()
    if kind == "ordinary":
        row["title"] = "Will it rain?"
    row.pop("condition")
    row.pop("question")
    bet = Bet(  # pylint: disable=unexpected-keyword-arg
        **row, condition_id="condition", market="omen_subgraph"
    )
    values = dict(
        question_template_id=2,
        lightning_rule="still_hold_phrase",
        lightning_probability=None if kind == "mech" else 0.85,
    )
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.bet_amount = 10**18
        sd.return_value.db.get.side_effect = values.get
        b.quoted_tokens = 2 * 10**18
        bounds = b._ceiling_min_out(0.85, 0.7)
    assert bounds is not None
    assert bounds[0] == pytest.approx(ceiling)


def test_lane_edge_does_not_use_still_hold_setting(tmp_path: Any) -> None:
    """Keep the lane's strategy and ceiling when the follower edge changes."""
    inputs = decision_inputs(tmp_path)
    expected = lane_decision(**inputs)
    inputs["fast"] = replace(inputs["fast"], still_hold_min_edge=0.99)
    assert lane_decision(**inputs) == expected


@pytest.mark.parametrize("still_hold,minimum", [(True, 0.08), (False, 0.04)])
def test_decision_logs_resolved_edge(
    tmp_path: Any, still_hold: bool, minimum: float
) -> None:
    """Log the edge used for the decision, with the existing edge field."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, still_hold_min_edge=0.08)
    b.context.params.lightning = LightningConfig(lightning_still_hold=still_hold)
    row = raw_market()
    row.pop("condition")
    row.pop("question")
    bet = Bet(  # pylint: disable=unexpected-keyword-arg
        **row, condition_id="condition", market="omen_subgraph"
    )
    values = dict(
        question_template_id=2,
        requested_at=1050,
        request_prices=[0.8, 0.2],
        lightning_probability=0.85,
        lightning_rule="still_hold_phrase",
        lightning_preview_request=False,
    )
    b._get_pool_balances = lambda *args: done([2 * 10**29, 8 * 10**29])
    b._get_decision_prediction = lambda: PredictionResponse(
        p_yes=0.81, p_no=0.19, confidence=1.0, info_utility=1.0
    )
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
        sd.return_value.db.get.side_effect = values.get
        run(b.async_act())
    records = [
        json.loads(call.args[1])
        for call in b.context.logger.info.call_args_list
        if call.args[0] == "[FAST_DECISION] %s"
    ]
    assert records[-1]["min_edge"] == pytest.approx(minimum)
    assert "edge" in records[-1]
