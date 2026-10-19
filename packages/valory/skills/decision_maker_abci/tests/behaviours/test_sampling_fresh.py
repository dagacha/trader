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

"""Tests for fresh market priority in sampling."""

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.decision_maker_abci.behaviours.sampling import (
    SamplingBehaviour,
    UNIX_DAY,
)
from packages.valory.skills.market_manager_abci.bets import Bet, QueueStatus

NOW = 1700000000


def _make_bet(bet_id: str, **overrides: Any) -> Bet:
    """Build a processable Omen market."""
    values: Dict[str, Any] = dict(
        id=bet_id,
        market="omen_subgraph",
        title="Will it rain?",
        collateralToken="0xtoken",
        creator="0xcreator",
        fee=0,
        openingTimestamp=NOW + 5 * UNIX_DAY,
        outcomeSlotCount=2,
        outcomeTokenAmounts=[100, 100],
        outcomeTokenMarginalPrices=[0.5, 0.5],
        outcomes=["Yes", "No"],
        scaledLiquidityMeasure=7.0,
        queue_status=QueueStatus.TO_PROCESS,
    )
    values.update(overrides)
    return Bet(**values)


@pytest.fixture
def sampling(monkeypatch: pytest.MonkeyPatch) -> SamplingBehaviour:
    """Build a sampler with a fixed cycle time and real bets."""
    behaviour = object.__new__(SamplingBehaviour)
    context = MagicMock()
    context.params = SimpleNamespace(
        prioritize_fresh_markets=True,
        fresh_market_max_age=1800,
        use_multi_bets_mode=False,
        enable_multi_bets_fallback=False,
        sample_bets_closing_days=60,
        opening_margin=100,
        safe_voting_range=100,
        disabled_polymarket_tags=[],
        is_outcome_side_threshold_filter_enabled=True,
        outcome_side_threshold_filter_threshold=0.95,
        is_running_on_polymarket=False,
        exclude_neg_risk_markets=True,
    )
    context.benchmarking_mode.enabled = False
    context.benchmarking_mode.nr_mech_calls = 10
    context.state.benchmarking_mech_calls = 0
    context.state.get_simulated_now_timestamp.return_value = NOW
    context.state.liquidity_cache = {}
    behaviour.__dict__["_context"] = context
    monkeypatch.setattr(SamplingBehaviour, "synced_timestamp", NOW)
    monkeypatch.setattr(SamplingBehaviour, "kpi_is_met", False)
    monkeypatch.setattr(SamplingBehaviour, "review_bets_for_selling", False)
    return behaviour


def test_fresh_beats_older_balanced_market(sampling: SamplingBehaviour) -> None:
    """Pick the fresh skewed market ahead of the older, more liquid market."""
    sampling.bets = [
        _make_bet(
            "fresh",
            creationTimestamp=NOW - 100,
            scaledLiquidityMeasure=4.5,
            outcomeTokenMarginalPrices=[0.88, 0.12],
        ),
        _make_bet(
            "old",
            creationTimestamp=NOW - 5000,
            scaledLiquidityMeasure=6.5,
            outcomeTokenMarginalPrices=[0.68, 0.32],
        ),
    ]
    assert sampling._sample() == 0
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=1 picked=fresh age_s=100"
    )


@pytest.mark.parametrize("second_age, expected", [(50, 1), (100, 0)])
def test_newest_first_then_liquidity(
    sampling: SamplingBehaviour, second_age: int, expected: int
) -> None:
    """Pick the newest market and use liquidity to break a creation-time tie."""
    sampling.bets = [
        _make_bet("liquid", creationTimestamp=NOW - 100),
        _make_bet(
            "less_liquid",
            creationTimestamp=NOW - second_age,
            scaledLiquidityMeasure=4.5,
        ),
    ]
    assert sampling._sample() == expected


def test_fresh_market_with_bets_is_ignored(sampling: SamplingBehaviour) -> None:
    """Fresh priority applies only before the first bet, even in multi-bet mode."""
    sampling.context.params.use_multi_bets_mode = True
    sampling.bets = [
        _make_bet(
            "already_bet",
            creationTimestamp=NOW - 100,
            investments={"Yes": [10], "No": []},
            scaledLiquidityMeasure=1.0,
        ),
        _make_bet("never_bet", creationTimestamp=NOW - 200),
    ]
    assert sampling.bets[0].n_bets > 0
    assert sampling._sample() == 1
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=1 picked=never_bet age_s=200"
    )


