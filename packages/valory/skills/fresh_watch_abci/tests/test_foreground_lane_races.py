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

"""Keep lane state when foreground work resumes after a wait."""

from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastBetPlacementBehaviour,
    FastDecisionBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.market_manager_abci.bets import Bet, QueueStatus


def market_bet(market_id: str) -> Bet:
    """Build a stored market with a stable identifier."""
    row = raw_market()
    row.pop("question")
    row.pop("condition")
    return Bet(**{**row, "id": market_id}, market="omen_subgraph")


@pytest.mark.parametrize("lane_market", ["market", "new-market"])
def test_decision_preserves_lane_purchase_during_quote(
    tmp_path: Any, lane_market: str
) -> None:
    """Merge the decision into bets loaded after the quote wait."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.shared_state = {}
    b.bets = [market_bet("market")]
    b.store_bets()
    b._get_decision_prediction = lambda: type("Prediction", (), {"p_yes": 0.9})()

    def quote(*_args: Any) -> Any:
        yield
        return [100 * 10**18, 100 * 10**18]

    b._get_pool_balances = quote
    selection = {"requested_at": 1050, "request_prices": [0.5, 0.5]}
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch("time.time", return_value=1100),
    ):
        sd.return_value.sampled_bet_index = 0
        sd.return_value.db.get_strict.side_effect = selection.__getitem__
        generator = b.evaluate()
        assert next(generator) is None
        lane_writer = make(FastDecisionBehaviour, tmp_path)
        lane_writer.read_bets()
        purchased = next(
            (bet for bet in lane_writer.bets if bet.id == lane_market), None
        )
        if purchased is None:
            purchased = market_bet(lane_market)
            lane_writer.bets.append(purchased)
        purchased.investments["Yes"] = [4 * 10**18]
        purchased.processed_timestamp = 1090
        purchased.queue_status = QueueStatus.PROCESSED
        lane_writer.store_bets()
        assert run(generator)["stake"] == 4

    b.read_bets()
    purchased = next(bet for bet in b.bets if bet.id == lane_market)
    assert purchased.investments == {"Yes": [4 * 10**18], "No": []}
    assert purchased.processed_timestamp == 1090
    assert purchased.queue_status == QueueStatus.PROCESSED
    selected = next(bet for bet in b.bets if bet.id == "market")
    assert selected.strategy_vote == 0
    assert selected.prediction_response is not None


def test_placement_rechecks_durable_trade_cap_after_guard(tmp_path: Any) -> None:
    """Skip the prepared foreground buy when the lane fills the trade cap."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_lane=True)
    b.context.params.max_trades = 1
    b.context.shared_state = {}
    b.bets = [market_bet("market")]
    b.store_bets()
    payloads = []

    def guard() -> Any:
        yield
        assert b.record_successful_placement("omen:lane-shot") == 1

    b.wait_for_lightning_lane = guard

    def finish(payload: Any) -> Any:
        payloads.append(payload)
        return done(None)

    b.finish_behaviour = finish
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            BetPlacementBehaviour,
            "async_act",
            side_effect=AssertionError("foreground buy exceeded the trade cap"),
        ),
    ):
        sd.return_value.sampled_bet_index = 0
        sd.return_value.successful_trade_count = 0
        sd.return_value.previous_checkpoint = None
        generator = b.async_act()
        assert next(generator) is None
        run(generator)
    assert len(payloads) == 1
    assert payloads[0].tx_hash is None
    assert b._placement_failure == "trade_cap"
