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
    FreshMarketPoller,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, FreshQueue
from packages.valory.skills.market_manager_abci.bets import Bet
from packages.valory.skills.trader_abci.mech_latency import request_timing


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
    context.params.max_trades = 3
    context.state.synchronized_data.successful_trade_count = 0
    context.state.synchronized_data.previous_checkpoint = None
    context.state.synchronized_data.mech_only_mode = False
    behaviour = cls(name="test", skill_context=context)
    from packages.valory.skills.staking_abci.rounds import StakingState

    behaviour.service_staking_state = StakingState.STAKED
    behaviour.ts_checkpoint = 100
    counter = tmp_path / "successful_trade_count.txt"
    if not counter.exists():
        counter.write_text("0")
    behaviour._compute_activity_status = lambda: done((False, False, 0, 0))
    return behaviour


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
        assert json.loads((tmp_path / "fresh_watch.json").read_text())["asked"] == {
            "market": 1000
        }
        assert (
            json.loads((tmp_path / "multi_bets.json").read_text())[0]["id"] == "market"
        )
        queue = FreshQueue(tmp_path / "fresh_watch.json")
        assert queue.pending == []


@pytest.mark.parametrize(
    "dynamic,offchain", [(True, True), (False, True), (True, False)]
)
@pytest.mark.parametrize(
    "http_url,penalized,eligible",
    [
        (None, {}, False),
        ("", {}, False),
        ("https://mech.test", {"0xABC": 1000}, False),
        ("https://mech.test", {}, True),
    ],
)
def test_select_requires_eligible_offchain_mech(
    tmp_path: Any,
    dynamic: bool,
    offchain: bool,
    http_url: Any,
    penalized: Any,
    eligible: bool,
) -> None:
    """Keep the market queued when its tool has no eligible dynamic endpoint."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast_dynamic_mech = dynamic
    b.context.params.use_offchain = offchain
    b.context.state.penalized_mechs = penalized
    b.context.state.fast_background_running = True
    b.sleep = MagicMock(side_effect=lambda delay: done(None))
    b._get_pool_balances = MagicMock(side_effect=lambda *args: done([100, 100]))
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    row = raw_market()
    queue.pending = [row]
    queue.save()
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(
                address="0xabc",
                http_url=http_url,
                relevant_tools={"superforcaster"},
            ),
            SimpleNamespace(
                address="0xdef", http_url="https://other.test", relevant_tools=set()
            ),
        ]
        selection = run(b.select())
    selected = eligible or not (dynamic and offchain)
    assert selection["lane"] == ("market" if selected else "wait")
    reloaded = FreshQueue(queue.path)
    assert reloaded.pending == ([] if selected else [row])
    assert reloaded.asked == ({"market": 1000} if selected else {})
    if selected:
        assert selection["mech_tool"] == "superforcaster"
        b.sleep.assert_not_called()
    else:
        b.context.logger.warning.assert_called_once_with(
            "Neither fast prediction tool is available."
        )
        b.sleep.assert_called_once_with(FastConfig().poll_s)
        b._get_pool_balances.assert_not_called()


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
    b.context.shared_state = {
        "mech_latency": {
            "uuid": {"market": "market", "mech": "0xselected", "latency_s": 31}
        }
    }
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
    assert result["mech"] == "0xselected"
    assert result["latency_s"] == 31


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("condition_id", [None, "condition"])
def test_evaluate_matches_completed_market_timing(
    tmp_path: Any, completed: bool, condition_id: Any
) -> None:
    """Use the bet's completed record after an earlier nonce is overwritten."""
    b = make(FastDecisionBehaviour, tmp_path)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph", condition_id=condition_id)
    metadata = {"tool": "superforcaster", "request_context": bet.to_request_context()}
    timings = {
        "uuid": dict(request_timing(metadata, 1000), mech="0xold", latency_s=99),
        "other": {"market": "other_market", "mech": "0xother", "latency_s": 5},
    }
    timings["uuid"] = dict(request_timing(metadata, 1050), mech="0xselected")
    if completed:
        timings["uuid"]["latency_s"] = 31
    b.context.shared_state = {"mech_latency": timings}
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.db.get_strict.return_value = 1050
        b._get_decision_prediction = lambda: None
        result = run(b.evaluate())
    assert result["mech"] == ("0xselected" if completed else None)
    assert result["latency_s"] == (31 if completed else None)


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
    assert 'creationTimestamp_gte: "900"' in calls[0]
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
    if case == "no_quote":
        with (
            patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
            patch(
                "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
                return_value=1065,
            ),
        ):
            sd.return_value.mechs_info = [
                SimpleNamespace(relevant_tools=set(FastConfig().tools))
            ]
            b._get_pool_balances = lambda *args: done([100, 100])
            assert run(b.select())["lane"] == "market"


