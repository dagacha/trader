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

from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    read_successful_trade_count_state,
)
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
    context.state.fast_activity_status = None
    context.state.fast_last_poll = 0
    context.state.fast_started_at = 1000
    context.params.mech_marketplace_config.offchain_poll_timeout_seconds = 300
    context.benchmarking_mode.enabled = False
    context.params.use_offchain = True
    context.params.max_trades = 3
    context.params.placement_min_edge = 0.03
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
        outcomeTokenAmounts=[100 * 10**18, 100 * 10**18],
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
        b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        ("https://mech.test", {"0xABC": 1000}, True),
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
    b._get_pool_balances = MagicMock(
        side_effect=lambda *args: done([100 * 10**18, 100 * 10**18])
    )
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
        (0.9, [100 * 10**18, 100 * 10**18], "", 4),
        (None, [100 * 10**18, 100 * 10**18], "mech_response", 0),
        (0.9, None, "live_price_unavailable", 0),
        (0.9, [25 * 10**18, 75 * 10**18], "max_slip", 0),
    ],
)
@pytest.mark.parametrize("fee", [0, 2 * 10**16])
def test_evaluate(
    tmp_path: Any, prediction: Any, balances: Any, reason: Any, stake: Any, fee: int
) -> None:
    """Evaluate a real sampled bet without policy setup or strategy loading."""
    b = make(FastDecisionBehaviour, tmp_path)
    b.context.shared_state = {
        "mech_latency": {
            "uuid": {"market": "market", "mech": "0xselected", "latency_s": 31}
        }
    }
    raw = raw_market()
    raw["fee"] = fee
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
    if stake:
        from packages.valory.skills.fresh_watch_abci.strategy import buy_amount

        investment = int(stake * 10**18)
        expected_price = investment / buy_amount(
            investment, balances, result["side"], fee
        )
        assert result["executed_price"] == expected_price
        assert result["price"] == expected_price
        assert result["request_price"] == 0.5
        assert result["edge"] == pytest.approx(result["p_hat"] - expected_price)
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
        "outside_recent",
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
    if case in {"outside", "outside_recent"}:
        b.context.params.fast = replace(FastConfig(), active_window_utc="01:00-02:00")
    if case == "outside":
        queue.last_housekeeping = 1050 - FastConfig().housekeeping_interval_s
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
        None if case == "no_quote" else [100 * 10**18, 100 * 10**18]
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
            b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    restarted._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    decision._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps({"p_hat": 0.9}),
        }[key]
        assert run(b._calc_buy_amount()) is allowed


@pytest.mark.parametrize("prepared,tokens", [(True, 100), (False, 0)])
def test_placement_log(tmp_path: Any, prepared: bool, tokens: int) -> None:
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
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "fast_decision": json.dumps(dict(p_hat=0.875, stake=4, reason="")),
            "request_prices": [0.5, 0.5],
        }[key]
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ):
            run(
                b.finish_behaviour(
                    BetPlacementPayload("agent", tx_hash="tx" if prepared else None)
                )
            )
    args = b.context.logger.info.call_args.args
    assert args[0] == "[FAST_DECISION] %s"
    result = json.loads(args[1])
    assert result["age_at_bet"] == 100
    assert result["prepared"] is prepared
    assert "placed" not in result
    assert result["stake"] == 4


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
        b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
    """Keep housekeeping and allow capped on-chain requests for unmet activity."""
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
                "housekeeping" if housekeeping else "mech_only"
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


@pytest.mark.parametrize(
    "refresh", ["interval", "checkpoint", "trade", "consensus_trade"]
)
def test_activity_status_cached_across_watch_rounds(
    tmp_path: Any, refresh: str
) -> None:
    """Reuse activity reads across watch instances and refresh after expiry."""
    b = make(FreshWatchBehaviour, tmp_path)
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData

    b.context.state.synchronized_data = SynchronizedData(
        AbciAppDB(setup_data={"previous_checkpoint": [100]})
    )
    b._select = lambda *args: done({"lane": "wait"})
    reads = MagicMock(side_effect=lambda: done((False, False, 8, 3)))
    b._compute_activity_status = reads
    with (
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.monotonic",
            return_value=10,
        ) as clock,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.read_successful_trade_count_state",
            wraps=read_successful_trade_count_state,
        ) as counts,
    ):
        assert run(b.select())["activity_completed"] == 3
        assert counts.call_count == 1
        restarted: Any = FreshWatchBehaviour(name="next", skill_context=b.context)
        restarted._compute_activity_status = reads
        restarted._select = b._select
        clock.return_value = 27
        assert run(restarted.select())["previous_checkpoint"] == 100
        assert reads.call_count == 1
        if refresh == "checkpoint":
            b.context.state.synchronized_data = (
                b.context.state.synchronized_data.update(previous_checkpoint=200)
            )
            restarted.ts_checkpoint = 200
            (tmp_path / "successful_trade_count.txt").write_text(
                json.dumps(dict(count=1, placement_keys=[], period_start=200))
            )
        elif refresh == "trade":
            (tmp_path / "successful_trade_count.txt").write_text(
                json.dumps(dict(count=1, placement_keys=[], period_start=100))
            )
        elif refresh == "consensus_trade":
            (tmp_path / "successful_trade_count.txt").unlink()
            b.context.state.synchronized_data = (
                b.context.state.synchronized_data.update(successful_trade_count=1)
            )
        else:
            clock.return_value = 10 + FastConfig().housekeeping_interval_s
            restarted.ts_checkpoint = 100
        counts.reset_mock()
        run(restarted.select())
        assert counts.call_count == 1
        assert reads.call_count == 2
        if refresh == "checkpoint":
            state = json.loads((tmp_path / "successful_trade_count.txt").read_text())
            assert state["count"] == 1
            assert state["period_start"] == 200