@pytest.mark.parametrize(
    "timestamp, expected", [(0, 1), (NOW - 1801, 1), (NOW - 1800, 0)]
)
def test_creation_time_and_age_limit(
    sampling: SamplingBehaviour, timestamp: int, expected: int
) -> None:
    """Ignore unknown or old creation times and include the exact age limit."""
    sampling.bets = [
        _make_bet("candidate", creationTimestamp=timestamp, scaledLiquidityMeasure=4.5),
        _make_bet("old", creationTimestamp=NOW - 5000),
    ]
    assert sampling._sample() == expected
    if expected == 1:
        sampling.context.logger.info.assert_any_call(
            "[FRESH_FIRST] fresh_candidates=0 fallback=priority"
        )


@pytest.mark.parametrize("status", [QueueStatus.PROCESSED, QueueStatus.REPROCESSED])
def test_fresh_processed_market_uses_normal_priority(
    sampling: SamplingBehaviour, status: QueueStatus
) -> None:
    """Use normal priority when the only fresh market was already processed."""
    sampling.bets = [
        _make_bet(
            "retry",
            creationTimestamp=NOW - 100,
            queue_status=status,
            processed_timestamp=NOW - 50,
            scaledLiquidityMeasure=4.5,
        ),
        _make_bet("old", creationTimestamp=NOW - 5000),
    ]
    assert sampling._sample() == 1
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=0 fallback=priority"
    )


def test_fresh_to_process_beats_newer_processed_market(
    sampling: SamplingBehaviour,
) -> None:
    """Pick a fresh TO_PROCESS market before a newer processed market."""
    sampling.bets = [
        _make_bet(
            "processed",
            creationTimestamp=NOW - 50,
            queue_status=QueueStatus.PROCESSED,
            processed_timestamp=NOW - 25,
        ),
        _make_bet("fresh", creationTimestamp=NOW - 100),
    ]
    assert sampling._sample() == 1
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=1 picked=fresh age_s=100"
    )


@pytest.mark.parametrize("disabled_by", ["flag", "benchmarking"])
def test_disabled_keeps_existing_order(
    sampling: SamplingBehaviour, disabled_by: str
) -> None:
    """Use the existing queue and liquidity order when fresh priority is off."""
    sampling.bets = [
        _make_bet("fresh", creationTimestamp=NOW - 100, scaledLiquidityMeasure=4.5),
        _make_bet("old", creationTimestamp=NOW - 5000),
    ]
    if disabled_by == "flag":
        sampling.context.params.prioritize_fresh_markets = False
    else:
        sampling.context.benchmarking_mode.enabled = True
    assert sampling._sample() == 1
    assert not any(
        "[FRESH_FIRST]" in call.args[0]
        for call in sampling.context.logger.info.call_args_list
    )


@pytest.mark.parametrize("rejection", ["skew", "zero_liquidity", "neg_risk"])
def test_filters_drop_fresh_pick_and_try_next(
    sampling: SamplingBehaviour, rejection: str
) -> None:
    """Run the existing rejection filters on each fresh pick."""
    rejected = _make_bet("rejected", creationTimestamp=NOW - 50)
    if rejection == "skew":
        rejected.outcomeTokenMarginalPrices = [0.99, 0.01]
    elif rejection == "zero_liquidity":
        rejected.scaledLiquidityMeasure = 0.0
    else:
        sampling.context.params.is_running_on_polymarket = True
        rejected.neg_risk = True
    sampling.bets = [
        rejected,
        _make_bet(
            "next_fresh", creationTimestamp=NOW - 100, scaledLiquidityMeasure=4.5
        ),
        _make_bet("old", creationTimestamp=NOW - 5000),
    ]
    assert sampling._sample() == 1
    assert sampling.shared_state.liquidity_cache == {"next_fresh": 4.5}
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=2 picked=rejected age_s=50"
    )
    sampling.context.logger.info.assert_any_call(
        "[FRESH_FIRST] fresh_candidates=1 picked=next_fresh age_s=100"
    )


def test_polymarket_without_creation_times_keeps_order(
    sampling: SamplingBehaviour,
) -> None:
    """Keep Polystrat sampling unchanged even if the fresh flag is enabled."""
    sampling.context.params.is_running_on_polymarket = True
    sampling.bets = [
        _make_bet(
            "low_liquidity", market="polymarket_client", scaledLiquidityMeasure=4.5
        ),
        _make_bet("high_liquidity", market="polymarket_client"),
    ]
    assert sampling._sample() == 1
