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

"""Watch fresh Omen markets and execute the fixed fast strategy."""

import json
import time
from dataclasses import asdict
from typing import Any, Callable, Dict, Generator, List, Optional

from packages.valory.protocols.contract_api.message import ContractApiMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.abstract_round_abci.behaviours import AbstractRoundBehaviour
from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    read_successful_trade_count_state,
)
from packages.valory.skills.agent_performance_summary_abci.models import (
    write_json_atomically,
)
from packages.valory.skills.check_stop_trading_abci.behaviours import (
    CheckStopTradingBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.decision_request import (
    DecisionRequestBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.mech_only import (
    MechOnlySelectionBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.post_bet_update import (
    PostBetUpdateBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.reedem import RedeemBehaviour
from packages.valory.skills.decision_maker_abci.models import RedeemingProgress
from packages.valory.skills.decision_maker_abci.policy import EGreedyPolicy
from packages.valory.skills.fresh_watch_abci.rounds import (
    FastDecisionRound,
    FreshWatchAbciApp,
    FreshWatchRound,
    SelectionPayload,
)
from packages.valory.skills.fresh_watch_abci.strategy import (
    FreshQueue,
    decide,
    housekeeping_due,
    template,
)
from packages.valory.skills.market_manager_abci.bets import (
    Bet,
    PredictionResponse,
    QueueStatus,
)
from packages.valory.skills.staking_abci.rounds import StakingState

_FIELDS = """id title collateralToken condition { id } creator fee creationTimestamp
openingTimestamp outcomeSlotCount outcomeTokenAmounts outcomeTokenMarginalPrices
outcomes scaledLiquidityMeasure"""


def log_decision(behaviour: DecisionMakerBaseBehaviour, result: Dict[str, Any]) -> None:
    """Write one structured strategy line.

    :param behaviour: calling behaviour.
    :param result: decision and timing fields.
    """
    result.setdefault("mech", None)
    result.setdefault("latency_s", None)
    behaviour.context.logger.info(
        "[FAST_DECISION] %s", json.dumps(result, sort_keys=True)
    )


class FreshWatchBehaviour(DecisionMakerBaseBehaviour, CheckStopTradingBehaviour):
    """Poll the fresh-market query and select one durable queue entry."""

    matching_round = FreshWatchRound  # type: ignore[assignment]

    def contract_interact(
        self, *args: Any, **kwargs: Any
    ) -> Generator[None, None, bool]:
        """Use the decision reader with the staking reader's default performative.

        :param args: positional contract arguments.
        :param kwargs: contract inputs and result placeholder.
        :yield: contract API I/O.
        :return: whether the contract read succeeded.
        """
        if not args:
            kwargs.setdefault(
                "performative", ContractApiMessage.Performative.GET_RAW_TRANSACTION
            )
        return (yield from super().contract_interact(*args, **kwargs))

    def _fetch_fresh(
        self, cursor: int, now: int
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Read all pages before advancing the cursor.

        :param cursor: inclusive creation timestamp lower bound.
        :param now: fixed upper timestamp bound for this poll.
        :yield: control during HTTP I/O.
        :return: complete market rows or None on failure.
        """
        subgraph = self.context.omen_subgraph
        creators = json.dumps(
            [creator.lower() for creator in self.params.fast.creators]
        )
        rows: List[Dict[str, Any]] = []
        while True:
            query = (
                "{ fixedProductMarketMakers(first: 1000, skip: "
                + str(len(rows))
                + ", orderBy: creationTimestamp, orderDirection: asc, where: {"
                + f'creator_in: {creators}, creationTimestamp_gte: "{cursor}", '
                + f'creationTimestamp_lte: "{now}", openingTimestamp_gt: "{now}", '
                + "outcomeSlotCount: 2, isPendingArbitration: false}) {"
                + _FIELDS
                + "} }"
            )
            response = yield from self.get_http_response(
                content=json.dumps({"query": query}).encode(), **subgraph.get_spec()
            )
            page = subgraph.process_response(response)
            if not isinstance(page, list):
                return None
            rows.extend(page)
            if len(page) < 1000:
                return rows

    def select(self) -> Generator[None, None, Dict[str, Any]]:
        """Choose the next lane and persist a market before its paid request.

        :yield: control during polling, live quotes, and idle waits.
        :return: consensus inputs for the selected lane.
        """
        kpi_met, target_met, target, completed = (
            yield from self._compute_activity_status()
        )
        period = (
            self.ts_checkpoint
            if self.service_staking_state == StakingState.STAKED
            else self.synchronized_data.previous_checkpoint
        )
        count, _ = read_successful_trade_count_state(self.params.store_path, period)
        if count is None:
            count = self.synchronized_data.successful_trade_count
        capped = self.params.max_trades > 0 and count >= self.params.max_trades
        if capped:
            marker = self.params.store_path / "fast_trade_cap.json"
            try:
                logged = json.loads(marker.read_text()) == {"period_start": period}
            except (OSError, ValueError):
                logged = False
            if not logged:
                self.context.logger.info(
                    "reason=trade_cap period_start=%s count=%s max_trades=%s",
                    period,
                    count,
                    self.params.max_trades,
                )
                write_json_atomically(marker, {"period_start": period})
        selection = yield from self._select(capped, target_met or target == 0)
        return dict(
            selection,
            previous_checkpoint=period,
            is_staking_kpi_met=kpi_met,
            is_activity_target_met=target_met,
            activity_target=target,
            activity_completed=completed,
            mech_only_mode=capped,
        )

    def _select(
        self, capped: bool, target_met: bool
    ) -> Generator[None, None, Dict[str, Any]]:
        """Select a market, a capped request, or periodic housekeeping.

        :param capped: whether the period trade cap is reached.
        :param target_met: whether no more staking activity is needed.
        :yield: polling and contract I/O.
        :return: the selected lane and its inputs.
        """
        cfg = self.params.fast
        queue = FreshQueue(self.params.store_path / "fresh_watch.json")
        now = time.time()
        if capped:
            if housekeeping_due(now, queue.last_housekeeping, False, cfg):
                self.context.state.redeeming_progress = RedeemingProgress(
                    event_filtering_batch_size=self.params.event_filtering_batch_size
                )
                queue.last_housekeeping = now
                queue.save()
                return {"lane": "housekeeping"}
            yield from self.sleep(cfg.poll_s)
            if not target_met and self.params.use_offchain:
                return {"lane": "mech_only"}
            return {"lane": "wait"}
        last_poll = self.context.state.fast_last_poll
        if cfg.in_window(now) or queue.pending:
            if self.context.state.fast_background_running is True:
                if not queue.pending and (
                    self.context.state.fast_polling or now - last_poll >= cfg.poll_s
                ):
                    yield from self.sleep(1)
                    return {"lane": "wait"}
            elif now - last_poll >= cfg.poll_s:
                rows = yield from self._fetch_fresh(
                    max(
                        queue.last_seen - cfg.overlap_s, int(now) - cfg.fresh_max_age_s
                    ),
                    int(now),
                )
                self.context.state.fast_last_poll = now
                if rows is not None:
                    queue.ingest(
                        rows, now, cfg.creators, cfg.fresh_max_age_s, cfg.overlap_s
                    )
                    queue.save()
        if not queue.pending:
            if housekeeping_due(now, queue.last_housekeeping, False, cfg):
                self.context.state.redeeming_progress = RedeemingProgress(
                    event_filtering_batch_size=self.params.event_filtering_batch_size
                )
                queue.last_housekeeping = now
                queue.save()
                return {"lane": "housekeeping"}
            yield from self.sleep(cfg.poll_s)
            return {"lane": "wait"}

        mechs = self.synchronized_data.mechs_info
        if self.params.fast_dynamic_mech is True and self.params.use_offchain is True:
            penalized = {
                address.lower() for address in self.shared_state.penalized_mechs
            }
            mechs = [
                mech
                for mech in mechs
                if mech.http_url and mech.address.lower() not in penalized
            ]
        offered = {tool for mech in mechs for tool in mech.relevant_tools}
        tool = next((tool for tool in cfg.tools if tool in offered), None)
        if tool is None:
            self.context.logger.warning("Neither fast prediction tool is available.")
            yield from self.sleep(cfg.poll_s)
            return {"lane": "wait"}
        raw = queue.pop()
        assert raw is not None
        bet_data = raw.copy()
        condition = bet_data.pop("condition", None)
        if condition is not None:
            bet_data["condition_id"] = condition["id"]
        bet = Bet(**bet_data, market="omen_subgraph")
        self.read_bets()
        index = next(
            (index for index, stored in enumerate(self.bets) if stored.id == bet.id),
            len(self.bets),
        )
        if index == len(self.bets):
            self.bets.append(bet)
        else:
            self.bets[index].update_market_info(bet)
        self.bets[index].queue_status = QueueStatus.TO_PROCESS
        self.store_bets()
        balances = yield from self._get_pool_balances(
            bet.id, bet.collateralToken, bet.condition_id
        )
        requested_at = time.time()
        if (
            balances is None
            or bet.openingTimestamp <= requested_at
            or requested_at - bet.creationTimestamp > cfg.fresh_max_age_s
        ):
            retry = (
                balances is None
                and bet.openingTimestamp > requested_at
                and requested_at - bet.creationTimestamp <= cfg.fresh_max_age_s
            )
            if retry:
                # Preserve markets found by the poller during the pool read.
                queue = FreshQueue(queue.path)
                queue.pending.append(raw)
                queue.pending.sort(
                    key=lambda row: (int(row["creationTimestamp"]), row["id"])
                )
                del queue.asked[bet.id]
                queue.save()
            log_decision(
                self,
                dict(
                    market=bet.id,
                    age_at_request=requested_at - bet.creationTimestamp,
                    age_at_answer=None,
                    age_at_bet=None,
                    p_tool=None,
                    template=template(bet.title),
                    p_hat=None,
                    side=None,
                    price=None,
                    edge=None,
                    stake=0,
                    reason=(
                        "request_price_unavailable_restored_for_retry"
                        if retry
                        else "request_closed_or_expired"
                    ),
                ),
            )
            if retry:
                yield from self.sleep(cfg.poll_s)
            return {"lane": "wait"}
        prices = [balances[1] / sum(balances), balances[0] / sum(balances)]
        return dict(
            lane="market",
            sampled_bet_index=index,
            mech_tool=tool,
            request_prices=prices,
            requested_at=requested_at,
        )

    def async_act(self) -> Generator:
        """Agree on a selection."""
        selection = yield from self.select()
        payload = SelectionPayload(
            self.context.agent_address, json.dumps(selection, sort_keys=True)
        )
        yield from self.finish_behaviour(payload)


class FreshMarketPoller(FreshWatchBehaviour):
    """Collect markets while the foreground FSM processes one market."""

    def get_callback_request(self) -> Callable:
        """Receive HTTP replies independently of the foreground round.

        :return: the callback for this poller's pending HTTP request.
        """

        def callback(message: Any, _current_behaviour: Any) -> None:
            """Deliver a reply to this cooperative poller."""
            if self.state == self.AsyncState.WAITING_MESSAGE:
                self.try_send(message)

        return callback

    def async_act(self) -> Generator:
        """Poll on a fixed cadence, without sending any consensus payload."""
        cfg = self.params.fast
        now = time.time()
        state = self.context.state
        queue = FreshQueue(self.params.store_path / "fresh_watch.json")
        if (
            cfg.in_window(now) or queue.pending
        ) and now - state.fast_last_poll >= cfg.poll_s:
            state.fast_last_poll = now
            state.fast_polling = True
            try:
                rows = yield from self._fetch_fresh(
                    max(
                        queue.last_seen - cfg.overlap_s, int(now) - cfg.fresh_max_age_s
                    ),
                    int(now),
                )
                if rows is not None:
                    # The foreground can pop a market while the HTTP request waits.
                    queue = FreshQueue(queue.path)
                    queue.ingest(
                        rows, now, cfg.creators, cfg.fresh_max_age_s, cfg.overlap_s
                    )
                    queue.save()
            except TimeoutException:
                self.context.logger.warning(
                    "Fresh-market discovery timed out; retry on the next poll."
                )
            finally:
                state.fast_polling = False
        yield from self.sleep(max(1.0, cfg.poll_s - (time.time() - now)))


class FastDecisionBehaviour(DecisionMakerBaseBehaviour):
    """Apply the fixed strategy to one response and a live pool quote."""

    matching_round = FastDecisionRound

    def evaluate(self) -> Generator[None, None, Dict[str, Any]]:
        """Build the decision without policy or strategy downloads.

        :yield: control during the live pool read.
        :return: decision fields and market ages.
        """
        if self.synchronized_data.mech_only_mode is True:
            return {"stake": 0.0, "side": None, "reason": "mech_only"}
        bet = self.sampled_bet
        db = self.synchronized_data.db
        now = time.time()
        requested_at = float(db.get_strict("requested_at"))
        result = dict(
            market=bet.id,
            age_at_request=requested_at - bet.creationTimestamp,
            age_at_answer=now - bet.creationTimestamp,
            age_at_bet=None,
            answered_at=now,
            p_tool=None,
            template=template(bet.title),
            p_hat=None,
            side=None,
            price=None,
            edge=None,
            stake=0.0,
            reason="mech_response",
        )
        timings = self.context.shared_state.get("mech_latency", {})
        timing: Dict[str, Any] = (
            next(
                (
                    record
                    for record in reversed(timings.values())
                    if record.get("market") == (bet.condition_id or bet.id)
                    and record.get("latency_s") is not None
                ),
                {},
            )
            if isinstance(timings, dict)
            else {}
        )
        result.update(mech=timing.get("mech"), latency_s=timing.get("latency_s"))
        prediction = self._get_decision_prediction()
        if prediction is None:
            return result
        result["p_tool"] = prediction.p_yes
        balances = yield from self._get_pool_balances(
            bet.id, bet.collateralToken, bet.condition_id
        )
        if balances is None:
            result["reason"] = "live_price_unavailable"
            return result
        if bet.openingTimestamp <= time.time():
            result["reason"] = "market_closed"
            return result
        prices = [balances[1] / sum(balances), balances[0] / sum(balances)]
        decision = decide(
            bet.title,
            prediction.p_yes,
            prices,
            db.get_strict("request_prices"),
            self.params.fast,
        )
        result.update(asdict(decision))
        if decision.stake > 0:
            bet.strategy_vote = decision.side
            bet.prediction_response = PredictionResponse(
                p_yes=decision.p_hat,
                p_no=1 - decision.p_hat,
                confidence=1.0,
                info_utility=1.0,
            )
            self.store_bets()
        return result

    def async_act(self) -> Generator:
        """Agree on a decision and log skipped markets."""
        result = yield from self.evaluate()
        if result["stake"] <= 0:
            log_decision(self, result)
        payload = SelectionPayload(
            self.context.agent_address, json.dumps(result, sort_keys=True)
        )
        yield from self.finish_behaviour(payload)


class FastBetPlacementBehaviour(BetPlacementBehaviour):
    """Retain settlement and balance checks; use the calibrated probability."""

    def _get_decision_prediction(self) -> Optional[PredictionResponse]:
        """Read the calibrated prediction for the final execution check.

        :return: calibrated YES and NO probabilities.
        """
        result = json.loads(self.synchronized_data.db.get_strict("fast_decision"))
        p_hat = result["p_hat"]
        return PredictionResponse(
            p_yes=p_hat, p_no=1 - p_hat, confidence=1.0, info_utility=1.0
        )

    def _calc_buy_amount(self) -> Generator[None, None, bool]:
        """Abort when the execution quote exceeds the price or slip bounds.

        :yield: control during the contract quote.
        :return: whether the quoted buy satisfies the bounds.
        """
        success = yield from super()._calc_buy_amount()
        if not success or self.quoted_tokens <= 0:
            return False
        price = self.investment_amount / self.quoted_tokens
        requested = self.synchronized_data.db.get_strict("request_prices")[
            self.outcome_index
        ]
        cfg = self.params.fast
        return price <= cfg.max_price and price - requested <= cfg.max_slip + 1e-12

    def finish_behaviour(self, payload: Any) -> Generator:
        """Log the final bet preparation outcome once.

        :param payload: existing bet-placement payload.
        :yield: control during consensus.
        """
        result = json.loads(self.synchronized_data.db.get_strict("fast_decision"))
        result["age_at_bet"] = time.time() - self.sampled_bet.creationTimestamp
        if not payload.tx_hash:
            result["reason"] = "placement_balance_quote_or_edge"
            result["stake"] = 0.0
        if self.quoted_tokens > 0:
            result["price"] = self.investment_amount / self.quoted_tokens
            p = result["p_hat"] if self.outcome_index == 0 else 1 - result["p_hat"]
            result["edge"] = p - result["price"]
        log_decision(self, result)
        yield from super().finish_behaviour(payload)


class HousekeepingRedeemBehaviour(RedeemBehaviour):
    """Reuse redemption without tool selection or accuracy downloads."""

    def _setup_policy_and_tools(self) -> Generator[None, None, bool]:
        """Supply an empty accounting policy. It never selects a tool.

        :yield: no I/O is needed.
        :return: True.
        """
        self._policy = EGreedyPolicy(0.0, 1, 0)
        self.mech_tools = set(self.params.fast.tools)
        yield from ()
        return True


class FreshWatchRoundBehaviour(AbstractRoundBehaviour):
    """Behaviours for the fresh-watch app."""

    initial_behaviour_cls = FreshWatchBehaviour
    abci_app_cls = FreshWatchAbciApp
    behaviours = {
        FreshWatchBehaviour,
        MechOnlySelectionBehaviour,
        FastDecisionBehaviour,
        FastBetPlacementBehaviour,
        DecisionRequestBehaviour,
        PostBetUpdateBehaviour,
        HousekeepingRedeemBehaviour,
    }
