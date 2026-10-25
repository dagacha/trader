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

"""Pure strategy rules and a durable queue for the fast Omen agent."""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class FastConfig:
    """Fast-lane controls. Amounts use xDAI, prices use probabilities."""

    creators: List[str] = field(
        default_factory=lambda: [
            "0x89c5cc945dd550bcffb72fe42bff002429f46fec",
            "0xffc8029154ecd55abed15bd428ba596e7d23f557",
        ]
    )
    fresh_max_age_s: int = 600
    overlap_s: int = 120
    poll_s: int = 15
    active_window_utc: str = "00:00-05:30"
    housekeeping_interval_s: int = 600
    tools: List[str] = field(
        default_factory=lambda: [
            "superforcaster_calibrated_full_search",
            "superforcaster",
        ]
    )
    prior_weight: float = 0.5
    priors: Dict[str, float] = field(
        default_factory=lambda: {
            "still_hold": 0.85,
            "numeric_threshold": 0.60,
            "other_event": 0.18,
        }
    )
    min_edge: float = 0.03
    max_price: float = 0.90
    no_min_p: float = 0.70
    cap: float = 4.0
    kelly_bankroll: float = 10.0
    max_slip: float = 0.20
    price_margin: float = 0.01
    min_stake: float = 0.1

    def in_window(self, now: float) -> bool:
        """Check the half-open UTC window, including windows across midnight.

        :param now: Unix timestamp.
        :return: whether the timestamp is inside the active window.
        """
        start, end = self.active_window_utc.split("-")
        minute = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M")
        if start > end:
            return minute >= start or minute < end
        return start <= minute < end


def template(title: str) -> str:
    """Classify a title. Numeric phrases take priority.

    :param title: market question.
    :return: prior template name.
    """
    if re.search(
        r"\b(above|below|at least|at most|more than|less than|or more|or higher|exceed)\b",
        title,
        re.I,
    ):
        return "numeric_threshold"
    if re.search(r"\b(still|remain|continue|maintain|hold|stay|keep)\b", title, re.I):
        return "still_hold"
    return "other_event"


@dataclass(frozen=True)
class Decision:
    """The strategy result for one market."""

    template: str
    p_hat: float
    side: int
    price: float
    edge: float
    stake: float
    reason: str


def buy_amount(investment: int, balances: List[int], side: int, fee: int) -> int:
    """Quote a binary buy with the FPMM calcBuyAmount integer math.

    :param investment: collateral in wei.
    :param balances: YES and NO reserves in wei.
    :param side: outcome index.
    :param fee: market fee scaled by 10**18.
    :return: outcome tokens in wei.
    """
    one = 10**18
    net = investment - investment * fee // one
    numerator = balances[side] * one * balances[1 - side]
    denominator = balances[1 - side] + net
    ending = (numerator + denominator - 1) // denominator
    return balances[side] + net - (ending + one - 1) // one


def decide(
    title: str,
    p_tool: float,
    balances: List[int],
    requested: List[float],
    cfg: FastConfig,
    fee: int,
) -> Decision:
    """Find the largest stake within Kelly sizing and all execution bounds.

    :param title: market question.
    :param p_tool: tool probability for YES.
    :param balances: live YES and NO reserves in wei.
    :param requested: YES and NO prices at request time.
    :param cfg: strategy controls.
    :param fee: market fee scaled by 10**18.
    :return: a bet or an explicit skip reason.
    """
    kind = template(title)
    p_hat = cfg.prior_weight * p_tool + (1 - cfg.prior_weight) * cfg.priors[kind]
    probabilities = [p_hat, 1 - p_hat]
    if min(balances) <= 0 or not all(0 < value < 1 for value in requested):
        return Decision(kind, p_hat, 0, 0.0, 0.0, 0.0, "invalid_price")
    if not 0 <= fee < 10**18:
        return Decision(kind, p_hat, 0, 0.0, 0.0, 0.0, "invalid_fee")
    prices = [balances[1] / sum(balances), balances[0] / sum(balances)]
    side = max(range(2), key=lambda index: probabilities[index] - prices[index])
    probability = probabilities[side]

    def skip_reason(price: float) -> str:
        """Name the first failed execution bound.

        :param price: average execution price.
        :return: skip reason, or an empty string.
        """
        if price > cfg.max_price - cfg.price_margin:
            return "max_price"
        if side == 1 and probability < cfg.no_min_p:
            return "no_min_p"
        if probability - price + 1e-12 < cfg.min_edge + cfg.price_margin:
            return "min_edge"
        if price - requested[side] > cfg.max_slip - cfg.price_margin + 1e-12:
            return "max_slip"
        return ""

    # The zero-stake limit includes the fee. Impact can only increase the price.
    price = prices[side] / (1 - fee / 10**18)
    reason = skip_reason(price)
    if reason:
        return Decision(kind, p_hat, side, price, probability - price, 0.0, reason)

    # Leave the configured price margin for pool moves before placement.
    # Price rises with stake, while Kelly size falls. Bisect their feasible range.
    # Check CAP first so an allowed capped bet keeps its exact amount.
    low, high = 0.0, cfg.cap
    stake = high
    for _ in range(64):
        investment = int(stake * 10**18)
        tokens = buy_amount(investment, balances, side, fee)
        executed = investment / tokens if tokens > 0 else float("inf")
        if not skip_reason(executed) and stake <= (
            cfg.kelly_bankroll * (probability - executed) / (1 - executed)
        ):
            low = stake
            price = executed
        else:
            high = stake
        next_stake = (low + high) / 2
        if int(next_stake * 10**18) == investment:
            break
        stake = next_stake
    if low <= 0:
        return Decision(
            kind, p_hat, side, price, probability - price, 0.0, "no_feasible_stake"
        )
    if low < cfg.min_stake:
        return Decision(kind, p_hat, side, price, probability - price, 0.0, "min_stake")
    return Decision(kind, p_hat, side, price, probability - price, low, "")