@pytest.mark.parametrize(
    "tokens,success,submitter,reason",
    [
        (0, True, "placement", "quote"),
        (100, False, "placement", "quote"),
        (50, True, "placement", "max_price"),
        (70, True, "placement", "max_slip"),
        (100, True, None, "balance"),
        (100, True, "placement", "edge_recheck"),
    ],
)
def test_placement_failure_reason(
    tmp_path: Any, tokens: int, success: bool, submitter: Any, reason: str
) -> None:
    """Name the failed check and retain the requested stake in the log."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.quoted_tokens = tokens
    with (
        patch.object(
            BetPlacementBehaviour, "_calc_buy_amount", return_value=done(success)
        ),
        patch.object(
            BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(type(b), "sampled_bet", new_callable=PropertyMock) as sampled,
    ):
        sampled.return_value.creationTimestamp = 1000
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps(
                dict(p_hat=0.815, stake=4, reason="", price=0.6, edge=0.215)
            ),
        }[key]
        if submitter:
            run(b._calc_buy_amount())
        run(b.finish_behaviour(BetPlacementPayload("agent", tx_submitter=submitter)))
    result = json.loads(b.context.logger.info.call_args.args[1])
    assert result["reason"] == reason
    assert result["prepared"] is False
    assert result["request_price"] == 0.5
    assert result["stake"] == 4
    assert result["executed_price"] == (
        50 / tokens if tokens and success and submitter else None
    )
    if result["executed_price"] is not None:
        assert result["edge"] == pytest.approx(0.815 - result["executed_price"])
    else:
        assert result["price"] is None
        assert result["edge"] is None


def test_placement_quote_retry(tmp_path: Any) -> None:
    """Clear an old execution price when a later quote read fails."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.quoted_tokens = 100
    with (
        patch.object(
            BetPlacementBehaviour,
            "_calc_buy_amount",
            side_effect=[done(True), done(False)],
        ),
        patch.object(
            BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
        ),
        patch.object(type(b), "sampled_bet", new_callable=PropertyMock) as sampled,
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sampled.return_value.creationTimestamp = 1000
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps(dict(p_hat=0.815, stake=4, reason="")),
        }[key]
        assert run(b._calc_buy_amount())
        assert not run(b._calc_buy_amount())
        run(b.finish_behaviour(BetPlacementPayload("agent", tx_submitter="placement")))
    result = json.loads(b.context.logger.info.call_args.args[1])
    assert result["reason"] == "quote"
    assert result["executed_price"] is None
    assert result["price_ceiling"] is None
    assert result["min_tokens_out"] is None


@pytest.mark.parametrize("lane", ["wait", "market", "housekeeping"])
def test_staked_activity_cache_after_consensus(tmp_path: Any, lane: str) -> None:
    """Reuse the staked period after consensus replaces an old checkpoint."""
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
    from packages.valory.skills.fresh_watch_abci.rounds import (
        Event,
        FreshWatchRound,
        SelectionPayload,
    )
    from packages.valory.skills.staking_abci.rounds import StakingState

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.state.synchronized_data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                previous_checkpoint=[100],
            )
        )
    )
    reads = MagicMock(side_effect=lambda: done((False, False, 8, 3)))
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.monotonic"
    ) as clock:
        for now in (10, 27, 44):
            clock.return_value = now
            b.service_staking_state = StakingState.STAKED
            b.ts_checkpoint = 200
            b._compute_activity_status = reads
            b._select = lambda *args: done({"lane": lane})
            selection = run(b.select())
            assert selection["previous_checkpoint"] == 200
            round_ = FreshWatchRound(b.context.state.synchronized_data, MagicMock())
            round_.process_payload(SelectionPayload("agent", json.dumps(selection)))
            outcome = round_.end_block()
            assert outcome is not None
            data, event = outcome
            assert (
                event
                == {
                    "wait": Event.WAIT,
                    "market": Event.MARKET,
                    "housekeeping": Event.HOUSEKEEPING,
                }[lane]
            )
            assert isinstance(data, SynchronizedData)
            assert data.previous_checkpoint == 200
            b.context.state.synchronized_data = data
            b = FreshWatchBehaviour(name="next", skill_context=b.context)
    assert reads.call_count == 1


@pytest.mark.parametrize("bound", ["max_price", "max_slip"])
@pytest.mark.parametrize("margin", [0.0, 0.01])
def test_decided_amount_passes_placement(
    tmp_path: Any, bound: str, margin: float
) -> None:
    """Accept the consensus amount, then refuse it after an adverse small buy."""
    from dataclasses import asdict, replace

    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
    from packages.valory.skills.fresh_watch_abci.rounds import (
        Event,
        FastDecisionRound,
        SelectionPayload,
    )
    from packages.valory.skills.fresh_watch_abci.strategy import buy_amount, decide

    cfg = replace(
        FastConfig(),
        prior_weight=1,
        price_margin=margin,
        kelly_bankroll=1000000,
        max_price=0.675 if bound == "max_price" else 0.9,
    )
    requested = [0.65, 0.35] if bound == "max_price" else [0.475, 0.525]
    reserves, fee = [35 * 10**18, 65 * 10**18], 2 * 10**16
    decision = decide("still", 0.99, reserves, requested, cfg, fee)
    assert decision.stake >= cfg.min_stake
    limit = cfg.max_price if bound == "max_price" else requested[0] + cfg.max_slip
    assert decision.price == pytest.approx(limit - margin, abs=1e-10)
    if margin:
        unprotected = decide(
            "still", 0.99, reserves, requested, replace(cfg, price_margin=0), fee
        )
        assert decision.stake < unprotected.stake
    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                request_prices=[requested],
            )
        )
    )
    round_ = FastDecisionRound(data, MagicMock())
    round_.process_payload(SelectionPayload("agent", json.dumps(asdict(decision))))
    outcome = round_.end_block()
    assert outcome is not None
    synced, event = outcome
    assert event == Event.BET
    assert isinstance(synced, SynchronizedData)
    assert synced.bet_amount == int(decision.stake * 10**18)
    assert synced.vote is not None
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = cfg
    from packages.valory.protocols.contract_api.message import ContractApiMessage

    response = MagicMock()
    response.performative = ContractApiMessage.Performative.RAW_TRANSACTION
    response.raw_transaction.body = {
        "amount": buy_amount(synced.bet_amount, reserves, synced.vote, fee)
    }
    b.get_contract_api_response = MagicMock(side_effect=lambda **kwargs: done(response))
    b.context.params.slippage = 0.01
    with (
        patch.object(
            type(b), "synchronized_data", new_callable=PropertyMock, return_value=synced
        ),
        patch.object(
            type(b),
            "sampled_bet",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id="market", creationTimestamp=1000),
        ),
    ):
        allowed = run(b._calc_buy_amount())
        assert allowed is bool(margin)
        if not margin:
            assert b._placement_failure == "edge_recheck"
        assert synced.bet_amount / b.quoted_tokens == decision.price
        assert (
            b.get_contract_api_response.call_args.kwargs["contract_callable"]
            == "calc_buy_amount"
        )
        assert (
            b.get_contract_api_response.call_args.kwargs["investment_amount"]
            == synced.bet_amount
        )

        other_buy = 10**15
        other_tokens = buy_amount(other_buy, reserves, synced.vote, fee)
        net = other_buy - other_buy * fee // 10**18
        moved = [reserve + net for reserve in reserves]
        moved[synced.vote] -= other_tokens
        response.raw_transaction.body = {
            "amount": buy_amount(synced.bet_amount, moved, synced.vote, fee)
        }
        run(b._calc_buy_amount())
        if margin:
            assert limit - margin < synced.bet_amount / b.quoted_tokens < limit
            assert run(b._calc_buy_amount())
        else:
            from packages.valory.skills.decision_maker_abci.payloads import (
                BetPlacementPayload,
            )

            assert synced.bet_amount / b.quoted_tokens > limit
            # Run the preparation path: the guard must stop before building a buy.
            b.wait_for_condition_with_sleep = lambda *args: done(None)
            b._build_buy_tx = MagicMock()
            tx_hash = run(b._prepare_safe_tx())
            assert tx_hash is None
            b._build_buy_tx.assert_not_called()
            assert b._placement_failure == bound
            with (
                patch.object(
                    BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
                ),
                patch.object(
                    type(b), "sampled_bet", new_callable=PropertyMock
                ) as sampled,
            ):
                sampled.return_value.creationTimestamp = 1000
                run(
                    b.finish_behaviour(
                        BetPlacementPayload("agent", "placement", tx_hash)
                    )
                )
            logged = json.loads(b.context.logger.info.call_args.args[1])
            assert logged["prepared"] is False
            assert logged["reason"] == bound


