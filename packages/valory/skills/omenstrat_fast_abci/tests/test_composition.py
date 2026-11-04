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

"""Check fast and housekeeping paths in the composed FSM."""

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.decision_maker_abci.states.base import Event as D
from packages.valory.skills.decision_maker_abci.states.bet_placement import (
    BetPlacementRound,
)
from packages.valory.skills.decision_maker_abci.states.decision_request import (
    DecisionRequestRound,
)
from packages.valory.skills.decision_maker_abci.states.post_bet_update import (
    PostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.redeem import RedeemRound
from packages.valory.skills.fresh_watch_abci.rounds import (
    Event,
    FastDecisionRound,
    FreshWatchRound,
)
from packages.valory.skills.mech_interact_abci.states.base import Event as M
from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound
from packages.valory.skills.mech_interact_abci.states.response import MechResponseRound
from packages.valory.skills.omenstrat_fast_abci.composition import (
    FastPreTxSettlementRound,
    OmenstratFastAbciApp,
)
from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import Event as T
from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
    PostTxSettlementRound,
)


def test_fast_route() -> None:
    """A request and bet return to the watch without housekeeping."""
    transitions = OmenstratFastAbciApp.transition_function
    assert transitions[FreshWatchRound][Event.MARKET] == DecisionRequestRound
    from packages.valory.skills.decision_maker_abci.states.mech_only import (
        MechOnlySelectionRound,
    )

    assert transitions[FreshWatchRound][Event.MECH_ONLY] == MechOnlySelectionRound
    assert transitions[MechOnlySelectionRound][D.DONE] == MechRequestRound
    assert transitions[MechOnlySelectionRound][D.NO_MARKETS] == FreshWatchRound
    assert transitions[FreshWatchRound][Event.HOUSEKEEPING] == RedeemRound
    assert transitions[DecisionRequestRound][D.DONE] == MechRequestRound
    assert transitions[MechRequestRound][M.OFFCHAIN_DONE] == MechResponseRound
    assert (
        transitions[MechRequestRound][M.OFFCHAIN_DEPOSIT_NEEDED]
        == FastPreTxSettlementRound
    )
    assert (
        transitions[PostTxSettlementRound][T.OFFCHAIN_MECH_DEPOSIT_SETTLED]
        == MechRequestRound
    )
    assert transitions[MechRequestRound][M.OFFCHAIN_ALL_FAILED] == FastDecisionRound
    assert transitions[MechResponseRound][M.DONE] == FastDecisionRound
    assert transitions[FastDecisionRound][Event.BET] == BetPlacementRound
    assert transitions[FastDecisionRound][Event.SKIP] == FreshWatchRound
    assert transitions[BetPlacementRound][D.DONE] == FastPreTxSettlementRound
    from packages.valory.skills.decision_maker_abci.states.trade_count import (
        TradeCountRound,
    )

    assert transitions[PostTxSettlementRound][T.BET_PLACEMENT_DONE] == TradeCountRound
    assert transitions[TradeCountRound][D.DONE] == PostBetUpdateRound
    for event in (D.NONE, D.NO_MAJORITY, D.ROUND_TIMEOUT):
        assert transitions[TradeCountRound][event] == TradeCountRound
    assert transitions[PostBetUpdateRound][D.DONE] == FreshWatchRound
    names = {round_.__name__ for round_ in transitions}
    assert not names & {
        "ResetAndPauseRound",
        "TradeCapRound",
        "CheckStopTradingRound",
        "MechVersionDetectionRound",
        "UpdateBetsRound",
        "CheckBenchmarkingModeRound",
    }


@pytest.mark.parametrize("vote", [True, False])
def test_settlement_drops_previous_transaction_state(vote: bool) -> None:
    """A new transaction must not inherit the previous transaction's retries."""
    from unittest.mock import MagicMock

    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        FastPreTxSettlementRound,
    )
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
        SynchronizedData,
        VotingPayload,
    )

    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                tx_hashes_history=["old"],
                blacklisted_keepers=["agent"],
                final_tx_hash=["old"],
                tx_submitter=["bet_placement_round"],
                most_voted_tx_hash=["new"],
                fast_finalization_failures=[3],
                fast_finalization_may_be_sent=[True],
            )
        )
    )
    round_ = FastPreTxSettlementRound(data, MagicMock())
    assert round_.end_block() is None
    round_.process_payload(VotingPayload("agent", vote))
    outcome = round_.end_block()
    assert outcome is not None
    result, event = outcome
    if not vote:
        assert event == T.REFILL_REQUIRED
        assert result.db.get("tx_hashes_history") == "old"
        return
    assert event == T.CHECKS_PASSED
    assert result.db.get("tx_hashes_history") == ""
    assert result.db.get("blacklisted_keepers") == ""
    assert result.db.get("final_tx_hash") is None
    assert result.db.get("fast_finalization_failures") == 0
    assert result.db.get("fast_finalization_may_be_sent") is False
    assert result.db.get("most_voted_tx_hash") == "new"
    assert result.db.get("tx_submitter") == "bet_placement_round"


