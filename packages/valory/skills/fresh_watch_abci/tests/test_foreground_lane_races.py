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

import builtins
from pathlib import Path
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastBetPlacementBehaviour,
    FastDecisionBehaviour,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    lane,
    settled,
)
from packages.valory.skills.market_manager_abci.bets import Bet, QueueStatus


def market_bet(market_id: str) -> Bet:
    """Build a stored market with a stable identifier."""
    row = raw_market()
    row.pop("question")
    row.pop("condition")
    return Bet(**{**row, "id": market_id}, market="omen_subgraph")


@pytest.mark.parametrize("restore_path", ["quote_failure", "no_tool"])
@pytest.mark.parametrize("remembered", ["shared", "stored"])
@pytest.mark.parametrize("enabled", [True, False])
def test_selection_does_not_restore_lane_purchase_during_quote(
    tmp_path: Any, restore_path: str, remembered: str, enabled: bool
) -> None:
    """Keep a completed lane purchase out of both foreground retry paths."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.shared_state = {}
    b.context.state.fast_background_running = True
    b.context.params.lightning = LightningConfig(
        lightning_lane=enabled,
        lightning_still_hold=restore_path == "no_tool",
        lightning_base_rate_min_n=1,
    )
    b._fast_tool = lambda: None if restore_path == "no_tool" else "superforcaster"
    b.sleep = lambda _delay: done(None)
    row = {**raw_market(), "creationTimestamp": 199950, "openingTimestamp": 300000}
    another = {**row, "id": "new-market", "creationTimestamp": 200001}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [row]
    queue.save()
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    cache.update(
        [{**raw_market(), "creationTimestamp": 27000, "payouts": [1, 0]}],
        27200,
        b.context.params.lightning,
    )
    assert cache.usable(200000, b.context.params.lightning) == 1
    assert cache.usable(200001, b.context.params.lightning) is None

    def quote(*_args: Any) -> Any:
        yield
        return None if restore_path == "quote_failure" else [100 * 10**18] * 2

    b._get_pool_balances = quote
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch("time.time", return_value=200000) as clock,
    ):
        sd.return_value.previous_checkpoint = None
        sd.return_value.successful_trade_count = 0
        generator = b.select()
        assert next(generator) is None
        assert FreshQueue(queue.path).asked == {row["id"]: 199950}
        lane_writer = lane(tmp_path, lightning_lane=enabled)
        lane_writer.context.shared_state = b.context.shared_state
        info = dict(
            tx="0xfeed",
            market=row["id"],
            row=row,
            amount=4 * 10**18,
            probability=0.95,
        )
        lane_writer._complete(info, {"status": "0x1"}, {"timestamp": hex(200001)})
        assert FreshQueue(queue.path).asked == {}
        lane_writer.read_bets()
        assert lane_writer.bets[0].has_investments
        if remembered == "stored":
            b.context.shared_state.pop("lightning_lane_bet_markets")
        discovered = FreshQueue(queue.path)
        discovered.ingest([another], 200001, b.context.params.fast.creators, 600)
        discovered.save()
        clock.return_value = 200001
        assert run(generator)["lane"] == "wait"

    reloaded = FreshQueue(queue.path)
    assert reloaded.asked == {}
    assert reloaded.pending == [another]


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


def test_decision_keeps_positions_after_merge_read_failure(tmp_path: Any) -> None:
    """Decline without writing when the read after the quote fails once."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.shared_state = {}
    older = market_bet("older-position")
    older.set_investment_amount(1, 2 * 10**18)
    b.bets = [market_bet("market"), older]
    b.store_bets()
    path = Path(b.multi_bets_filepath)
    original_bets = path.read_bytes()
    b._get_decision_prediction = lambda: type("Prediction", (), {"p_yes": 0.9})()

    def quote(*_args: Any) -> Any:
        yield
        return [100 * 10**18, 100 * 10**18]

    b._get_pool_balances = quote
    original_open = builtins.open
    failed: list[bool] = []
    writes: list[str] = []

    def fail_read(filename: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(filename) == path:
            if mode == "r" and not failed:
                failed.append(True)
                raise OSError("single bets read failure")
            if mode == "w":
                writes.append(mode)
        return original_open(filename, mode, *args, **kwargs)

    selection = {"requested_at": 1050, "request_prices": [0.5, 0.5]}
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch("time.time", return_value=1100),
    ):
        sd.return_value.sampled_bet_index = 0
        sd.return_value.db.get_strict.side_effect = selection.__getitem__
        generator = b.evaluate()
        assert next(generator) is None
        with patch("builtins.open", side_effect=fail_read):
            result = run(generator)

    assert failed == [True]
    assert result["reason"] == "bets_read_failed"
    assert result["stake"] == 0
    assert writes == []
    assert path.read_bytes() == original_bets
    b.read_bets()
    assert {bet.id: bet.invested_amount for bet in b.bets} == {
        "market": 0,
        "older-position": 2 * 10**18,
    }


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


def test_flag_off_restored_purchase_blocks_second_buy_after_guard(
    tmp_path: Any,
) -> None:
    """Skip a second buy after a restored lane shot fills during the wait."""
    writer = lane(tmp_path)
    info = settled(writer, tmp_path)
    writer.context.shared_state["lightning_lane_inflight"] = info
    writer._persist()
    writer = lane(tmp_path, lightning_lane=False)
    info = writer.context.shared_state["lightning_lane_inflight"]
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_lane=False)
    b.context.params.max_trades = 3
    b.context.shared_state = writer.context.shared_state
    b.bets = [market_bet(MARKET)]
    b.store_bets()
    payloads = []

    def sleep(_delay: float) -> Any:
        yield

    b.sleep = sleep

    def finish(payload: Any) -> Any:
        payloads.append(payload)
        return done(None)

    b.finish_behaviour = finish
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch("time.time", return_value=1005),
        patch.object(
            BetPlacementBehaviour,
            "async_act",
            side_effect=AssertionError("foreground placed a second buy"),
        ),
    ):
        sd.return_value.sampled_bet_index = 0
        sd.return_value.successful_trade_count = 0
        sd.return_value.previous_checkpoint = None
        generator = b.async_act()
        assert next(generator) is None
        writer._complete(info, {"status": "0x1"}, {"timestamp": hex(1005)})
        assert b.durable_trade_count() == 1
        assert b.durable_trade_count() < b.params.max_trades
        run(generator)
    assert len(payloads) == 1
    assert payloads[0].tx_hash is None
    assert b._placement_failure == "lane_already_bet"
