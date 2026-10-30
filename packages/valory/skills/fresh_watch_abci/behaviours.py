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

import hashlib
import json
import time
from abc import ABC
from dataclasses import asdict, replace
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple, cast

from eth_abi import encode
from eth_utils import to_checksum_address  # type: ignore[import-not-found]

from packages.valory.contracts.gnosis_safe.contract import SafeOperation
from packages.valory.protocols.contract_api.message import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.base import (
    BaseTxPayload,
    LEDGER_API_ADDRESS,
)
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.abstract_round_abci.behaviours import AbstractRoundBehaviour
from packages.valory.skills.abstract_round_abci.dialogues import (
    LedgerApiDialogue,
    LedgerApiDialogues,
)
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
    SAFE_GAS,
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
from packages.valory.skills.decision_maker_abci.behaviours.trade_count import (
    TradeCountBehaviour,
)
from packages.valory.skills.decision_maker_abci.models import RedeemingProgress
from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload
from packages.valory.skills.decision_maker_abci.policy import EGreedyPolicy
from packages.valory.skills.fresh_watch_abci.chain_discovery import (
    ChainDiscoveryMixin,
    ChainRPCError,
)
from packages.valory.skills.fresh_watch_abci.lane import execution_ceiling
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
    lightning_config,
    natural_yes_index,
    preview_enabled,
    still_hold_rule,
)
from packages.valory.skills.fresh_watch_abci.preview import PreviewMixin, preview_night
from packages.valory.skills.fresh_watch_abci.rounds import (
    FastDecisionRound,
    FreshWatchAbciApp,
    FreshWatchRound,
    SelectionPayload,
)
from packages.valory.skills.fresh_watch_abci.rpc import request_with_timeout
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
from packages.valory.skills.mech_interact_abci.behaviours.base import (
    MechInteractBaseBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainCycleResult,
)
from packages.valory.skills.mech_interact_abci.behaviours.offchain_response import (
    OffchainResponsePoller,
    _PollSnapshot,
    serialise_responses,
)
from packages.valory.skills.mech_interact_abci.payloads import JSONPayload
from packages.valory.skills.mech_interact_abci.states.base import Event as MechEvent
from packages.valory.skills.staking_abci.rounds import StakingState
from packages.valory.skills.trader_abci.mech_latency import (
    TraderMechRequestBehaviour,
    TraderMechResponseBehaviour,
)

_FIELDS = """id title collateralToken condition { id } creator fee creationTimestamp
openingTimestamp outcomeSlotCount outcomeTokenAmounts outcomeTokenMarginalPrices
outcomes scaledLiquidityMeasure question { templateId }"""

# Bounded response deadline for the fast-lane preflight estimate_gas call.
# Short enough to leave headroom under the placement-round timeout; long
# enough to cover one healthy RPC round-trip across the configured endpoints.
_PREFLIGHT_TIMEOUT_S = 5.0


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


def lightning_controls(behaviour: DecisionMakerBaseBehaviour) -> LightningConfig:
    """Read the optional controls for a fast or lightning agent."""
    controls = getattr(behaviour.params, "lightning", None)
    controls = controls if isinstance(controls, LightningConfig) else LightningConfig()
    if controls.lightning_preview and not preview_enabled(behaviour):
        return replace(controls, lightning_preview=False)
    return controls


def remove_preview_bets(behaviour: DecisionMakerBaseBehaviour) -> None:
    """Remove request-only records before any normal market processing.

    :param behaviour: foreground behaviour with the local bets store.
    """
    behaviour.read_bets()
    retained = [bet for bet in behaviour.bets if not bet.id.startswith("preview:")]
    if len(retained) != len(behaviour.bets):
        behaviour.bets = retained
        if retained:
            behaviour.store_bets()
        else:
            write_json_atomically(behaviour.params.store_path / "multi_bets.json", [])