@pytest.mark.parametrize("missing_prediction", [False, True])
def test_placement_edge_recheck_log(tmp_path: Any, missing_prediction: bool) -> None:
    """Name an edge abort after a real contract quote passes the fast bounds."""
    from packages.valory.protocols.contract_api.message import ContractApiMessage
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.is_running_on_polymarket = False
    b.context.params.placement_min_edge = 0.03
    b.context.params.slippage = 0.01
    b.wait_for_condition_with_sleep = lambda *args: done(None)
    response = MagicMock()
    response.performative = ContractApiMessage.Performative.RAW_TRANSACTION
    response.raw_transaction.body = {"amount": 100}
    b.get_contract_api_response = lambda **kwargs: done(response)
    b._build_buy_tx = MagicMock()
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            type(b),
            "sampled_bet",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id="market", creationTimestamp=1000),
        ),
        patch.object(
            BetPlacementBehaviour,
            "finish_behaviour",
            side_effect=lambda payload: done(None),
        ),
    ):
        sd.return_value.bet_amount = 50
        sd.return_value.vote = 0
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps(dict(p_hat=0.52, stake=4, reason="")),
        }[key]
        if missing_prediction:
            b._get_decision_prediction = lambda: None
        tx_hash = run(b._prepare_safe_tx())
        assert tx_hash is None
        b._build_buy_tx.assert_not_called()
        run(b.finish_behaviour(BetPlacementPayload("agent", "placement", tx_hash)))
    logged = json.loads(b.context.logger.info.call_args.args[1])
    assert logged["reason"] == "edge_recheck"
    assert logged["prepared"] is False
    assert logged["executed_price"] == 0.5


@pytest.mark.parametrize("benchmarking", [False, True])
def test_placement_reused_before_quote(tmp_path: Any, benchmarking: bool) -> None:
    """Clear a prior quote before a balance skip or a simulated placement."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b._placement_failure = "max_slip"
    b._executed_price = 0.8
    b._price_ceiling = 0.85
    b._min_tokens_out = 100
    b.context.benchmarking_mode.enabled = benchmarking
    payload = BetPlacementPayload("agent", None, None, benchmarking)

    def placement(self: Any) -> Any:
        yield from self.finish_behaviour(payload)

    with (
        patch.object(BetPlacementBehaviour, "async_act", placement),
        patch.object(
            BetPlacementBehaviour,
            "finish_behaviour",
            side_effect=lambda payload: done(None),
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            type(b),
            "sampled_bet",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id="market", creationTimestamp=1000),
        ),
    ):
        sd.return_value.vote = 0
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps(dict(p_hat=0.815, stake=4, reason="")),
        }[key]
        run(b.async_act())
    logged = json.loads(b.context.logger.info.call_args.args[1])
    assert logged["reason"] == ("benchmarking" if benchmarking else "balance")
    assert logged["prepared"] is (None if benchmarking else False)
    assert logged["executed_price"] is None
    assert logged["price_ceiling"] is None
    assert logged["min_tokens_out"] is None
    assert logged["price"] is None
    assert logged["edge"] is None


def test_all_penalized_tools_do_not_idle(tmp_path: Any) -> None:
    """Keep the queued market when each offered tool endpoint has a penalty."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast_dynamic_mech = True
    b.context.params.use_offchain = True
    b.context.state.penalized_mechs = {"0xabc": 1000}
    b.sleep = lambda delay: done(None)
    b.context.state.fast_background_running = True
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [raw_market()]
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
                http_url="https://mech.test",
                relevant_tools={"superforcaster"},
            )
        ]
        assert run(b.select())["lane"] == "market"
    assert not any(
        call.args[0] == "Neither fast prediction tool is available."
        for call in b.context.logger.warning.call_args_list
    )


@pytest.mark.parametrize(
    "max_price,max_slip,p_side,min_edge,ceiling,min_out",
    [
        (0.6, 0.3, 0.9, 0.03, 0.6, 84),
        (0.9, 0.1, 0.9, 0.03, 0.6, 84),
        (0.9, 0.3, 0.7, 0.1, 0.6, 84),
        (0.9, 0.3, 0.60001, 0.1, 0.50001, 100),
    ],
)
def test_ceiling_min_out(
    tmp_path: Any,
    max_price: float,
    max_slip: float,
    p_side: float,
    min_edge: float,
    ceiling: float,
    min_out: int,
) -> None:
    """Use each price bound, round up in wei, and retain the quoted-token cap."""
    from dataclasses import replace

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = replace(
        FastConfig(), max_price=max_price, max_slip=max_slip
    )
    b.context.params.placement_min_edge = min_edge
    b.quoted_tokens = 100
    with patch.object(
        type(b), "investment_amount", new_callable=PropertyMock, return_value=50
    ):
        result = b._ceiling_min_out(p_side, 0.5)
    assert result == (ceiling, min_out)
    assert min_out <= b.quoted_tokens