def housekeeping_due(now: float, last: float, queued: bool, cfg: FastConfig) -> bool:
    """Give queued work priority and rate-limit housekeeping in the window.

    :param now: current timestamp.
    :param last: timestamp of the last housekeeping entry.
    :param queued: whether markets are waiting.
    :param cfg: lane controls.
    :return: whether to enter housekeeping.
    """
    return not queued and (
        not cfg.in_window(now) or now - last >= cfg.housekeeping_interval_s
    )


class FreshQueue:
    """Keep pending markets, the cursor, and the asked set in one atomic file."""

    def __init__(self, path: Path) -> None:
        """Load durable state; fail closed if the state is corrupt.

        :param path: queue file in the agent store.
        """
        self.path = path
        data = json.loads(path.read_text()) if path.exists() else {}
        self.pending: List[Dict[str, Any]] = data.get("pending", [])
        self.last_seen: int = data.get("last_seen", 0)
        asked = data.get("asked", {})
        # Old files lack timestamps. The watermark bounds their creation times.
        self.asked: Dict[str, int] = (
            {market_id: self.last_seen for market_id in asked}
            if isinstance(asked, list)
            else asked
        )
        self.last_housekeeping: float = data.get("last_housekeeping", 0.0)

    def ingest(
        self,
        rows: List[Dict[str, Any]],
        now: float,
        creators: List[str],
        max_age: int,
        overlap_s: int = 120,
    ) -> None:
        """Add fresh allowed markets in creation order.

        :param rows: complete subgraph result after pagination.
        :param now: current timestamp.
        :param creators: allowed creator addresses.
        :param max_age: maximum age at discovery.
        :param overlap_s: query overlap and extra asked-ID retention margin.
        """
        # Retain the age boundary plus a margin of at least the query overlap.
        # Pruned markets are too old for both the query and discovery filter.
        cutoff = now - max_age - max(0, overlap_s)
        self.asked = {
            market_id: created
            for market_id, created in self.asked.items()
            if created >= cutoff
        }
        allowed = {creator.lower() for creator in creators}
        pending = {row["id"]: row for row in self.pending}
        for row in rows:
            created = int(row["creationTimestamp"])
            if created > now:
                continue
            self.last_seen = max(self.last_seen, created)
            if (
                now - created <= max_age
                and int(row["openingTimestamp"]) > now
                and row["creator"].lower() in allowed
                and row["id"] not in self.asked
            ):
                pending.setdefault(row["id"], row)
        self.pending = sorted(
            pending.values(), key=lambda row: (int(row["creationTimestamp"]), row["id"])
        )

    def save(self) -> None:
        """Atomically save the queue before any paid request."""
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dict(
                    pending=self.pending,
                    asked=self.asked,
                    last_seen=self.last_seen,
                    last_housekeeping=self.last_housekeeping,
                )
            )
        )
        temporary.replace(self.path)

    def pop(self) -> Optional[Dict[str, Any]]:
        """Mark one market asked before returning it.

        :return: oldest pending market, or None.
        """
        if not self.pending:
            return None
        row = self.pending.pop(0)
        self.asked[row["id"]] = int(row["creationTimestamp"])
        self.save()
        return row