class FreshWatchBehaviour(
    ChainDiscoveryMixin,
    PreviewMixin,
    DecisionMakerBaseBehaviour,
    CheckStopTradingBehaviour,
):
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

    def _discovery_creators(self) -> List[str]:
        """Use lightning creators when any independent lightning path is enabled.

        :return: the allowed creator addresses for market discovery.
        """
        cfg = lightning_controls(self)
        if (
            cfg.lightning_still_hold
            or cfg.lightning_chain_detect
            or cfg.lightning_preview
        ):
            return cfg.lightning_creators
        return self.params.fast.creators

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
            [creator.lower() for creator in self._discovery_creators()]
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

    def _base_rate_response(self, **kwargs: Any) -> Generator[None, None, Any]:
        """Read one evidence page within a short scheduler deadline.

        :param kwargs: HTTP request fields.
        :yield: cooperative request I/O.
        :return: the HTTP response.
        """
        message, dialogue = self._build_http_request_message(**kwargs)
        return (yield from request_with_timeout(self, message, dialogue, 5.0))

    def _refresh_base_rate(self, now: float) -> Generator[None, None, None]:
        """Refresh daily or recover unusable evidence during the night."""
        cfg = lightning_controls(self)
        cache = BaseRateCache(self.params.store_path / "lightning_base_rate.json")
        if not cache.refresh_due(now, cfg, self.params.fast):
            return
        cache.reserve_refresh(now)
        subgraph = self.context.omen_subgraph
        creators = json.dumps([value.lower() for value in cfg.lightning_creators])
        rows: List[Dict[str, Any]] = []
        cursor = int(now - cfg.lightning_base_rate_window_days * 86400)
        while True:
            query = (
                "{ fixedProductMarketMakers(first: 1000, skip: "
                + str(len(rows))
                + ", orderBy: creationTimestamp, orderDirection: asc, where: {"
                + f'creator_in: {creators}, creationTimestamp_gte: "{cursor}", '
                + f'creationTimestamp_lte: "{int(now)}", payouts_not: null, '
                + "outcomeSlotCount: 2}) {id title creator creationTimestamp outcomes payouts "
                + "question { templateId }} }"
            )
            try:
                response = yield from self._base_rate_response(
                    content=json.dumps({"query": query}).encode(), **subgraph.get_spec()
                )
            except TimeoutException:
                self.context.logger.warning(
                    "Lightning base-rate refresh timed out; continuing discovery."
                )
                return
            page = subgraph.process_response(response)
            if not isinstance(page, list):
                return
            rows.extend(page)
            if len(page) < 1000:
                break
        cache.update(rows, now, cfg)
        self.context.logger.info(
            "[LIGHTNING_BASE_RATE] n=%s yes=%s rate=%s window=%s",
            cache.sample,
            cache.data["yes"],
            cache.rate,
            cfg.lightning_base_rate_window_days,
        )

    def _discovery_period(self) -> float:
        """Poll each block only while chain detection is enabled."""
        if self.context.state.fast_chain_fallback is True:
            return self.params.fast.poll_s
        return (
            5.0
            if lightning_controls(self).lightning_chain_detect
            or lightning_controls(self).lightning_preview
            else self.params.fast.poll_s
        )

    def _poll_markets(
        self, queue: FreshQueue, now: float
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Use configured discovery and retain the existing subgraph fallback."""
        cfg = self.params.fast
        controls = lightning_controls(self)
        chain_rows = None
        if controls.lightning_chain_detect:
            chain_rows = yield from self.chain_markets(now)
            self.context.state.fast_chain_fallback = chain_rows is None
            if chain_rows:
                return chain_rows
        if controls.lightning_preview:
            try:
                yield from self.preview_poll(now)
            except (ChainRPCError, TimeoutException) as error:
                self.context.logger.warning(
                    "Lightning preview discovery failed: %s", error
                )
        if chain_rows is not None:
            return chain_rows
        rows = yield from self._fetch_fresh(
            max(queue.last_seen - cfg.overlap_s, int(now) - cfg.fresh_max_age_s),
            int(now),
        )
        if controls.lightning_chain_detect and rows is not None:
            for row in rows:
                self.context.logger.info(
                    "[LIGHTNING_DETECT] market=%s delay_s=%.3f source=subgraph",
                    row["id"],
                    max(0, now - int(row["creationTimestamp"])),
                )
        return rows

    def select(self) -> Generator[None, None, Dict[str, Any]]:
        """Choose the next lane and persist a market before its paid request.

        :yield: control during polling, live quotes, and idle waits.
        :return: consensus inputs for the selected lane.
        """
        # Keep payload activity fields current without repeating all staking reads
        # in each fast round. The shared cache survives behaviour replacement.
        cache = self.context.state.fast_activity_status
        now = time.monotonic()
        count = None
        # Mech requests after the trade cap do not change the trade count, so
        # recompute the activity target until it is met.
        capped_unmet = (
            cache is not None
            and 0 < self.params.max_trades <= cache[3]
            and not cache[2][1]
        )
        if (
            cache is not None
            and not capped_unmet
            and cache[1] == self.synchronized_data.previous_checkpoint
            and now - cache[0] < self.params.fast.housekeeping_interval_s
        ):
            count, _ = read_successful_trade_count_state(
                self.params.store_path, cache[1]
            )
            if count is None:
                count = self.synchronized_data.successful_trade_count
        if count is None or cache is None or cache[3] != count:
            status = yield from self._compute_activity_status()
            period = (
                self.ts_checkpoint
                if self.service_staking_state == StakingState.STAKED
                else self.synchronized_data.previous_checkpoint
            )
            if count is None or cache is None or cache[1] != period:
                count, _ = read_successful_trade_count_state(
                    self.params.store_path, period
                )
                if count is None:
                    count = self.synchronized_data.successful_trade_count
            cache = (now, period, status, count)
            self.context.state.fast_activity_status = cache
        _, period, (kpi_met, target_met, target, completed), count = cache
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

    def _fast_tool(self) -> Optional[str]:
        """Select the existing fast tool with the same mech eligibility rules."""
        cfg = self.params.fast
        mechs = self.synchronized_data.mechs_info
        if self.params.fast_dynamic_mech is True and self.params.use_offchain is True:
            penalized = {
                address.lower() for address in self.shared_state.penalized_mechs
            }
            endpoints = [
                mech
                for mech in mechs
                if mech.http_url
                and any(tool in cfg.tools for tool in mech.relevant_tools)
            ]
            mechs = [
                mech for mech in endpoints if mech.address.lower() not in penalized
            ] or endpoints
        offered = {tool for mech in mechs for tool in mech.relevant_tools}
        return next((tool for tool in cfg.tools if tool in offered), None)

    def _preview_selection(self, now: float) -> Optional[Dict[str, Any]]:
        """Reserve one proposed question before the paid fast request."""
        cfg = lightning_controls(self)
        if not cfg.lightning_preview or not self.params.fast.in_window(now):
            return None
        store = self.preview_store()
        queue = FreshQueue(self.params.store_path / "fresh_watch.json")
        self.read_bets()
        known_titles = {row["title"] for row in queue.pending} | {
            bet.title for bet in self.bets if not bet.id.startswith("preview:")
        }
        title = store.next_title(now, known_titles)
        tool = self._fast_tool() if title else None
        if title is None or tool is None:
            return None
        entry = store.reserve(
            title,
            now,
            preview_night(now, self.params.fast.active_window_utc),
            cfg.lightning_preview_max_per_night,
        )
        if entry is None:
            return None
        bet = Bet(
            id="preview:" + hashlib.sha256(title.encode()).hexdigest(),
            market="omen_subgraph",
            title=title,
            collateralToken="0x0000000000000000000000000000000000000000",
            creator=cfg.lightning_creators[0],
            fee=0,
            creationTimestamp=int(now),
            openingTimestamp=int(now) + 900,
            outcomeSlotCount=2,
            outcomeTokenAmounts=[1, 1],
            outcomeTokenMarginalPrices=[0.5, 0.5],
            outcomes=["Yes", "No"],
            scaledLiquidityMeasure=1,
        )
        self.read_bets()
        self.bets.append(bet)
        self.store_bets()
        self.preview_log(entry)
        return dict(
            lane="preview_request",
            sampled_bet_index=len(self.bets) - 1,
            mech_tool=tool,
            request_prices=[0.5, 0.5],
            requested_at=now,
            lightning_preview_title=title,
            lightning_preview_request=True,
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
        remove_preview_bets(self)
        queue = FreshQueue(self.params.store_path / "fresh_watch.json")
        now = time.time()
        yield from self._refresh_base_rate(now)
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
            if not target_met:
                return {"lane": "mech_only"}
            return {"lane": "wait"}
        last_poll = self.context.state.fast_last_poll
        poll_s = self._discovery_period()
        if cfg.in_window(now) or queue.pending:
            if self.context.state.fast_background_running is True:
                if not queue.pending and (
                    self.context.state.fast_polling or now - last_poll >= poll_s
                ):
                    yield from self.sleep(1)
                    return {"lane": "wait"}
            elif now - last_poll >= poll_s:
                rows = yield from self._poll_markets(queue, now)
                now = time.time()
                self.context.state.fast_last_poll = now
                if rows is not None:
                    queue = FreshQueue(queue.path)
                    queue.ingest(
                        rows,
                        now,
                        self._discovery_creators(),
                        cfg.fresh_max_age_s,
                        cfg.overlap_s,
                    )
                    queue.save()
        if not queue.pending:
            preview = self._preview_selection(now)
            if preview is not None:
                return preview
            if housekeeping_due(now, queue.last_housekeeping, False, cfg):
                self.context.state.redeeming_progress = RedeemingProgress(
                    event_filtering_batch_size=self.params.event_filtering_batch_size
                )
                queue.last_housekeeping = now
                queue.save()
                return {"lane": "housekeeping"}
            if self.context.state.fast_background_running is not True:
                yield from self.sleep(cfg.poll_s)
                return {"lane": "wait"}
            # Wake as soon as the background poller queues a market.
            for _ in range(cfg.poll_s):
                yield from self.sleep(1)
                if FreshQueue(queue.path).pending:
                    break
            return {"lane": "wait"}

        lightning = lightning_controls(self)
        candidate = queue.pending[0]
        if lightning.lightning_lane:
            self.read_bets()
            mined = self.context.shared_state.get("lightning_lane_bet_markets", set())
            if candidate["id"] in mined or any(
                bet.id == candidate["id"] and bet.has_investments for bet in self.bets
            ):
                queue.pending.pop(0)
                queue.save()
                return {"lane": "wait"}
        if "block_hash" in candidate and (
            float(candidate["scaledLiquidityMeasure"]) <= 0
            or min(map(int, candidate["outcomeTokenAmounts"])) <= 0
        ):
            queue.pending.pop(0)
            queue.save()
            return {"lane": "wait"}
        canonical = natural_yes_index(candidate) is not None
        rule = still_hold_rule(candidate["title"], candidate["creator"], lightning)
        probability = None
        if lightning.lightning_still_hold and rule and canonical:
            base_rate = BaseRateCache(
                self.params.store_path / "lightning_base_rate.json"
            )
            probability = base_rate.usable(now, lightning)
            if probability is None:
                self.context.logger.info(
                    "[LIGHTNING_BASE_RATE] off reason=%s",
                    base_rate.off_reason(now, lightning),
                )
        preview = (
            self.preview_store().match(candidate["title"], now)
            if lightning.lightning_preview and canonical
            else None
        )
        if preview is not None:
            self.preview_log(preview)
            if preview.get("p_yes") is None:
                requested_at = preview["requested_at"]
                timeout = min(
                    300.0,
                    self.params.mech_marketplace_config.offchain_poll_timeout_seconds,
                )
                if (
                    self.context.state.fast_started_at < requested_at <= now
                    and now - requested_at < timeout
                ):
                    yield from self.sleep(1)
                    return {"lane": "wait"}
                self.preview_store().answer_failed(candidate["title"], now)
                self.preview_log(preview, "abandoned_request")
                preview = None
        tool = self._fast_tool()
        if tool is None and probability is None and preview is None:
            self.context.logger.warning("Neither fast prediction tool is available.")
            yield from self.sleep(cfg.poll_s)
            return {"lane": "wait"}
        raw = queue.pop()
        assert raw is not None
        bet_data = raw.copy()
        bet_data.pop("block_number", None)
        bet_data.pop("block_hash", None)
        question = bet_data.pop("question", None)
        question_template_id = (
            question.get("templateId") if isinstance(question, dict) else None
        )
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
        pool_read = (
            self._chain_pool_balances
            if "block_hash" in raw
            else self._get_pool_balances
        )
        balances = yield from pool_read(bet.id, bet.collateralToken, bet.condition_id)
        requested_at = time.time()
        if (
            balances is None
            or ("block_hash" in raw and (not balances or min(balances) <= 0))
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
            elif "block_hash" in raw and balances and min(balances) <= 0:
                queue = FreshQueue(queue.path)
                queue.asked.pop(bet.id, None)
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
        if probability is not None:
            probability = base_rate.usable(time.time(), lightning)
        if probability is not None:
            self.context.logger.info(
                "[LIGHTNING_STILL_HOLD] market=%s rule=%s p_yes=%s",
                bet.id,
                rule,
                probability,
            )
            return dict(
                lane="lightning_decision",
                sampled_bet_index=index,
                request_prices=prices,
                requested_at=requested_at,
                lightning_probability=probability,
                lightning_rule=rule,
                question_template_id=question_template_id,
            )
        if preview is not None:
            preview = self.preview_store().consume(bet.title, requested_at)
        if preview is not None:
            return dict(
                lane="lightning_decision",
                sampled_bet_index=index,
                request_prices=prices,
                requested_at=preview["requested_at"],
                lightning_preview_title=bet.title,
                lightning_preview_probability=preview["p_yes"],
                question_template_id=question_template_id,
            )
        if tool is None:
            queue = FreshQueue(queue.path)
            queue.pending.append(raw)
            queue.pending.sort(
                key=lambda row: (int(row["creationTimestamp"]), row["id"])
            )
            del queue.asked[bet.id]
            queue.save()
            yield from self.sleep(cfg.poll_s)
            return {"lane": "wait"}
        return dict(
            lane="market",
            sampled_bet_index=index,
            mech_tool=tool,
            request_prices=prices,
            requested_at=requested_at,
            question_template_id=question_template_id,
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
        yield from self._refresh_base_rate(now)
        now = time.time()
        poll_s = self._discovery_period()
        if (
            cfg.in_window(now) or queue.pending
        ) and now - state.fast_last_poll >= poll_s:
            state.fast_last_poll = now
            state.fast_polling = True
            try:
                rows = yield from self._poll_markets(queue, now)
                committed_at = time.time()
                if rows is not None:
                    # The foreground can pop a market while the HTTP request waits.
                    queue = FreshQueue(queue.path)
                    queue.ingest(
                        rows,
                        committed_at,
                        self._discovery_creators(),
                        cfg.fresh_max_age_s,
                        cfg.overlap_s,
                    )
                    queue.save()
            except TimeoutException:
                self.context.logger.warning(
                    "Fresh-market discovery timed out; retry on the next poll."
                )
            finally:
                state.fast_polling = False
        yield from self.sleep(max(1.0, self._discovery_period() - (time.time() - now)))


class PreviewDeadlineBehaviour(PreviewMixin, MechInteractBaseBehaviour, ABC):
    """Stop preview foreground work at its original absolute deadline."""

    def get_callback_request(self) -> Callable:
        """Ignore a reply from work cancelled at the preview deadline.

        :return: callback bound to the current preview request phase.
        """
        expired = getattr(self, "_preview_deadline_reached", False)
        callback = super().get_callback_request()

        def receive(message: Any, current_behaviour: Any) -> None:
            """Keep cancelled I/O replies out of the failure consensus wait."""
            if expired == getattr(self, "_preview_deadline_reached", False):
                callback(message, current_behaviour)

        return receive

    def act(self) -> None:
        """Check the deadline even when an I/O response never arrives."""
        db = self.synchronized_data.db
        if (
            not getattr(self, "_preview_deadline_reached", False)
            and not getattr(self, "_preview_work_finished", False)
            and lightning_controls(self).lightning_preview
            and db.get("lightning_preview_request", False) is True
            and time.time()
            >= db.get_strict("requested_at")
            + min(
                300.0,
                self.params.mech_marketplace_config.offchain_poll_timeout_seconds,
            )
        ):
            self.stop()
            self._on_sent_message()
            self._preview_deadline_reached = True
            self.preview_store().answer_failed(
                db.get_strict("lightning_preview_title"), time.time()
            )
        super().act()

    def finish_behaviour(self, payload: BaseTxPayload) -> Generator:
        """Keep an on-time result while consensus on its payload completes.

        :param payload: completed work's consensus payload.
        :yield: consensus I/O.
        """
        self._preview_work_finished = True
        yield from super().finish_behaviour(payload)


class FastMechRequestBehaviour(PreviewDeadlineBehaviour, TraderMechRequestBehaviour):
    """Keep the normal request path and bound paid preview requests."""

    def _run_offchain_request_cycle(self) -> Generator:
        """Publish the usual failed-request event after a preview deadline."""
        if not getattr(self, "_preview_deadline_reached", False):
            yield from super()._run_offchain_request_cycle()
            return
        result = OffchainCycleResult(
            offchain_result=MechEvent.OFFCHAIN_ALL_FAILED.value,
            mech_requests_json="[]",
            mech_responses_json="[]",
            last_failure_reason="preview_deadline",
        )
        yield from self.finish_behaviour(self._payload_from_offchain_result(result))


class PreviewDeadlinePoller(OffchainResponsePoller):
    """Use normal unanswered handling after a preview's deadline."""

    def _poll_until_terminal(
        self, mech_url: str, request_id_int_str: str
    ) -> Generator[None, None, _PollSnapshot]:
        """Report the expired work through the normal poller path.

        :param mech_url: endpoint of the unanswered mech.
        :param request_id_int_str: unanswered request identifier.
        :yield: no further I/O after the deadline.
        :return: an unanswered preview result.
        """
        yield from ()
        return _PollSnapshot(
            status="rejected", error="preview_deadline", unanswered=True
        )


class FastMechResponseBehaviour(PreviewDeadlineBehaviour, TraderMechResponseBehaviour):
    """Keep normal response polling and bound paid preview responses."""

    def _run_offchain_response_cycle(self) -> Generator:
        """Publish a failed response after the absolute preview deadline."""
        if not getattr(self, "_preview_deadline_reached", False):
            yield from super()._run_offchain_response_cycle()
            return
        responses = yield from PreviewDeadlinePoller(self).run()
        payload = JSONPayload(
            self.context.agent_address, serialise_responses(responses)
        )
        yield from self.finish_behaviour(payload)


class FastDecisionRequestBehaviour(DecisionRequestBehaviour):
    """Keep the normal request path without invented preview market context."""

    def setup(self) -> None:
        """Use the same prompt and tool for a proposed exact title."""
        super().setup()
        if (
            lightning_controls(self).lightning_preview
            and self.synchronized_data.db.get("lightning_preview_request", False)
            is True
            and self._metadata is not None
        ):
            self._metadata = replace(self._metadata, request_context=None)


class FastDecisionBehaviour(PreviewMixin, DecisionMakerBaseBehaviour):
    """Apply the fixed strategy to one response and a live pool quote."""

    matching_round = FastDecisionRound

    def _restore_preview_market(self, bet: Bet) -> None:
        """Return the selected market to its normal request path.

        :param bet: market whose consumed preview is now too old.
        """
        fields = asdict(bet)
        row = {key: fields[key] for key in _FIELDS.split() if key in fields}
        row["condition"] = {"id": bet.condition_id}
        queue = FreshQueue(self.params.store_path / "fresh_watch.json")
        pending = {item["id"]: item for item in queue.pending}
        pending.setdefault(bet.id, row)
        queue.pending = sorted(
            pending.values(),
            key=lambda item: (int(item["creationTimestamp"]), item["id"]),
        )
        queue.asked.pop(bet.id, None)
        queue.save()

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
            request_price=None,
            executed_price=None,
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
        lightning = lightning_controls(self)
        canonical = (
            natural_yes_index(
                {
                    "outcomes": bet.outcomes,
                    "question": {"templateId": db.get("question_template_id", None)},
                }
            )
            is not None
        )
        still_hold = bool(
            lightning.lightning_still_hold
            and canonical
            and still_hold_rule(bet.title, bet.creator, lightning)
        )
        direct = db.get("lightning_probability", None) if still_hold else None
        direct = direct if isinstance(direct, (int, float)) else None
        preview_probability = (
            db.get("lightning_preview_probability", None)
            if lightning.lightning_preview and canonical
            else None
        )
        preview_probability = (
            preview_probability
            if isinstance(preview_probability, (int, float))
            else None
        )
        prediction = (
            None
            if direct is not None or preview_probability is not None
            else self._get_decision_prediction()
        )
        if (
            lightning.lightning_preview
            and db.get("lightning_preview_request", False) is True
        ):
            title = db.get_strict("lightning_preview_title")
            store = self.preview_store()
            for orphan in store.expire(now):
                self.preview_log(orphan, "orphan_fee_loss")
            if title not in store.entries:
                result["reason"] = "preview_expired"
            elif prediction is None or now >= requested_at + min(
                300.0,
                self.params.mech_marketplace_config.offchain_poll_timeout_seconds,
            ):
                store.answer_failed(title, now)
                result["reason"] = "preview_answer_failed"
            else:
                store.answer(title, prediction.p_yes, now)
                result["reason"] = "preview_answer_cached"
                result["p_tool"] = prediction.p_yes
            entry = store.entries.get(title)
            if entry is not None:
                self.preview_log(entry, str(result["reason"]))
            remove_preview_bets(self)
            return result
        if preview_probability is not None and now - requested_at >= 900:
            self._restore_preview_market(bet)
            result["reason"] = "preview_expired"
            return result
        if direct is None and preview_probability is None and prediction is None:
            return result
        if direct is not None:
            p_yes = direct
        elif preview_probability is not None:
            p_yes = preview_probability
        else:
            p_yes = cast(PredictionResponse, prediction).p_yes
        result["p_tool"] = None if direct is not None else p_yes
        result["lightning_rule"] = (
            db.get("lightning_rule", None) if direct is not None else None
        )
        balances = yield from self._get_pool_balances(
            bet.id, bet.collateralToken, bet.condition_id
        )
        if preview_probability is not None and time.time() - requested_at >= 900:
            self._restore_preview_market(bet)
            result["reason"] = "preview_expired"
            return result
        if balances is None:
            result["reason"] = "live_price_unavailable"
            return result
        if bet.openingTimestamp <= time.time():
            result["reason"] = "market_closed"
            return result
        cfg = self.params.fast
        if still_hold:
            cache = BaseRateCache(self.params.store_path / "lightning_base_rate.json")
            prior = cache.current_rate(now, lightning)
            cfg = lightning_config(
                cfg,
                lightning,
                lightning.still_hold_prior if prior is None else prior,
            )
        if direct is not None:
            cfg = replace(
                cfg,
                prior_weight=1.0,
                max_price=lightning.lightning_still_hold_max_price,
            )
        decision = decide(
            bet.title,
            p_yes,
            balances,
            db.get_strict("request_prices"),
            cfg,
            bet.fee,
            yes_only=still_hold,
        )
        result.update(asdict(decision))
        result["request_price"] = db.get_strict("request_prices")[decision.side]
        result["executed_price"] = decision.price
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

    _placement_failure = "other"
    _executed_price: Optional[float] = None
    _price_ceiling: Optional[float] = None
    _min_tokens_out: Optional[int] = None

    def async_act(self) -> Generator:
        """Clear quote state before each placement attempt."""
        yield from self.wait_for_lightning_lane()
        self._placement_failure = "other"
        self._executed_price = None
        self._price_ceiling = None
        self._min_tokens_out = None
        if lightning_controls(self).lightning_lane:
            bet = self.sampled_bet
            mined = self.context.shared_state.get("lightning_lane_bet_markets", set())
            if bet.id in mined or bet.has_investments:
                self._placement_failure = "lane_already_bet"
                yield from self.finish_behaviour(
                    BetPlacementPayload(
                        self.context.agent_address,
                        tx_submitter=self.matching_round.auto_round_id(),
                        tx_hash=None,
                    )
                )
                return
        yield from super().async_act()

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
        self._placement_failure = "quote"
        self._executed_price = None
        self._price_ceiling = None
        self._min_tokens_out = None
        success = yield from super()._calc_buy_amount()
        if not success or self.quoted_tokens <= 0:
            return False
        price = self.investment_amount / self.quoted_tokens
        self._executed_price = price
        requested = self.synchronized_data.db.get_strict("request_prices")[
            self.outcome_index
        ]
        cfg = self.params.fast
        if price > self._execution_max_price():
            self._placement_failure = "max_price"
            return False
        if price - requested > cfg.max_slip + 1e-12:
            self._placement_failure = "max_slip"
            return False
        self._placement_failure = "edge_recheck"
        prediction = self._get_decision_prediction()
        if prediction is None:
            return False
        p_side = prediction.p_yes if self.outcome_index == 0 else prediction.p_no
        bounds = self._ceiling_min_out(p_side, requested)
        if bounds is None:
            return False
        self._price_ceiling, self.buy_amount = bounds
        self._min_tokens_out = self.buy_amount
        return True

    def _execution_max_price(self) -> float:
        """Apply the direct still-hold price cap to the final live quote."""
        maximum = self.params.fast.max_price
        cfg = lightning_controls(self)
        db = self.synchronized_data.db
        if (
            cfg.lightning_still_hold
            and db.get("lightning_rule", None) == "still_hold_phrase"
            and isinstance(db.get("lightning_probability", None), (int, float))
            and natural_yes_index(
                {
                    "outcomes": self.sampled_bet.outcomes,
                    "question": {"templateId": db.get("question_template_id", None)},
                }
            )
            is not None
        ):
            maximum = cfg.lightning_still_hold_max_price
        return maximum

    def _ceiling_min_out(
        self, p_side: float, requested_price: float
    ) -> Optional[Tuple[float, int]]:
        """Keep the enforced edge when the pool moves before execution.

        :param p_side: calibrated probability for the chosen outcome.
        :param requested_price: price when the prediction was requested.
        :return: price ceiling and minimum tokens, or None if no margin remains.
        """
        cfg = self.params.fast
        return execution_ceiling(
            self.investment_amount,
            self.quoted_tokens,
            p_side,
            requested_price,
            self._execution_max_price(),
            cfg.max_slip,
            self.params.placement_min_edge,
        )

    def _preflight_ok(self) -> Generator[None, None, bool]:
        """Estimate this owner's Safe call before submitting it for settlement.

        :yield: ledger API I/O.
        :return: False for a revert; True for success or an unrelated RPC error.
        """
        owner = self.context.agent_address
        signature = bytes.fromhex(owner[2:]).rjust(32, b"\0") + bytes(32) + b"\1"
        zero_address = "0x" + "00" * 20
        # Encode the Safe execTransaction call with the approved owner signature.
        data = bytes.fromhex("6a761202") + encode(
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
            [
                self.params.multisend_address,
                self.txs_value,
                self.multisend_data,
                SafeOperation.DELEGATE_CALL.value,
                SAFE_GAS,
                0,
                0,
                zero_address,
                zero_address,
                signature,
            ],
        )
        # Web3 rejects a lowercase address with hex letters before issuing the
        # estimate; checksum here so a valid Safe address never bypasses the check.
        safe_address = to_checksum_address(self.synchronized_data.safe_contract_address)
        transaction = {
            "from": owner,
            "to": safe_address,
            "data": "0x" + data.hex(),
        }
        # With safeTxGas and gasPrice zero, Safe turns a false execution into GS013.
        response = yield from self._estimate_gas(transaction, self.params.mech_chain_id)
        if response is None:
            error = "preflight deadline expired"
        elif response.performative == LedgerApiMessage.Performative.STATE:
            return bool(response.state.body.get("estimate_gas_result"))
        else:
            error = response.message or "unknown ledger error"
        if "revert" in error.lower() or "GS013" in error:
            return False
        self.context.logger.warning(
            "Fast bet preflight RPC error; proceeding: %s", error
        )
        return True

    def _estimate_gas(
        self, transaction: Dict[str, Any], chain_id: str
    ) -> Generator[None, None, Optional[LedgerApiMessage]]:
        """Issue estimate_gas and bound the wait for the ledger reply.

        :param transaction: estimate target with from, to and data fields.
        :param chain_id: ledger chain id.
        :yield: ledger API I/O.
        :return: the ledger response, or None once the preflight deadline expires.
        """
        dialogues = cast(LedgerApiDialogues, self.context.ledger_api_dialogues)
        message, dialogue = dialogues.create(
            counterparty=LEDGER_API_ADDRESS,
            performative=LedgerApiMessage.Performative.GET_STATE,  # type: ignore[arg-type]
            ledger_id=self.context.default_ledger_id,
            callable="estimate_gas",
            kwargs=LedgerApiMessage.Kwargs(
                {"transaction": transaction, "chain_id": chain_id}
            ),
            args=tuple(),
        )
        dialogue = cast(LedgerApiDialogue, dialogue)
        try:
            response = yield from request_with_timeout(
                self, message, dialogue, _PREFLIGHT_TIMEOUT_S
            )
        except TimeoutException:
            return None
        return response

    def _prepare_safe_tx(self) -> Generator[None, None, Optional[str]]:
        """Skip a prepared fast bet if the Safe execution would revert.

        :yield: contract and ledger API I/O.
        :return: settlement payload or None for a failed placement.
        """
        tx_hex = yield from super()._prepare_safe_tx()
        if tx_hex is not None and not (yield from self._preflight_ok()):
            self._placement_failure = "preflight_revert"
            return None
        return tx_hex

    def finish_behaviour(self, payload: Any) -> Generator:
        """Log the final bet preparation outcome once.

        :param payload: existing bet-placement payload.
        :yield: control during consensus.
        """
        result = json.loads(self.synchronized_data.db.get_strict("fast_decision"))
        result["age_at_bet"] = time.time() - self.sampled_bet.creationTimestamp
        result["prepared"] = bool(payload.tx_hash)
        if self.benchmarking_mode.enabled:
            result["reason"] = "benchmarking"
            result["prepared"] = None
        elif not payload.tx_hash:
            result["reason"] = (
                "balance" if payload.tx_submitter is None else self._placement_failure
            )
        result["request_price"] = self.synchronized_data.db.get_strict(
            "request_prices"
        )[self.outcome_index]
        result["executed_price"] = self._executed_price
        result["price_ceiling"] = self._price_ceiling
        result["min_tokens_out"] = self._min_tokens_out
        result["price"] = None
        result["edge"] = None
        if self._executed_price is not None:
            result["price"] = self._executed_price
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
        TradeCountBehaviour,
        MechOnlySelectionBehaviour,
        FastDecisionBehaviour,
        FastBetPlacementBehaviour,
        FastDecisionRequestBehaviour,
        PostBetUpdateBehaviour,
        HousekeepingRedeemBehaviour,
    }
