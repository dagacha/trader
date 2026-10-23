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

"""Exercise fast behaviours with durable state and simulated I/O."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastBetPlacementBehaviour,
    FastDecisionBehaviour,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, FreshQueue
from packages.valory.skills.market_manager_abci.bets import Bet


def done(value: Any) -> Any:
    """Return a value from cooperative I/O."""
    yield from ()
    return value


def run(generator: Any) -> Any:
    """Run a finite behaviour step."""
    while True:
        try:
            next(generator)
        except StopIteration as result:
            return result.value


def make(cls: Any, tmp_path: Any) -> Any:
    """Build a behaviour with the normal storage paths."""
    context = MagicMock()
    context.params.store_path = tmp_path
    context.params.fast = FastConfig(active_window_utc="00:00-23:59")
    context.state.fast_last_poll = 0
    return cls(name="test", skill_context=context)


def raw_market() -> Any:
    """Return a valid binary Omen market."""
    return dict(
        id="market",
        title="Will it still hold?",
        creator=FastConfig().creators[0],
        creationTimestamp=1000,
        openingTimestamp=100000,
        fee=0,
        collateralToken="token",
        outcomeSlotCount=2,
        outcomeTokenAmounts=[100, 100],
        outcomeTokenMarginalPrices=[0.5, 0.5],
        outcomes=["Yes", "No"],
        scaledLiquidityMeasure=100,
        condition={"id": "condition"},
    )


def test_select_and_restart(tmp_path: Any) -> None:
    """Request only one market and retain the asked set on disk."""
    b = make(FreshWatchBehaviour, tmp_path)
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools={"superforcaster"})
        ]
        b._fetch_fresh = lambda cursor, now: done([raw_market()])
        b._get_pool_balances = lambda *args: done([100, 100])
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            selection = run(b.select())
        assert selection["lane"] == "market"
        assert selection["mech_tool"] == "superforcaster"
        assert selection["request_prices"] == [0.5, 0.5]
        assert json.loads((tmp_path / "fresh_watch.json").read_text())["asked"] == [
            "market"
        ]
        assert (
            json.loads((tmp_path / "multi_bets.json").read_text())[0]["id"] == "market"
        )
        queue = FreshQueue(tmp_path / "fresh_watch.json")
        assert queue.pending == []


@pytest.mark.parametrize(
    "prediction,balances,reason,stake",
    [
        (0.9, [100, 100], "", 4),
        (None, [100, 100], "mech_response", 0),
        (0.9, None, "live_price_unavailable", 0),
        (0.9, [35, 65], "max_slip", 0),
    ],
)
def test_evaluate(
    tmp_path: Any, prediction: Any, balances: Any, reason: Any, stake: Any
) -> None:
    """Evaluate a real sampled bet without policy setup or strategy loading."""
    b = make(FastDecisionBehaviour, tmp_path)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "requested_at": 1050,
        }[key]
        b._get_decision_prediction = lambda: (
            None if prediction is None else SimpleNamespace(p_yes=prediction)
        )
        b._get_pool_balances = lambda *args: done(balances)
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ):
            result = run(b.evaluate())
    assert result["reason"] == reason
    assert result["stake"] == stake
    assert result["age_at_request"] == 50
    assert result["age_at_answer"] == 100


def test_calibrated_placement_prediction(tmp_path: Any) -> None:
    """The final execution check uses the blended probability."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.db.get_strict.return_value = json.dumps(dict(p_hat=0.875))
        prediction = b._get_decision_prediction()
    assert prediction.p_yes == 0.875
    assert prediction.p_no == 0.125


def test_housekeeping_accounting(tmp_path: Any) -> None:
    """Redeem can build and store its payload without a tool policy fetch."""
    from packages.valory.skills.fresh_watch_abci.behaviours import (
        HousekeepingRedeemBehaviour,
    )

    b = make(HousekeepingRedeemBehaviour, tmp_path)
    assert run(b._setup_policy_and_tools())
    b.context.benchmarking_mode.enabled = False
    payload = b._build_payload()
    assert set(json.loads(payload.mech_tools)) == set(FastConfig().tools)
    b._store_all()
    assert (tmp_path / "policy_store_multi_bet_failure_adjusting.json").exists()


