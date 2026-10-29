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

"""Require the canonical Omen outcome order for lightning shortcuts."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastBetPlacementBehaviour,
    FastDecisionBehaviour,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.market_manager_abci.bets import Bet


def test_reversed_still_hold_uses_normal_request(tmp_path: Any) -> None:
    """Request a tool when the matching title has reversed outcomes."""
    b = make(FreshWatchBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    b.context.params.lightning = cfg
    BaseRateCache(tmp_path / "lightning_base_rate.json").update(
        [{**raw_market(), "payouts": [1, 0]}], 1050, cfg
    )
    b._fetch_fresh = lambda *args: done([{**raw_market(), "outcomes": ["No", "Yes"]}])
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    assert selection["lane"] == "market"
    assert selection["mech_tool"] == "superforcaster"
    assert "lightning_probability" not in selection


@pytest.mark.parametrize("stale_direct", [False, True])
def test_reversed_still_hold_uses_normal_decision(
    tmp_path: Any, stale_direct: bool
) -> None:
    """Keep the normal side choice even when old direct metadata is present."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, no_min_p=0.5)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    raw = {**raw_market(), "outcomes": ["No", "Yes"]}
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    values = dict(requested_at=1050, request_prices=[0.8, 0.2])
    if stale_direct:
        values.update(lightning_probability=0.95, lightning_rule="still_hold_phrase")
    b._get_decision_prediction = MagicMock(return_value=SimpleNamespace(p_yes=0.1))
    b._get_pool_balances = lambda *args: done([20 * 10**27, 80 * 10**27])
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
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        result = run(b.evaluate())
    assert result["p_tool"] == 0.1
    assert result["p_hat"] == pytest.approx(0.475)
    assert result["side"] == 1
    assert result["stake"] > 0
    b._get_decision_prediction.assert_called_once_with()


def test_reversed_still_hold_retains_normal_execution_cap(tmp_path: Any) -> None:
    """Ignore stale direct metadata when the selected pool reverses outcomes."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    values = dict(lightning_probability=0.95, lightning_rule="still_hold_phrase")
    raw = {**raw_market(), "outcomes": ["No", "Yes"]}
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        assert b._execution_max_price() == 0.9


def test_base_rate_excludes_reversed_outcome_order(tmp_path: Any) -> None:
    """Count only resolutions where the first payout refers to Yes."""
    cfg = LightningConfig(lightning_base_rate_min_n=1)
    cache = BaseRateCache(tmp_path / "rate.json")
    cache.update(
        [
            {**raw_market(), "id": "canonical", "payouts": [1, 0]},
            {
                **raw_market(),
                "id": "reversed",
                "outcomes": ["No", "Yes"],
                "payouts": [0, 1],
            },
        ],
        1050,
        cfg,
    )
    assert cache.sample == 1
    assert cache.rate == 1


def test_base_rate_query_fetches_outcome_names(tmp_path: Any) -> None:
    """Read the outcome labels needed to validate each resolved payout."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    b.context.omen_subgraph.process_response.return_value = []
    b.get_http_response = MagicMock(side_effect=lambda **kwargs: done(None))
    run(b._refresh_base_rate(1050))
    query = json.loads(b.get_http_response.call_args.kwargs["content"])["query"]
    fields = query.split("}) {", 1)[1]
    assert "outcomes" in fields.split()