@pytest.mark.parametrize("p_side", [0.52, 0.53])
def test_ceiling_min_out_rejects_quote(tmp_path: Any, p_side: float) -> None:
    """Refuse a ceiling at or below the current quoted price."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.placement_min_edge = 0.03
    b.quoted_tokens = 100
    with patch.object(
        type(b), "investment_amount", new_callable=PropertyMock, return_value=50
    ):
        assert b._ceiling_min_out(p_side, 0.5) is None


@pytest.mark.parametrize("side", [0, 1])
def test_ceiling_replaces_quote_slippage(tmp_path: Any, side: int) -> None:
    """Use calibrated side probability and the enforced placement edge."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.placement_min_edge = 0.03
    b.quoted_tokens = 100
    b.buy_amount = 97
    with (
        patch.object(
            BetPlacementBehaviour, "_calc_buy_amount", return_value=done(True)
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.bet_amount = 50
        sd.return_value.vote = side
        sd.return_value.db.get_strict.side_effect = lambda key: {
            "request_prices": [0.5, 0.5],
            "fast_decision": json.dumps({"p_hat": 0.6 if side == 0 else 0.4}),
        }[key]
        assert run(b._calc_buy_amount())
    assert b.buy_amount == 88
    assert b._price_ceiling == 0.57


@pytest.mark.parametrize(
    "result,error,allowed",
    [
        (123456, None, True),
        (0, None, False),
        (None, "execution reverted: GS013", False),
        (None, "VM execution error: GS013", False),
        (None, None, True),
        (None, "RPC connection timed out", True),
    ],
)
def test_preflight_safe_call(
    tmp_path: Any, result: Any, error: Any, allowed: bool
) -> None:
    """Simulate the exact Safe call with this agent's approved owner signature."""
    from eth_abi import decode

    from packages.valory.protocols.ledger_api import LedgerApiMessage

    b = make(FastBetPlacementBehaviour, tmp_path)
    owner = "0x" + "11" * 20
    safe = "0x" + "22" * 20
    target = "0x" + "33" * 20
    b.context.agent_address = owner
    b.context.params.multisend_address = target
    b.context.params.mech_chain_id = "gnosis"
    b.multisend_data = b"batch"
    from aea_ledger_ethereum import EthereumApi

    ledger = object.__new__(EthereumApi)
    ledger._api = MagicMock()
    if error:
        ledger.api.eth.estimate_gas.side_effect = ValueError(error)
    else:
        ledger.api.eth.estimate_gas.return_value = result

    def estimate(transaction: Any, chain_id: str) -> Any:
        if result is None and error is None:
            # The bounded wait expired without a reply, so the helper
            # swallows TimeoutException and returns None to the caller.
            return done(None)
        try:
            body = ledger.get_state(
                "estimate_gas",
                transaction=transaction,
                raise_on_try=True,
            )
        except ValueError as exc:
            response = LedgerApiMessage(
                performative=LedgerApiMessage.Performative.ERROR,  # type: ignore[arg-type]
                code=500,
                message=str(exc),
                data=b"",
            )
        else:
            response = LedgerApiMessage(
                performative=LedgerApiMessage.Performative.STATE,  # type: ignore[arg-type]
                ledger_id="ethereum",
                state=LedgerApiMessage.State("ethereum", body),
            )
        return done(response)

    b._estimate_gas = MagicMock(side_effect=estimate)
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(type(b), "txs_value", new_callable=PropertyMock, return_value=7),
    ):
        sd.return_value.safe_contract_address = safe
        assert run(b._preflight_ok()) is allowed
    call = b._estimate_gas.call_args
    transaction = call.args[0] if call.args else call.kwargs["transaction"]
    chain_id = call.args[1] if len(call.args) > 1 else call.kwargs["chain_id"]
    assert chain_id == "gnosis"
    assert transaction["from"] == owner
    assert transaction["to"] == safe
    data = bytes.fromhex(transaction["data"][2:])
    assert data[:4].hex() == "6a761202"
    values = decode(
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
        data[4:],
    )
    assert values[:5] == (target, 7, b"batch", 1, 0)
    assert values[5:9] == (0, 0, "0x" + "00" * 20, "0x" + "00" * 20)
    assert values[9] == bytes.fromhex(owner[2:]).rjust(32, b"\0") + bytes(32) + b"\1"
    assert b.context.logger.warning.call_count == int(allowed and result is None)


def test_preflight_checksums_lowercase_safe_address(tmp_path: Any) -> None:
    """Normalise a lowercase Safe address with hex letters before estimating."""
    from eth_utils import to_checksum_address  # type: ignore[import-not-found]

    from packages.valory.protocols.ledger_api import LedgerApiMessage

    b = make(FastBetPlacementBehaviour, tmp_path)
    owner = to_checksum_address("0x" + "ab" * 20)
    # A valid lowercase Safe address with hex letters: Web3 rejects it before
    # sending estimate_gas, so the preflight must checksum it first.
    lowercase_safe = "0x" + "ab" * 20
    checksum_safe = to_checksum_address(lowercase_safe)
    assert lowercase_safe != checksum_safe
    b.context.agent_address = owner
    b.context.params.multisend_address = "0x" + "33" * 20
    b.context.params.mech_chain_id = "gnosis"
    b.multisend_data = b"batch"
    captured: Any = {}

    def estimate(transaction: Any, chain_id: str) -> Any:
        captured["tx"] = transaction
        return done(
            LedgerApiMessage(
                performative=LedgerApiMessage.Performative.STATE,  # type: ignore[arg-type]
                ledger_id="ethereum",
                state=LedgerApiMessage.State(
                    "ethereum", {"estimate_gas_result": 42000}
                ),
            )
        )

    b._estimate_gas = MagicMock(side_effect=estimate)
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(type(b), "txs_value", new_callable=PropertyMock, return_value=0),
    ):
        sd.return_value.safe_contract_address = lowercase_safe
        assert run(b._preflight_ok()) is True
    assert captured["tx"]["to"] == checksum_safe
    assert captured["tx"]["to"] != lowercase_safe


def test_estimate_gas_bounded_wait_returns_none_on_deadline(tmp_path: Any) -> None:
    """Return None when the bounded response wait expires with no reply."""
    from packages.valory.skills.fresh_watch_abci import behaviours as fw_behaviours

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.agent_address = "0x" + "ab" * 20
    b.context.default_ledger_id = "ethereum"
    dialogue = MagicMock()
    dialogue.dialogue_label.dialogue_reference = ("nonce", "")
    b.context.ledger_api_dialogues.create.return_value = (MagicMock(), dialogue)
    b.context.requests.request_id_to_callback = {}
    b.sleep = lambda seconds: iter([None])
    with (
        patch.object(fw_behaviours, "_PREFLIGHT_TIMEOUT_S", 0.01),
        patch("time.monotonic", side_effect=[0.0, 0.0, 0.02]),
    ):
        gen = b._estimate_gas({"from": "0x0", "to": "0x0", "data": "0x"}, "gnosis")
        # Advance one scheduler tick, then pass the response deadline.
        next(gen)
        with pytest.raises(StopIteration) as exc_info:
            gen.send(None)
    assert exc_info.value.value is None
    b.context.outbox.put_message.assert_called_once()
    b.context.requests.request_id_to_callback.pop("nonce")(None, b)
    assert b.context.requests.request_id_to_callback == {}