@pytest.mark.parametrize(
    "quoted_at,opening,retry",
    [(1600, 100000, True), (1601, 100000, False), (1050, 1050, False)],
)
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("discover_during_quote", [False, True])
def test_quote_failure_survives_restart(
    tmp_path: Any,
    quoted_at: int,
    opening: int,
    retry: bool,
    restart: bool,
    discover_during_quote: bool,
) -> None:
    """Restore only eligible rows and keep discovery made during a pool read."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.fast_background_running = True
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    row = {**raw_market(), "openingTimestamp": opening}
    another = {**raw_market(), "id": "next", "creationTimestamp": 1040}
    queue.pending = [row] if discover_during_quote else [row, another]
    queue.save()

    def quote(*args: Any) -> Any:
        yield None
        # The background poller can write while the foreground waits for RPC.
        if discover_during_quote:
            discovered = FreshQueue(queue.path)
            discovered.ingest([another], quoted_at, FastConfig().creators, 600)
            discovered.save()
        return None

    b._get_pool_balances = quote
    b.sleep = MagicMock(side_effect=lambda delay: done(None))
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=quoted_at,
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools=set(FastConfig().tools))
        ]
        assert run(b.select())["lane"] == "wait"
    reloaded = FreshQueue(queue.path)
    assert reloaded.asked == ({} if retry else {"market": 1000})
    assert reloaded.pending == ([row, another] if retry else [another])
    logged = json.loads(b.context.logger.info.call_args.args[1])
    assert logged["reason"] == (
        "request_price_unavailable_restored_for_retry"
        if retry
        else "request_closed_or_expired"
    )
    if not retry:
        b.sleep.assert_not_called()
        return
    b.sleep.assert_called_once_with(FastConfig().poll_s)
    restarted = make(FreshWatchBehaviour, tmp_path) if restart else b
    restarted.context.state.fast_background_running = True
    restarted._get_pool_balances = lambda *args: done([100, 100])
    with (
        patch.object(
            type(restarted), "synchronized_data", new_callable=PropertyMock
        ) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=quoted_at,
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools=set(FastConfig().tools))
        ]
        for market_id in ("market", "next"):
            selection = run(restarted.select())
            assert selection["lane"] == "market"
            assert restarted.bets[selection["sampled_bet_index"]].id == market_id
    assert FreshQueue(queue.path).asked == {"next": 1040, "market": 1000}


def test_string_timestamps_through_selection_and_decision(tmp_path: Any) -> None:
    """Subgraph decimal timestamps work through the stored bet and decision."""
    b = make(FreshWatchBehaviour, tmp_path)
    row = {**raw_market(), "creationTimestamp": "1000", "openingTimestamp": "100000"}
    b._fetch_fresh = lambda *args: done([row])
    b._get_pool_balances = lambda *args: done([100, 100])
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools=set(FastConfig().tools))
        ]
        selection = run(b.select())
    assert selection["lane"] == "market"
    decision = make(FastDecisionBehaviour, tmp_path)
    decision.read_bets()
    bet = decision.bets[selection["sampled_bet_index"]]
    assert bet.creationTimestamp == 1000
    assert bet.openingTimestamp == 100000
    decision._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.9)
    decision._get_pool_balances = lambda *args: done([100, 100])
    with (
        patch.object(
            type(decision), "synchronized_data", new_callable=PropertyMock
        ) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ),
    ):
        sd.return_value.sampled_bet_index = selection["sampled_bet_index"]
        sd.return_value.db.get_strict.side_effect = selection.__getitem__
        result = run(decision.evaluate())
    assert result["age_at_request"] == 50
    assert result["age_at_answer"] == 100
    assert result["stake"] == 4


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


@pytest.mark.parametrize(
    "opening,reason,stake",
    [(1099, "market_closed", 0), (1100, "market_closed", 0), (1101, "", 4)],
)
def test_closed_at_answer(tmp_path: Any, opening: int, reason: str, stake: int) -> None:
    """Skip a market that closed while the mech was running."""
    b = make(FastDecisionBehaviour, tmp_path)
    raw = raw_market()
    raw.pop("condition")
    raw["openingTimestamp"] = opening
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "requested_at": 1050,
            "request_prices": [0.5, 0.5],
        }[key]
        b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.9)
        b._get_pool_balances = lambda *args: done([100, 100])
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ):
            result = run(b.evaluate())
            assert result["reason"] == reason
            assert result["stake"] == stake


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
    assert reloaded.asked == {"market": 1000}
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


@pytest.mark.parametrize("cls", [FreshWatchBehaviour, FreshMarketPoller])
@pytest.mark.parametrize(
    "last_seen,overlap,expected", [(1000, 120, 880), (0, 120, 450), (1000, 60, 940)]
)
def test_overlap_query_bound(
    tmp_path: Any, cls: Any, last_seen: int, overlap: int, expected: int
) -> None:
    """Both discovery paths include the overlap and bound the query by age."""
    from dataclasses import replace

    b = make(cls, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, overlap_s=overlap)
    b.sleep = lambda *args: done(None)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_seen = last_seen
    queue.last_housekeeping = 1050
    queue.save()
    calls = []

    def fetch(cursor: int, now: int) -> Any:
        calls.append((cursor, now))
        return done([])

    b._fetch_fresh = fetch
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        run(b.async_act() if cls is FreshMarketPoller else b.select())
    assert calls == [(expected, 1050)]


@pytest.mark.parametrize(
    "selected_at,quoted_at,opening,allowed",
    [
        (1600, 1600, 100000, True),
        (1601, 1601, 100000, False),
        (1599, 1601, 100000, False),
        (1050, 1050, 1049, False),
        (1050, 1050, 1050, False),
        (1050, 1051, 1051, False),
    ],
)
def test_queued_market_expires_before_request(
    tmp_path: Any, selected_at: int, quoted_at: int, opening: int, allowed: bool
) -> None:
    """Only a fresh, open queue entry can enter the paid-request lane."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.fast_background_running = True
    b.sleep = lambda *args: done(None)
    row = {**raw_market(), "openingTimestamp": opening}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.ingest([row], 1001, FastConfig().creators, 600)
    queue.save()
    b._get_pool_balances = lambda *args: done([100, 100])
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            side_effect=[selected_at, quoted_at],
        ),
    ):
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools=set(FastConfig().tools))
        ]
        selection = run(b.select())
    assert selection["lane"] == ("market" if allowed else "wait")
    reloaded = FreshQueue(queue.path)
    assert reloaded.pending == []
    assert reloaded.asked == {"market": 1000}
    reloaded.ingest([row], quoted_at, FastConfig().creators, 600)
    reloaded.save()
    assert FreshQueue(queue.path).pending == []