def test_fast_agent_validates_with_trader_receipt_check() -> None:
    """Use the trader receipt check, not the upstream validation behaviour."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        OmenstratFastConsensusBehaviour,
    )
    from packages.valory.skills.trader_abci.tx_validation import (
        TraderValidateTransactionBehaviour,
    )
    from packages.valory.skills.transaction_settlement_abci.behaviours import (
        ValidateTransactionBehaviour,
    )

    behaviours = OmenstratFastConsensusBehaviour.behaviours
    assert TraderValidateTransactionBehaviour in behaviours
    assert ValidateTransactionBehaviour not in behaviours


def test_settled_fast_bets_reach_durable_cap(tmp_path: Any) -> None:
    """Count settled transactions once and stop selecting markets at the cap."""
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
        read_trades,
    )
    from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
    from packages.valory.skills.decision_maker_abci.states.trade_count import (
        TradeCountRound,
    )
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour
    from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
    from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
        done,
        make,
        raw_market,
        run,
    )
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        OmenstratFastConsensusBehaviour,
    )
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        OmenstratFastAbciApp,
    )
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import Event as T
    from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
        PostTxSettlementRound,
    )

    settled = OmenstratFastAbciApp.transition_function[PostTxSettlementRound][
        T.BET_PLACEMENT_DONE
    ]
    assert settled is TradeCountRound
    cls = next(
        b
        for b in OmenstratFastConsensusBehaviour.behaviours
        if b.matching_round is settled
    )
    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                previous_checkpoint=[100],
                successful_trade_count=[0],
            )
        )
    )
    payloads = []

    def finish(payload: Any) -> Any:
        payloads.append(payload)
        return done(None)

    for tx_hash, count in [("tx1", 1), ("tx1", 1), ("tx2", 2), ("tx3", 3)]:
        payloads.clear()
        data = SynchronizedData(data.update(final_tx_hash=tx_hash).db)
        b = make(cls, tmp_path)
        b.context.agent_address = "agent"
        b.context.state.round_sequence.last_round_transition_timestamp.timestamp.return_value = (
            1050
        )
        b.finish_behaviour = finish
        with (
            patch.object(
                cls, "synchronized_data", new_callable=PropertyMock, return_value=data
            ),
            patch.object(cls, "sampled_bet", new_callable=PropertyMock) as bet,
        ):
            bet.return_value.id = "market"
            run(b.async_act())
        round_ = settled(data, b.context)
        round_.process_payload(payloads[0])
        outcome = round_.end_block()
        assert outcome is not None
        data = SynchronizedData(outcome[0].db)
        assert data.successful_trade_count == count
        stored = json.loads((tmp_path / "successful_trade_count.txt").read_text())
        assert stored["count"] == count
        assert stored["period_start"] == 100
        assert len(stored["placement_keys"]) == count
    assert len(read_trades(tmp_path)) == 3
    watch = make(FreshWatchBehaviour, tmp_path)
    watch.context.state.synchronized_data = data
    watch._compute_activity_status = lambda: done((False, False, 8, 3))
    watch.sleep = lambda *args: done(None)
    watch._fetch_fresh = MagicMock(
        side_effect=AssertionError("must not select a market")
    )
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [raw_market()]
    queue.last_housekeeping = 1050
    queue.save()
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(watch.select())["lane"] == "mech_only"
    assert FreshQueue(queue.path).pending == [raw_market()]
    watch._fetch_fresh.assert_not_called()


def _finalization_round(
    failures: Any = None,
    history: str = "",
    not_sent: bool = True,
    may_be_sent: bool = False,
    missed: int = 0,
) -> Any:
    """Build a fast finalization round with a keeper that got no hash."""
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        FastFinalizationRound,
    )
    from packages.valory.skills.transaction_settlement_abci.payloads import (
        FinalizationTxPayload,
    )
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        SynchronizedData,
        VerificationStatus,
    )

    setup: Dict[str, List[Any]] = dict(
        all_participants=[("agent",)],
        participants=[("agent",)],
        consensus_threshold=[1],
        missed_messages=[{"agent": missed}],
        fast_finalization_may_be_sent=[may_be_sent],
    )
    if failures is not None:
        setup["fast_finalization_failures"] = [failures]
    data = SynchronizedData(AbciAppDB(setup_data=setup))
    round_ = FastFinalizationRound(data, MagicMock())
    tx_data: Dict[str, Any] = dict(
        status_value=VerificationStatus.PENDING.value,
        serialized_keepers="",
        blacklisted_keepers="",
        tx_hashes_history=history,
        received_hash=False,
    )
    if not_sent:
        tx_data["not_sent"] = True
    round_.keeper_payload = FinalizationTxPayload("agent", tx_data=tx_data)
    return round_


def _limit() -> int:
    """Get the failure limit."""
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        MAX_FINALIZATION_FAILURES,
    )

    return MAX_FINALIZATION_FAILURES


def test_repeated_finalization_failures_return_to_the_watch() -> None:
    """A transaction that was never sent must not block the agent forever."""
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        FastFinalizationRound,
    )
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        Event as Settlement,
    )

    transitions = OmenstratFastAbciApp.transition_function
    assert transitions[FastFinalizationRound][Settlement.NONE] == FreshWatchRound
    failures = 0
    for _ in range(_limit() - 1):
        result, event = _finalization_round(failures).end_block()
        assert event == Settlement.FINALIZATION_FAILED
        failures = result.db.get("fast_finalization_failures")
    assert failures == _limit() - 1
    _, event = _finalization_round(failures).end_block()
    assert event == Settlement.NONE


def test_finalization_passes_a_received_hash() -> None:
    """A sent transaction goes to validation and adds no failure."""
    from packages.valory.skills.transaction_settlement_abci.payloads import (
        FinalizationTxPayload,
    )
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        Event as Settlement,
    )

    round_ = _finalization_round(0)
    tx_data = {**round_.keeper_payload.tx_data, "received_hash": True}
    round_.keeper_payload = FinalizationTxPayload("agent", tx_data=tx_data)
    result, event = round_.end_block()
    assert event == Settlement.DONE
    assert result.db.get("fast_finalization_failures") == 0


def test_finalization_waits_for_a_keeper_payload() -> None:
    """The round continues until the keeper reports."""
    round_ = _finalization_round()
    round_.keeper_payload = None
    assert round_.end_block() is None


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(history="a" * 64),
        dict(not_sent=False),
        dict(may_be_sent=True),
        dict(missed=1),
    ],
)
def test_finalization_keeps_a_transaction_that_may_be_sent(kwargs: Any) -> None:
    """Never abandon a settlement that can still have a live transaction."""
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        Event as Settlement,
    )

    result, event = _finalization_round(_limit(), **kwargs).end_block()
    assert event == Settlement.FINALIZATION_FAILED
    assert result.db.get("fast_finalization_may_be_sent") is True


def test_finalization_without_transaction_data_is_not_proof() -> None:
    """A keeper that reports no data has not proved that nothing was sent."""
    from packages.valory.skills.transaction_settlement_abci.payloads import (
        FinalizationTxPayload,
    )
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        Event as Settlement,
    )

    round_ = _finalization_round(_limit())
    round_.keeper_payload = FinalizationTxPayload("agent", tx_data=None)
    _, event = round_.end_block()
    assert event == Settlement.FINALIZATION_FAILED


def _finished(generator: Any) -> Any:
    """Run a generator that must finish without waiting."""
    with pytest.raises(StopIteration) as stop:
        next(generator)
    return stop.value.value


def _sender(gas: int) -> Any:
    """Run the fast sender for a raw transaction and get its payload."""
    from types import SimpleNamespace

    from packages.valory.skills.abstract_round_abci.behaviour_utils import (
        RPCResponseStatus,
    )
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        FastFinalizeBehaviour,
    )
    from packages.valory.skills.transaction_settlement_abci.behaviours import (
        FinalizeBehaviour,
    )
    from packages.valory.skills.transaction_settlement_abci.payloads import (
        FinalizationTxPayload,
    )

    def send(*_: Any, **__: Any) -> Any:
        """Accept a broadcast with a failed RPC response."""
        return "0xbackup", RPCResponseStatus.UNCLASSIFIED_ERROR
        yield  # pylint: disable=unreachable

    def consensus(*args: Any, **__: Any) -> Any:
        """Capture the payload."""
        sent.append(args[1])
        return None
        yield  # pylint: disable=unreachable

    sent: List[Any] = []
    behaviour = FastFinalizeBehaviour(name="finalize", skill_context=MagicMock())
    with (
        patch.object(FinalizeBehaviour, "send_raw_transaction", send) as _,
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils."
            "BaseBehaviour.send_a2a_transaction",
            consensus,
        ),
    ):
        status = _finished(
            behaviour.send_raw_transaction(SimpleNamespace(body={"gas": gas}))
        )
        payload = FinalizationTxPayload("agent", tx_data={"received_hash": False})
        _finished(behaviour.send_a2a_transaction(payload))
    return status, sent[0]


def test_sender_does_not_broadcast_a_transaction_with_no_gas() -> None:
    """A node can not accept gas 0, so the attempt is known as not sent."""
    from packages.valory.skills.abstract_round_abci.behaviour_utils import (
        RPCResponseStatus,
    )

    status, payload = _sender(0)
    assert status == (None, RPCResponseStatus.UNCLASSIFIED_ERROR)
    assert payload.tx_data["not_sent"] is True


def test_accepted_broadcast_with_failed_response_is_kept() -> None:
    """After gas 0 failures, an ambiguous broadcast blocks the exit for good."""
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        Event as Settlement,
    )

    _, payload = _sender(21000)
    assert "not_sent" not in payload.tx_data
    round_ = _finalization_round(_limit() - 1, not_sent=False)
    result, event = round_.end_block()
    assert event == Settlement.FINALIZATION_FAILED
    assert result.db.get("fast_finalization_may_be_sent") is True
    later = _finalization_round(_limit() - 1, may_be_sent=True)
    _, event = later.end_block()
    assert event == Settlement.FINALIZATION_FAILED


def test_sender_with_no_raw_transaction_reports_not_sent() -> None:
    """A keeper that did not reach the broadcast step has sent nothing."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        FastFinalizeBehaviour,
    )
    from packages.valory.skills.transaction_settlement_abci.payloads import (
        FinalizationTxPayload,
    )

    sent: List[Any] = []

    def consensus(*args: Any, **__: Any) -> Any:
        """Capture the payload."""
        sent.append(args[1])
        return None
        yield  # pylint: disable=unreachable

    behaviour = FastFinalizeBehaviour(name="finalize", skill_context=MagicMock())
    with patch(
        "packages.valory.skills.abstract_round_abci.behaviour_utils."
        "BaseBehaviour.send_a2a_transaction",
        consensus,
    ):
        payload = FinalizationTxPayload("agent", tx_data={"received_hash": False})
        _finished(behaviour.send_a2a_transaction(payload))
        _finished(
            behaviour.send_a2a_transaction(FinalizationTxPayload("agent", tx_data=None))
        )
    assert sent[0].tx_data["not_sent"] is True
    assert sent[1].tx_data is None


def test_fast_agent_finalizes_with_the_bounded_round() -> None:
    """Trading uses the bounded round. Termination keeps the upstream round."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        FastFinalizeBehaviour,
        OmenstratFastConsensusBehaviour,
    )
    from packages.valory.skills.omenstrat_fast_abci.composition import (
        FastFinalizationRound,
    )
    from packages.valory.skills.transaction_settlement_abci.behaviours import (
        FinalizeBehaviour,
    )
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        FinalizationRound,
    )

    behaviours = OmenstratFastConsensusBehaviour.behaviours
    assert {FastFinalizeBehaviour, FinalizeBehaviour} <= behaviours
    assert FastFinalizeBehaviour.matching_round is FastFinalizationRound
    transitions = OmenstratFastAbciApp.transition_function
    assert FastFinalizationRound in transitions
    assert FinalizationRound not in transitions
    assert not any(FinalizationRound in row.values() for row in transitions.values())