def test_estimate_gas_returns_ledger_response(tmp_path: Any) -> None:
    """Return the ledger reply when it arrives before the preflight deadline."""
    from packages.valory.protocols.ledger_api import LedgerApiMessage

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.agent_address = "0x" + "ab" * 20
    b.context.default_ledger_id = "ethereum"
    dialogue = MagicMock()
    dialogue.dialogue_label.dialogue_reference = ("nonce", "")
    b.context.ledger_api_dialogues.create.return_value = (MagicMock(), dialogue)
    b.context.requests.request_id_to_callback = {}
    b.sleep = lambda seconds: iter([None])
    reply = LedgerApiMessage(
        performative=LedgerApiMessage.Performative.STATE,  # type: ignore[arg-type]
        ledger_id="ethereum",
        state=LedgerApiMessage.State("ethereum", {"estimate_gas_result": 21000}),
    )
    gen = b._estimate_gas({"from": "0x0", "to": "0x0", "data": "0x"}, "gnosis")
    next(gen)
    b.context.requests.request_id_to_callback.pop("nonce")(reply, b)
    with pytest.raises(StopIteration) as exc_info:
        gen.send(None)
    assert exc_info.value.value is reply
    assert b.context.requests.request_id_to_callback == {}


@pytest.mark.parametrize(
    "prepared,allowed", [(False, True), (True, False), (True, True)]
)
def test_preflight_gates_prepared_tx(
    tmp_path: Any, prepared: bool, allowed: bool
) -> None:
    """Return no transaction after a failed preflight and record the failure."""
    from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
        BetPlacementBehaviour,
    )
    from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

    b = make(FastBetPlacementBehaviour, tmp_path)
    b._preflight_ok = MagicMock(side_effect=lambda: done(allowed))
    with patch.object(
        BetPlacementBehaviour,
        "_prepare_safe_tx",
        side_effect=lambda: done("tx" if prepared else None),
    ):
        assert run(b._prepare_safe_tx()) == ("tx" if prepared and allowed else None)
    assert b._preflight_ok.call_count == int(prepared)
    if prepared and not allowed:
        assert b._placement_failure == "preflight_revert"
        b._price_ceiling = 0.57
        b._min_tokens_out = 88
        with (
            patch.object(
                BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
            ),
            patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
            patch.object(type(b), "sampled_bet", new_callable=PropertyMock) as sampled,
        ):
            sampled.return_value.creationTimestamp = 1000
            sd.return_value.vote = 0
            sd.return_value.db.get_strict.side_effect = lambda key: {
                "request_prices": [0.5, 0.5],
                "fast_decision": json.dumps({"p_hat": 0.6}),
            }[key]
            run(b.finish_behaviour(BetPlacementPayload("agent", "placement", None)))
        b.context.logger.info.assert_called_once()
        logged = json.loads(b.context.logger.info.call_args.args[1])
        assert logged["reason"] == "preflight_revert"
        assert logged["prepared"] is False
        assert logged["price_ceiling"] == 0.57
        assert logged["min_tokens_out"] == 88


def test_ceiling_min_out_wei_precision(tmp_path: Any) -> None:
    """Round a fractional wei upward without a float conversion."""
    from dataclasses import replace

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = replace(FastConfig(), max_price=0.6, max_slip=0.3)
    b.quoted_tokens = 2 * 10**18
    with patch.object(
        type(b), "investment_amount", new_callable=PropertyMock, return_value=10**18
    ):
        assert b._ceiling_min_out(0.9, 0.5) == (0.6, 1666666666666666667)


def test_lightning_select_without_mech(tmp_path: Any) -> None:
    """Send qualified evidence-backed markets to a direct YES decision."""
    from packages.valory.skills.fresh_watch_abci.lightning import (
        BaseRateCache,
        LightningConfig,
    )

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=True, lightning_base_rate_min_n=1
    )
    b.sleep = lambda delay: done(None)
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    cache.update(
        [{**raw_market(), "payouts": [1, 0]}], 1050, b.context.params.lightning
    )
    b._fetch_fresh = lambda cursor, now: done([raw_market()])
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ),
    ):
        sd.return_value.mechs_info = []
        selection = run(b.select())
    assert selection["lane"] == "lightning_decision"
    assert selection["lightning_probability"] == 1
    assert selection["lightning_rule"] == "still_hold_phrase"
    assert "mech_tool" not in selection


def test_lightning_evaluate_direct_probability(tmp_path: Any) -> None:
    """Use base-rate YES sizing without reading a mech response."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, max_price=0.93)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    values = dict(
        requested_at=1050,
        request_prices=[0.5, 0.5],
        lightning_probability=0.95,
        lightning_rule="still_hold_phrase",
        lightning_preview_title=None,
        lightning_preview_request=False,
    )
    b._get_decision_prediction = MagicMock(
        side_effect=AssertionError("Direct bets must not read a mech response.")
    )
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        result = run(b.evaluate())
    assert result["p_hat"] == 0.95
    assert result["side"] == 0
    assert result["stake"] > 0
    b._get_decision_prediction.assert_not_called()


def test_lightning_refresh_complete_pages_and_failure(tmp_path: Any) -> None:
    """Refresh resolved evidence outside the window and preserve it on errors."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import (
        BaseRateCache,
        LightningConfig,
    )

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast = replace(
        b.context.params.fast, active_window_utc="00:00-00:01"
    )
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=True, lightning_base_rate_min_n=1
    )
    rows = [{**raw_market(), "payouts": [1, 0]}]
    b.get_http_response = lambda **kwargs: done(None)
    b.context.omen_subgraph.process_response.return_value = rows
    run(b._refresh_base_rate(1050))
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    assert cache.usable(1050, b.context.params.lightning) == 1
    before = cache.path.read_text()
    b.context.omen_subgraph.process_response.return_value = None
    run(b._refresh_base_rate(1050 + 86400))
    assert cache.path.read_text() == before


def test_preview_request_and_cached_market_decision(tmp_path: Any) -> None:
    """Request a proposed title once, then reuse its exact answer on the market."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    store = PreviewStore(tmp_path / "lightning_preview.json")
    title = "Will event X happen?"
    store.ingest({"result": {"questions": [{"question": title}]}}, 1040, 1050)
    b._fetch_fresh = lambda *args: done([])
    b.preview_poll = lambda now: done(None)
    b.sleep = lambda delay: done(None)
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
        assert selection["lane"] == "preview_request"
        assert selection["lightning_preview_title"] == title
        store = PreviewStore(store.path)
        store.answer(title, 0.99, 1060)
        queue = FreshQueue(tmp_path / "fresh_watch.json")
        queue.pending = [{**raw_market(), "title": title}]
        queue.save()
        selection = run(b.select())
    assert selection["lane"] == "lightning_decision"
    assert selection["lightning_preview_title"] == title
    assert selection["lightning_preview_probability"] == 0.99
    assert "lightning_probability" not in selection


def test_preview_answer_stored_without_pool_read(tmp_path: Any) -> None:
    """A preview response can only update the cache before a market exists."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    title = "Will event X happen?"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": title}]}}, 1040, 1050)
    store.reserve(title, 1050, "2026-10-29", 15)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**{**raw, "title": title}, market="omen_subgraph")
    values = dict(
        requested_at=1050,
        request_prices=[0.5, 0.5],
        lightning_preview_request=True,
        lightning_preview_title=title,
    )
    b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.99)
    b._get_pool_balances = MagicMock(
        side_effect=AssertionError("No market exists for this answer.")
    )
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
        result = run(b.evaluate())
    assert result["stake"] == 0
    assert result["reason"] == "preview_answer_cached"
    entry = PreviewStore(store.path).match(title, 1100)
    assert entry is not None
    assert entry["p_yes"] == 0.99
    b._get_pool_balances.assert_not_called()