@pytest.mark.parametrize(
    "count,cap,met,lane",
    [
        (2, 3, True, "market"),
        (3, 3, False, "mech_only"),
        (3, 3, True, "wait"),
        (9, 0, True, "market"),
    ],
)
def test_period_trade_cap(
    tmp_path: Any, count: int, cap: int, met: bool, lane: str
) -> None:
    """Keep trading below the cap; finish activity without bets after it."""
    from packages.valory.skills.staking_abci.rounds import StakingState

    path = tmp_path / "successful_trade_count.txt"
    path.write_text(json.dumps(dict(count=count, placement_keys=[], period_start=100)))
    cap_logs: list[str] = []
    for _ in range(2):  # Restart in the same staking period.
        b = make(FreshWatchBehaviour, tmp_path)
        b.context.params.max_trades = cap
        b.context.params.use_offchain = True
        b.service_staking_state = StakingState.STAKED
        b.ts_checkpoint = 100
        b._compute_activity_status = lambda: done((met, met, 8, 8 if met else 3))
        b._fetch_fresh = lambda *args: done([raw_market()])
        b._get_pool_balances = lambda *args: done([100, 100])
        b.sleep = lambda *args: done(None)
        queue = FreshQueue(tmp_path / "fresh_watch.json")
        queue.pending = [raw_market()]
        queue.asked = {}
        queue.last_housekeeping = 1050
        queue.save()
        with patch.object(
            type(b), "synchronized_data", new_callable=PropertyMock
        ) as sd:
            sd.return_value.successful_trade_count = 0
            sd.return_value.previous_checkpoint = 100
            sd.return_value.mechs_info = [
                SimpleNamespace(relevant_tools={"superforcaster"})
            ]
            with patch(
                "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
                return_value=1050,
            ):
                selection = run(b.select())
        assert selection["lane"] == lane
        assert selection["previous_checkpoint"] == 100
        assert selection["is_activity_target_met"] is met
        if lane != "market":
            assert FreshQueue(queue.path).pending == [raw_market()]
        cap_logs.extend(
            str(call)
            for call in b.context.logger.info.call_args_list
            if "reason=trade_cap" in str(call)
        )
    assert len(cap_logs) == int(cap > 0 and count >= cap)