def test_housekeeping_restarts_redeem_scan(tmp_path: Any) -> None:
    """A new housekeeping entry must not reuse a completed redeem scan."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.redeeming_progress.check_started = True
    b.context.params.event_filtering_batch_size = 5000
    b._fetch_fresh = lambda *args: done([])
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(b.select())["lane"] == "housekeeping"
    assert b.context.state.redeeming_progress.check_started is False
    assert b.context.state.redeeming_progress.event_filtering_batch_size == 5000


@pytest.mark.parametrize(
    "pages", [[None], [[raw_market()]], [[raw_market()] * 1000, []]]
)
def test_query_pages(tmp_path: Any, pages: Any) -> None:
    """Read every timestamp-bounded page and fail closed on an API error."""
    b = make(FreshWatchBehaviour, tmp_path)
    api = b.context.omen_subgraph
    api.get_spec.return_value = {"method": "POST", "url": "https://example.test"}
    api.process_response.side_effect = pages
    calls = []

    def request(**kwargs: Any) -> Any:
        calls.append(json.loads(kwargs["content"])["query"])
        return done(None)

    b.get_http_response = request
    result = run(b._fetch_fresh(900, 1100))
    assert len(calls) == len(pages)
    assert 'creationTimestamp_gt: "900"' in calls[0]
    assert 'creationTimestamp_lte: "1100"' in calls[0]
    assert 'openingTimestamp_gt: "1100"' in calls[0]
    if pages[0] is None:
        assert result is None
    else:
        assert len(result) == len(pages[0])


@pytest.mark.parametrize(
    "case",
    [
        "outside",
        "wait",
        "unavailable",
        "no_tool",
        "no_quote",
        "existing",
        "closed",
        "missing_condition",
        "primary",
    ],
)
def test_watch_paths(tmp_path: Any, case: str) -> None:
    """Handle idle, unavailable, restart, and tool-fallback paths."""
    from dataclasses import replace

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.event_filtering_batch_size = 5000
    b.sleep = lambda *args: done(None)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 1050
    if case == "outside":
        b.context.params.fast = replace(FastConfig(), active_window_utc="01:00-02:00")
    elif case in {
        "no_tool",
        "no_quote",
        "existing",
        "closed",
        "missing_condition",
        "primary",
    }:
        row = raw_market()
        if case == "closed":
            row["openingTimestamp"] = 1001
        if case == "missing_condition":
            row.pop("condition")
        queue.pending = [row]
    queue.save()
    b._fetch_fresh = lambda *args: done(None if case == "unavailable" else [])
    b._get_pool_balances = lambda *args: done(
        None if case == "no_quote" else [100, 100]
    )
    if case == "wait":
        b.context.state.fast_last_poll = 1050
    if case == "existing":
        row = raw_market()
        row.pop("condition")
        b.bets = [Bet(**row, market="omen_subgraph")]
        b.store_bets()
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.mechs_info = [
            SimpleNamespace(
                relevant_tools=set() if case == "no_tool" else set(FastConfig().tools)
            )
        ]
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            selection = run(b.select())
    assert selection["lane"] == (
        "housekeeping"
        if case == "outside"
        else (
            "market" if case in {"existing", "missing_condition", "primary"} else "wait"
        )
    )
    if case == "primary":
        assert selection["mech_tool"] == FastConfig().tools[0]
    if case in {"closed", "no_quote"}:
        assert b.context.logger.info.call_count >= 1


@pytest.mark.parametrize(
    "cls,result",
    [
        (FreshWatchBehaviour, {"lane": "wait"}),
        (FastDecisionBehaviour, {"stake": 0}),
        (FastDecisionBehaviour, {"stake": 1}),
    ],
)
def test_consensus_payload(tmp_path: Any, cls: Any, result: Any) -> None:
    """Send the selected data and log only a skipped decision here."""
    b = make(cls, tmp_path)
    if cls == FreshWatchBehaviour:
        b.select = lambda: done(result)
    else:
        b.evaluate = lambda: done(result)
    payloads = []

    def finish(payload: Any) -> Any:
        payloads.append(payload)
        return done(None)

    b.finish_behaviour = finish
    run(b.async_act())
    assert json.loads(payloads[0].selection) == result


@pytest.mark.parametrize(
    "amount,success,allowed",
    [(0, True, False), (100, False, False), (100, True, True), (50, True, False)],
)
def test_execution_quote(
    tmp_path: Any, amount: int, success: bool, allowed: bool
) -> None:
    """Apply slip and price bounds to the final executable quote."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.quoted_tokens = amount
    with (
        patch.object(
            BetPlacementBehaviour, "_calc_buy_amount", return_value=done(success)
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        sd.return_value.db.get_strict.return_value = [0.5, 0.5]
        assert run(b._calc_buy_amount()) is allowed


@pytest.mark.parametrize("placed,tokens", [(True, 100), (False, 0)])
def test_placement_log(tmp_path: Any, placed: bool, tokens: int) -> None:
    """Emit one terminal decision line with the final preparation result."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.quoted_tokens = tokens
    with (
        patch.object(
            BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(type(b), "sampled_bet", new_callable=PropertyMock) as sampled,
    ):
        sampled.return_value.creationTimestamp = 1000
        sd.return_value.db.get_strict.return_value = json.dumps(
            dict(p_hat=0.875, stake=4, reason="")
        )
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ):
            run(
                b.finish_behaviour(
                    BetPlacementPayload("agent", tx_hash="tx" if placed else None)
                )
            )
    args = b.context.logger.info.call_args.args
    assert args[0] == "[FAST_DECISION] %s"
    result = json.loads(args[1])
    assert result["age_at_bet"] == 100
    assert result["stake"] == (4 if placed else 0)


def test_closed_at_answer(tmp_path: Any) -> None:
    """Skip a market that closed while the mech was running."""
    b = make(FastDecisionBehaviour, tmp_path)
    raw = raw_market()
    raw.pop("condition")
    raw["openingTimestamp"] = 1070
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.db.get_strict.return_value = 1050
        b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.9)
        b._get_pool_balances = lambda *args: done([100, 100])
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ):
            assert run(b.evaluate())["reason"] == "market_closed"


def test_discovery_runs_during_request(tmp_path: Any) -> None:
    """Collect another market while the foreground request is in flight."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    poller = make(FreshMarketPoller, tmp_path)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [raw_market()]
    queue.save()
    another = {**raw_market(), "id": "next", "creationTimestamp": 1040}

    def fetch(*args: Any) -> Any:
        yield None
        return [another]

    poller._fetch_fresh = fetch
    poller.sleep = lambda delay: done(None)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        generator = poller.async_act()
        next(generator)
        assert poller.context.state.fast_polling is True
        assert (FreshQueue(queue.path).pop() or {})["id"] == "market"
        run(generator)
    reloaded = FreshQueue(queue.path)
    assert reloaded.asked == {"market"}
    assert [row["id"] for row in reloaded.pending] == ["next"]
    assert poller.context.state.fast_polling is False


@pytest.mark.parametrize("polling", [True, False])
def test_watch_waits_for_discovery(tmp_path: Any, polling: bool) -> None:
    """Do not enter housekeeping before the first discovery result arrives."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.fast_background_running = True
    b.context.state.fast_polling = polling
    b.sleep = lambda *args: done(None)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(b.select())["lane"] == "wait"


@pytest.mark.parametrize(
    "case", ["outside", "queued_outside", "recent", "failure", "timeout"]
)
def test_discovery_schedule(tmp_path: Any, case: str) -> None:
    """Poll only when required and recover after a missing response."""
    from dataclasses import replace

    from packages.valory.skills.abstract_round_abci.behaviour_utils import (
        TimeoutException,
    )
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = make(FreshMarketPoller, tmp_path)
    if "outside" in case:
        b.context.params.fast = replace(FastConfig(), active_window_utc="01:00-02:00")
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    if case == "queued_outside":
        queue.pending = [raw_market()]
    queue.save()
    if case == "recent":
        b.context.state.fast_last_poll = 1050
    calls = []

    def fetch(*args: Any) -> Any:
        calls.append(args)
        if case == "timeout":
            raise TimeoutException()
        yield from ()
        return None if case == "failure" else []

    b._fetch_fresh = fetch
    delays = []

    def sleep(delay: float) -> Any:
        delays.append(delay)
        return done(None)

    b.sleep = sleep
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        run(b.async_act())
    assert len(calls) == (0 if case in {"outside", "recent"} else 1)
    assert delays == [15]


def test_discovery_callback(tmp_path: Any) -> None:
    """Deliver a poll reply even when another behaviour owns the FSM round."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = make(FreshMarketPoller, tmp_path)
    callback = b.get_callback_request()
    message = MagicMock()
    callback(message, MagicMock())
    assert not b.is_notified
    waiting = b.wait_for_message()
    next(waiting)
    callback(message, MagicMock())
    assert b.received_message is message
    waiting.close()


def test_background_queue_has_priority(tmp_path: Any) -> None:
    """Process queued work during an in-flight discovery query."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.fast_background_running = True
    b.context.state.fast_polling = True
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [raw_market()]
    queue.save()
    b._get_pool_balances = lambda *args: done([100, 100])
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools=set(FastConfig().tools))
        ]
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            assert run(b.select())["lane"] == "market"