def test_lightning_discovery_uses_chain_and_preview(tmp_path: Any) -> None:
    """Use block discovery and poll previews before the subgraph fallback."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_chain_detect=True, lightning_preview=True
    )
    b.chain_markets = MagicMock(side_effect=lambda now: done([raw_market()]))
    b.preview_poll = MagicMock(side_effect=lambda now: done(None))
    b._fetch_fresh = MagicMock(
        side_effect=AssertionError("Healthy chain mode must not poll the subgraph.")
    )
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    assert run(b._poll_markets(queue, 1050)) == [raw_market()]
    b.chain_markets.assert_called_once_with(1050)
    b.preview_poll.assert_not_called()
    b._fetch_fresh.assert_not_called()


def test_lightning_placement_respects_template_price_cap(tmp_path: Any) -> None:
    """Enforce the explicit template cap through the existing min-out helper."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = replace(b.context.params.fast, max_price=0.93)
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=True, lightning_still_hold_max_price=0.8
    )
    values = dict(lightning_probability=0.95, lightning_rule="still_hold_phrase")
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            type(b), "investment_amount", new_callable=PropertyMock, return_value=10**18
        ),
    ):
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        b.quoted_tokens = 2 * 10**18
        ceiling, min_out = b._ceiling_min_out(0.95, 0.7)
    assert ceiling == 0.8
    assert min_out == 1250000000000000000


def test_lightning_prior_uses_available_base_rate(tmp_path: Any) -> None:
    """Use cached evidence ahead of the configured .95 fallback prior."""
    from packages.valory.skills.fresh_watch_abci.lightning import (
        BaseRateCache,
        LightningConfig,
    )

    b = make(FastDecisionBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, still_hold_prior=0.95)
    b.context.params.lightning = cfg
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    rows = [
        {**raw_market(), "id": str(n), "payouts": [1, 0] if n < 97 else [0, 1]}
        for n in range(100)
    ]
    cache.update(rows, 1050, cfg)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    values = dict(requested_at=1050, request_prices=[0.5, 0.5])
    b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.99)
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        result = run(b.evaluate())
    assert result["p_hat"] == 0.98


def test_lightning_failed_refresh_attempt_once_per_day(tmp_path: Any) -> None:
    """Persist the daily attempt before HTTP I/O without replacing evidence."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast = replace(
        b.context.params.fast, active_window_utc="00:00-00:01"
    )
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    b.get_http_response = MagicMock(side_effect=lambda **kwargs: done(None))
    b.context.omen_subgraph.process_response.return_value = None
    run(b._refresh_base_rate(1050))
    run(b._refresh_base_rate(1051))
    assert b.get_http_response.call_count == 1


def test_chain_discovery_does_not_wait_for_preview_ipfs(tmp_path: Any) -> None:
    """Return fresh chain markets before any slower preview I/O."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_chain_detect=True, lightning_preview=True
    )
    b.chain_markets = lambda now: done([raw_market()])
    b.preview_poll = MagicMock(
        side_effect=AssertionError("Preview I/O must not delay fresh markets.")
    )
    rows = run(b._poll_markets(FreshQueue(tmp_path / "fresh_watch.json"), 1050))
    assert rows == [raw_market()]
    b.preview_poll.assert_not_called()


def test_preview_normal_mech_metadata_and_cleanup(tmp_path: Any) -> None:
    """Build the normal mech prompt from the synthetic bet, then remove it."""
    from string import Template

    from packages.valory.skills.fresh_watch_abci.behaviours import (
        FastDecisionRequestBehaviour,
    )
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    title = "Will event X happen?"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": title}]}}, 1040, 1050)
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.mechs_info = [
            SimpleNamespace(relevant_tools={"superforcaster"})
        ]
        selection = b._preview_selection(1050)
    assert selection is not None
    request = make(FastDecisionRequestBehaviour, tmp_path)
    request.context.params.lightning = LightningConfig(lightning_preview=True)
    request.context.params.slot_count = 2
    request.context.params.prompt_template = Template("$question [$yes/$no]")
    request.read_bets()
    with patch.object(
        type(request), "synchronized_data", new_callable=PropertyMock
    ) as sd:
        sd.return_value.sampled_bet_index = selection["sampled_bet_index"]
        sd.return_value.mech_tool = selection["mech_tool"]
        sd.return_value.db.get.side_effect = lambda key, default=None: selection.get(
            key, default
        )
        request.setup()
    assert request.metadata["prompt"] == title + " [Yes/No]"
    assert request.metadata["tool"] == "superforcaster"
    assert request.metadata["request_context"] is None
    decision = make(FastDecisionBehaviour, tmp_path)
    decision.context.params.lightning = LightningConfig(lightning_preview=True)
    values = dict(selection)
    values.pop("lane")
    decision.read_bets()
    decision._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.99)
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
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        result = run(decision.evaluate())
    assert result["reason"] == "preview_answer_cached"
    assert json.loads((tmp_path / "multi_bets.json").read_text()) == []


def test_preview_response_after_expiry_does_not_trade(tmp_path: Any) -> None:
    """Discard a late response whose preview no longer exists."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    values = dict(
        requested_at=1050,
        request_prices=[0.5, 0.5],
        lightning_preview_request=True,
        lightning_preview_title=bet.title,
    )
    b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.99)
    b._get_pool_balances = MagicMock(
        side_effect=AssertionError("Expired previews cannot place bets.")
    )
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=2000,
        ),
    ):
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        result = run(b.evaluate())
    assert result["reason"] == "preview_expired"
    assert result["stake"] == 0
    b._get_pool_balances.assert_not_called()


def test_lightning_flags_off_preserve_subgraph_poll(tmp_path: Any) -> None:
    """OFF defaults use only the established fast subgraph discovery."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig()
    b.chain_markets = MagicMock(side_effect=AssertionError("Chain discovery is OFF."))
    b.preview_poll = MagicMock(side_effect=AssertionError("Preview discovery is OFF."))
    b._fetch_fresh = lambda *args: done([raw_market()])
    assert run(b._poll_markets(FreshQueue(tmp_path / "fresh_watch.json"), 1050)) == [
        raw_market()
    ]
    assert b._discovery_period() == FastConfig().poll_s