def test_cap_resets_with_checkpoint(tmp_path: Any) -> None:
    """A new staking period resets the durable cap and permits a trade."""
    from packages.valory.skills.staking_abci.rounds import StakingState

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.max_trades = 3
    b.service_staking_state = StakingState.STAKED
    b.ts_checkpoint = 200
    b._compute_activity_status = lambda: done((False, False, 8, 0))
    (tmp_path / "successful_trade_count.txt").write_text(
        json.dumps(dict(count=3, placement_keys=["old"], period_start=100))
    )
    b._fetch_fresh = lambda *args: done([raw_market()])
    b._get_pool_balances = lambda *args: done([100, 100])
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.successful_trade_count = 0
        sd.return_value.previous_checkpoint = 100
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools={"superforcaster"})
        ]
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            assert run(b.select())["lane"] == "market"
    assert (
        json.loads((tmp_path / "successful_trade_count.txt").read_text())["count"] == 0
    )


def test_mech_only_answer_cannot_prepare_bet(tmp_path: Any) -> None:
    """Discard capped responses before reading a sampled bet or pool."""
    b = make(FastDecisionBehaviour, tmp_path)
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.mech_only_mode = True
        result = run(b.evaluate())
    assert result["stake"] == 0
    assert result["reason"] == "mech_only"


@pytest.mark.parametrize("positional", [False, True])
def test_watch_staking_contract_read(tmp_path: Any, positional: bool) -> None:
    """Support the staking reader's default contract performative."""
    from aea.configurations.base import PublicId

    from packages.valory.protocols.contract_api.message import ContractApiMessage

    b = make(FreshWatchBehaviour, tmp_path)
    response = MagicMock()
    response.performative = ContractApiMessage.Performative.RAW_TRANSACTION
    response.raw_transaction.body = {"data": 123}
    b.get_contract_api_response = MagicMock(
        side_effect=lambda *args, **kwargs: done(response)
    )
    args = (ContractApiMessage.Performative.GET_RAW_TRANSACTION,) if positional else ()
    assert run(
        b.contract_interact(
            *args,
            contract_address="staking",
            contract_public_id=PublicId.from_str("valory/service_staking_token:0.1.0"),
            contract_callable="ts_checkpoint",
            data_key="data",
            placeholder="ts_checkpoint",
        )
    )
    assert b.ts_checkpoint == 123
    assert (
        b.get_contract_api_response.call_args.args[0]
        == ContractApiMessage.Performative.GET_RAW_TRANSACTION
    )


@pytest.mark.parametrize("housekeeping", [False, True])
def test_capped_watch_without_offchain(tmp_path: Any, housekeeping: bool) -> None:
    """Keep periodic housekeeping, but never send a capped on-chain request."""
    from packages.valory.skills.staking_abci.rounds import StakingState

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.use_offchain = False
    b.service_staking_state = StakingState.UNSTAKED
    b._compute_activity_status = lambda: done((False, False, 8, 3))
    b.sleep = lambda *args: done(None)
    (tmp_path / "successful_trade_count.txt").write_text(
        json.dumps(dict(count=3, placement_keys=[], period_start=100))
    )
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 0 if housekeeping else 1050
    queue.save()
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.previous_checkpoint = 100
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            assert run(b.select())["lane"] == (
                "housekeeping" if housekeeping else "wait"
            )


def test_watch_without_durable_counter(tmp_path: Any) -> None:
    """Use the consensus count if no durable state is available."""
    b = make(FreshWatchBehaviour, tmp_path)
    (tmp_path / "successful_trade_count.txt").unlink()
    b.context.params.use_offchain = True
    b._compute_activity_status = lambda: done((True, True, 8, 8))
    b.sleep = lambda *args: done(None)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 1050
    queue.save()
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.successful_trade_count = 3
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            assert run(b.select())["lane"] == "wait"