def test_disabled_lightning_ignores_stale_consensus_fields(tmp_path: Any) -> None:
    """Rollback to OFF defaults uses the normal response even after a preview."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig()
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    values = dict(
        requested_at=1050,
        request_prices=[0.5, 0.5],
        lightning_probability=0.99,
        lightning_preview_probability=0.99,
        lightning_preview_request=True,
        lightning_preview_title=bet.title,
    )
    b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.9)
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
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
        result = run(b.evaluate())
    assert result["p_hat"] == 0.875
    assert result["stake"] > 0
    assert result["p_tool"] == 0.9


@pytest.mark.parametrize(
    "flag",
    [None, "lightning_still_hold", "lightning_chain_detect", "lightning_preview"],
)
def test_lightning_creator_query_uses_enabled_controls(
    tmp_path: Any, flag: Any
) -> None:
    """Each independent lightning flag selects its configured creator list."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    creator = "0x" + "01" * 20
    b = make(FreshWatchBehaviour, tmp_path)
    controls: dict[str, Any] = {"lightning_creators": [creator]}
    if flag:
        controls[flag] = True
    b.context.params.lightning = LightningConfig(**controls)
    b.get_http_response = MagicMock(side_effect=lambda **kwargs: done(None))
    b.context.omen_subgraph.process_response.return_value = []
    assert run(b._fetch_fresh(450, 1050)) == []
    query = json.loads(b.get_http_response.call_args.kwargs["content"])["query"]
    expected = [creator] if flag else FastConfig().creators
    assert "creator_in: " + json.dumps(expected) in query


@pytest.mark.parametrize(
    "flag", ["lightning_still_hold", "lightning_chain_detect", "lightning_preview"]
)
@pytest.mark.parametrize("background", [False, True])
def test_lightning_creator_ingestion_uses_enabled_controls(
    tmp_path: Any, flag: str, background: bool
) -> None:
    """Foreground and background discovery retain custom lightning creators."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    creator = "0x" + "01" * 20
    b = make(FreshMarketPoller if background else FreshWatchBehaviour, tmp_path)
    controls: dict[str, Any] = {"lightning_creators": [creator], flag: True}
    b.context.params.lightning = LightningConfig(**controls)
    b.get_http_response = lambda **kwargs: done(None)
    b.context.omen_subgraph.process_response.return_value = None
    row = {**raw_market(), "creator": creator}
    b._poll_markets = lambda *args: done([row])
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
    b.sleep = lambda delay: done(None)
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
        if background:
            run(b.async_act())
            assert FreshQueue(tmp_path / "fresh_watch.json").pending == [row]
        else:
            selection = run(b._select(False, True))
            assert selection["lane"] == "market"


def test_preview_cleanup_preserves_real_markets(tmp_path: Any) -> None:
    """Remove request records without losing stored market positions."""
    from packages.valory.skills.fresh_watch_abci.behaviours import remove_preview_bets

    b = make(FreshWatchBehaviour, tmp_path)
    raw = raw_market()
    raw.pop("condition")
    b.bets = [
        Bet(**raw, market="omen_subgraph"),
        Bet(**{**raw, "id": "preview:question"}, market="omen_subgraph"),
    ]
    b.store_bets()
    remove_preview_bets(b)
    assert [bet.id for bet in b.bets] == ["market"]
    stored = json.loads((tmp_path / "multi_bets.json").read_text())
    assert [bet["id"] for bet in stored] == ["market"]


def test_base_rate_refresh_reads_all_resolved_pages(tmp_path: Any) -> None:
    """Calculate evidence from the complete paginated resolution sample."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import (
        BaseRateCache,
        LightningConfig,
    )

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast = replace(
        b.context.params.fast, active_window_utc="00:00-00:01"
    )
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    rows = [
        {**raw_market(), "id": str(index), "payouts": [int(index > 0), int(index == 0)]}
        for index in range(1001)
    ]
    b.context.omen_subgraph.process_response.side_effect = [rows[:1000], rows[1000:]]
    b.get_http_response = MagicMock(side_effect=lambda **kwargs: done(None))
    run(b._refresh_base_rate(1050))
    queries = [
        json.loads(call.kwargs["content"])["query"]
        for call in b.get_http_response.call_args_list
    ]
    assert "skip: 0," in queries[0]
    assert "skip: 1000," in queries[1]
    assert all('creationTimestamp_lte: "1050"' in query for query in queries)
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    assert cache.sample == 1001
    assert cache.data["yes"] == 1000
    assert cache.usable(1050, b.context.params.lightning) == 1000 / 1001


@pytest.mark.parametrize(
    "chain_rows,fallback,error",
    [
        ([], None, None),
        (None, [], "rpc"),
        (None, [raw_market()], "timeout"),
        (None, None, None),
    ],
)
def test_lightning_discovery_idle_and_fallback(
    tmp_path: Any, chain_rows: Any, fallback: Any, error: Any
) -> None:
    """Keep healthy chain polling idle and tolerate preview failures in fallback."""
    from packages.valory.skills.abstract_round_abci.behaviour_utils import (
        TimeoutException,
    )
    from packages.valory.skills.fresh_watch_abci.chain_discovery import ChainRPCError
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_chain_detect=True, lightning_preview=True
    )
    b.chain_markets = lambda now: done(chain_rows)

    def preview(now: float) -> Any:
        if error == "rpc":
            raise ChainRPCError("unavailable")
        if error == "timeout":
            raise TimeoutException()
        return (yield from done(None))

    b.preview_poll = preview
    b._fetch_fresh = MagicMock(side_effect=lambda *args: done(fallback))
    result = run(b._poll_markets(FreshQueue(tmp_path / "fresh_watch.json"), 1050))
    assert result == (chain_rows if chain_rows is not None else fallback)
    assert b._fetch_fresh.call_count == int(chain_rows is None)
    assert b.context.logger.warning.call_count == int(error is not None)
    if fallback:
        b.context.logger.info.assert_called_once_with(
            "[LIGHTNING_DETECT] market=%s delay_s=%.3f source=subgraph", "market", 50
        )


@pytest.mark.parametrize("case", ["empty", "no_tool", "capped"])
def test_preview_selection_does_not_reserve_ineligible_requests(
    tmp_path: Any, case: str
) -> None:
    """Do not spend a fee without a title, an eligible tool, and room in the cap."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_preview=True,
        lightning_preview_max_per_night=0 if case == "capped" else 15,
    )
    store = PreviewStore(tmp_path / "lightning_preview.json")
    title = "Will event X happen?"
    if case != "empty":
        store.ingest({"result": {"questions": [{"question": title}]}}, 1040, 1050)
    b._fast_tool = lambda: None if case == "no_tool" else "superforcaster"
    assert b._preview_selection(1050) is None
    latest = PreviewStore(store.path)
    assert latest.counts == {}
    assert all(entry["requested_at"] is None for entry in latest.entries.values())
    assert not (tmp_path / "multi_bets.json").exists()


def test_market_waits_for_its_pending_preview_answer(tmp_path: Any) -> None:
    """Keep the market queued while its existing paid preview waits for an answer."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    b.context.state.fast_background_running = True
    b._get_pool_balances = MagicMock(side_effect=AssertionError("answer is pending"))
    b.sleep = MagicMock(side_effect=lambda delay: done(None))
    row = {**raw_market(), "title": "Will event X happen?"}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [row]
    queue.save()
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": row["title"]}]}}, 1040, 1050)
    store.reserve(row["title"], 1050, "night", 15)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1060,
    ):
        assert run(b._select(False, True)) == {"lane": "wait"}
    assert FreshQueue(queue.path).pending == [row]
    assert FreshQueue(queue.path).asked == {}
    assert PreviewStore(store.path).counts == {"night": 1}
    b._get_pool_balances.assert_not_called()
    b.sleep.assert_called_once_with(1)


@pytest.mark.parametrize("slots,preview", [(2, False), (3, True)])
def test_normal_and_unsupported_requests_keep_their_context(
    tmp_path: Any, slots: int, preview: bool
) -> None:
    """Only a supported preview request can remove normal market context."""
    from string import Template

    from packages.valory.skills.fresh_watch_abci.behaviours import (
        FastDecisionRequestBehaviour,
    )
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionRequestBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    b.context.params.slot_count = slots
    b.context.params.prompt_template = Template("$question [$yes/$no]")
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.mech_tool = "superforcaster"
        sd.return_value.db.get.return_value = preview
        b.setup()
    if slots == 2:
        assert b.metadata["request_context"] == bet.to_request_context()
        assert b.metadata["prompt"] == bet.title + " [Yes/No]"
    else:
        assert b._metadata is None


def evaluate_preview(b: Any, values: Any, now: float) -> Any:
    """Evaluate stored preview consensus with the normal sampled market."""
    raw = {**raw_market(), "title": "Will event X happen?"}
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=now,
        ),
    ):
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        return run(b.evaluate())


@pytest.mark.parametrize("now", [1100, 1950])
def test_cached_preview_decision_uses_the_answer_before_expiry(
    tmp_path: Any, now: float
) -> None:
    """Size a fresh cached answer and reject it at the exact expiry boundary."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    b._get_decision_prediction = MagicMock(
        side_effect=AssertionError("answer is cached")
    )
    b._get_pool_balances = MagicMock(side_effect=lambda *args: done([100 * 10**18] * 2))
    values = dict(
        requested_at=1050, request_prices=[0.5, 0.5], lightning_preview_probability=0.99
    )
    result = evaluate_preview(b, values, now)
    if now < 1950:
        assert result["p_tool"] == 0.99
        assert result["p_hat"] == pytest.approx((0.99 + 0.18) / 2)
        assert result["side"] == 0
        assert result["stake"] > 0
        b._get_pool_balances.assert_called_once()
    else:
        assert result["reason"] == "preview_expired"
        assert result["stake"] == 0
        b._get_pool_balances.assert_not_called()
    b._get_decision_prediction.assert_not_called()


@pytest.mark.parametrize("now", [1100, 2000])
def test_failed_or_expired_preview_answer_cannot_trade(
    tmp_path: Any, now: float
) -> None:
    """Release failed previews to normal requests and report an expired paid orphan."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
    from packages.valory.skills.fresh_watch_abci.preview import PreviewStore

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_preview=True)
    b._get_decision_prediction = lambda: None
    b._get_pool_balances = MagicMock(side_effect=AssertionError("no market to quote"))
    title = "Will event X happen?"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": title}]}}, 1040, 1050)
    store.reserve(title, 1050, "night", 15)
    values = dict(
        requested_at=1050,
        request_prices=[0.5, 0.5],
        lightning_preview_request=True,
        lightning_preview_title=title,
    )
    result = evaluate_preview(b, values, now)
    latest = PreviewStore(store.path)
    assert result["stake"] == 0
    assert latest.match(title, now) is None
    if now == 1100:
        assert result["reason"] == "preview_answer_failed"
        assert latest.entries[title]["failed_at"] == now
    else:
        assert result["reason"] == "preview_expired"
        assert title not in latest.entries
        assert any(
            call.args[-1] == "orphan_fee_loss"
            for call in b.context.logger.info.call_args_list
        )
    assert latest.counts == {"night": 1}
    b._get_pool_balances.assert_not_called()


def test_direct_still_hold_can_buy_above_fast_price_cap(tmp_path: Any) -> None:
    """Use the direct cap at .91 and retain the fast cap for other markets."""
    from dataclasses import replace

    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastDecisionBehaviour, tmp_path)
    b.context.params.fast = replace(FastConfig(), prior_weight=1.0, price_margin=0)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    b._get_pool_balances = lambda *args: done([9 * 10**27, 91 * 10**27])
    b._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.95)
    values = dict(
        requested_at=1050,
        request_prices=[0.91, 0.09],
        lightning_probability=0.95,
        lightning_rule="still_hold_phrase",
    )
    raw = raw_market()
    raw.pop("condition")
    bet = Bet(**raw, market="omen_subgraph")
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
        direct = run(b.evaluate())
        assert direct["stake"] > 0
        assert direct["price"] == pytest.approx(0.91)
        assert direct["p_hat"] == 0.95
        values.pop("lightning_probability")
        values.pop("lightning_rule")
        bet.title = "Will event X happen?"
        ordinary = run(b.evaluate())
    assert ordinary["stake"] == 0
    assert ordinary["reason"] == "max_price"


def test_direct_execution_uses_direct_cap_and_retains_edge_and_slip(
    tmp_path: Any,
) -> None:
    """Replace only the direct price cap and retain each execution bound."""
    from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig

    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_still_hold=True)
    values = dict(lightning_probability=0.95, lightning_rule="still_hold_phrase")
    b.quoted_tokens = 2 * 10**18
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            type(b), "investment_amount", new_callable=PropertyMock, return_value=10**18
        ),
    ):
        sd.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        assert b._execution_max_price() == 0.93
        assert b._ceiling_min_out(0.95, 0.91) == (0.92, 1086956521739130435)
        assert b._ceiling_min_out(0.99, 0.91) == (0.93, 1075268817204301076)
        assert b._ceiling_min_out(0.95, 0.7) == (0.9, 1111111111111111112)
        values.pop("lightning_probability")
        assert b._execution_max_price() == 0.9
        values["lightning_probability"] = 0.95
        b.context.params.lightning = LightningConfig()
        assert b._execution_max_price() == 0.9
