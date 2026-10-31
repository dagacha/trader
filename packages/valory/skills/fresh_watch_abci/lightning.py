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

"""Optional lightning controls and the local still-hold evidence cache."""

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from packages.valory.skills.fresh_watch_abci.strategy import FastConfig


@dataclass(frozen=True)
class LightningConfig:
    """Keep optional lightning paths off for existing fast agents."""

    lightning_still_hold: bool = False
    lightning_still_hold_max_price: float = 0.93
    lightning_base_rate_window_days: int = 21
    lightning_base_rate_min_n: int = 60
    lightning_base_rate_floor: float = 0.90
    still_hold_prior: float = 0.85
    lightning_chain_detect: bool = False
    omen_fpmm_factory: str = "0x9083A2B699c0a4AD06F63580BDE2635d26a3eeF0"
    lightning_rpc_fallback_s: int = 60
    lightning_preview: bool = False
    lightning_preview_max_per_night: int = 15
    lightning_creators: List[str] = field(default_factory=lambda: FastConfig().creators)
    lightning_lane: bool = False
    lightning_rpc_url: str = field(default="", repr=False)
    lightning_poll_offset_s: float = 0.3
    lightning_poll_step_s: float = 0.25
    lightning_lane_max_age_s: int = 6
    lightning_lane_gas_limit: int = 400000
    lightning_lane_tip_gwei: float = 0.0
    lightning_lane_tip_cap_gwei: float = 1000.0
    lightning_lane_inflight_timeout_s: int = 60
    # -1 selects MAX_TRADES when FastParams loads the deployment controls.
    lightning_lane_max_per_night: int = 3

    def __post_init__(self) -> None:
        """Reject controls that cannot give a bounded probability or budget."""
        if not (
            math.isfinite(self.lightning_lane_tip_gwei)
            and math.isfinite(self.lightning_lane_tip_cap_gwei)
            and 0
            <= self.lightning_lane_tip_gwei
            <= self.lightning_lane_tip_cap_gwei
            <= 2000
            and math.isfinite(self.lightning_poll_offset_s)
            and math.isfinite(self.lightning_poll_step_s)
            and self.lightning_poll_offset_s >= 0
            and self.lightning_poll_step_s > 0
            and self.lightning_lane_max_age_s > 0
            and 100000 <= self.lightning_lane_gas_limit <= 2000000
            and self.lightning_lane_inflight_timeout_s > 0
            and self.lightning_lane_max_per_night >= -1
        ):
            raise ValueError("Lane fee, polling, and budget controls are invalid.")
        if not self.lightning_creators or any(
            re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is None
            for value in [self.omen_fpmm_factory, *self.lightning_creators]
        ):
            raise ValueError("Require valid factory and creator addresses.")
        if not (
            0 < self.lightning_still_hold_max_price < 1
            and 0 <= self.still_hold_prior <= 1
            and 0 <= self.lightning_base_rate_floor <= 1
        ):
            raise ValueError(
                "Lightning probability controls must be between zero and one."
            )
        if (
            self.lightning_base_rate_window_days < 1
            or self.lightning_base_rate_min_n < 1
            or self.lightning_rpc_fallback_s < 0
            or self.lightning_preview_max_per_night < 0
        ):
            raise ValueError("Lightning windows and budgets must be positive.")


def preview_enabled(behaviour: Any) -> bool:
    """Require the off-chain route before any preview work.

    :param behaviour: behaviour with the preview controls.
    :return: whether the configured request route supports previews.
    """
    if getattr(behaviour.params, "use_offchain", False) is True:
        return True
    shared = behaviour.context.shared_state
    if shared.get("lightning_preview_disabled") is not True:
        behaviour.context.logger.info(
            "[LIGHTNING_PREVIEW] off reason=requires USE_OFFCHAIN=true"
        )
        shared["lightning_preview_disabled"] = True
    return False


def natural_yes_index(bet: Mapping[str, Any]) -> Optional[int]:
    """Identify a natural-YES payout without inferring missing metadata.

    :param bet: discovered market with its Realitio question metadata.
    :return: outcome zero for the supported order and template, else None.
    """
    question = bet.get("question")
    if (
        bet.get("outcomes") == ["Yes", "No"]
        and isinstance(question, dict)
        and str(question.get("templateId")) == "2"
    ):
        return 0
    return None


def still_hold_rule(title: str, creator: str, cfg: LightningConfig) -> Optional[str]:
    """Match the explicit phrase only for a configured creator."""
    if creator.lower() in {
        value.lower() for value in cfg.lightning_creators
    } and re.search(r"\bstill hold\b", title, re.I):
        return "still_hold_phrase"
    return None


def lightning_config(
    fast: FastConfig, cfg: LightningConfig, prior: Optional[float] = None
) -> FastConfig:
    """Set the still-hold prior without changing other fast controls."""
    if prior is None and cfg.still_hold_prior == 0.85:
        return fast
    return replace(
        fast,
        priors={
            **fast.priors,
            "still_hold": cfg.still_hold_prior if prior is None else prior,
        },
    )


class BaseRateCache:
    """Store one complete daily resolved-market sample in the agent store."""

    def __init__(self, path: Path) -> None:
        """Load a snapshot, or leave the optional strategy disabled."""
        self.path = path
        try:
            self.data: Dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    @property
    def sample(self) -> int:
        """Return the number of valid binary resolutions."""
        return int(self.data.get("n", 0))

    @property
    def rate(self) -> Optional[float]:
        """Return the observed YES rate if a sample exists."""
        return self.data.get("rate")

    def refresh_due(self, now: float, cfg: LightningConfig, fast: FastConfig) -> bool:
        """Refresh daily, or retry unusable evidence after 30 minutes."""
        try:
            attempted = float(self.path.with_suffix(".attempt.json").read_text())
        except (OSError, ValueError):
            attempted = -86400
        if not cfg.lightning_still_hold:
            return False
        last = max(attempted, self.data.get("refreshed_at", -86400))
        if self.usable(now, cfg) is None:
            return now - last >= 1800
        return not fast.in_window(now) and last // 86400 < int(now) // 86400

    def reserve_refresh(self, now: float) -> None:
        """Reserve a refresh before either behaviour starts HTTP I/O."""
        self.path.with_suffix(".attempt.json").write_text(str(now))

    def current_rate(self, now: float, cfg: LightningConfig) -> Optional[float]:
        """Return current evidence for the configured window and creators."""
        creators = sorted(value.lower() for value in cfg.lightning_creators)
        if (
            self.data.get("template_id") != 2
            or self.data.get("creators") != creators
            or self.data.get("window_days") != cfg.lightning_base_rate_window_days
            or not 0 <= now - self.data.get("refreshed_at", 0) <= 172800
        ):
            return None
        return self.rate

    def usable(self, now: float, cfg: LightningConfig) -> Optional[float]:
        """Disable direct bets for stale, small, or low-rate samples."""
        if self.off_reason(now, cfg) is not None:
            return None
        return self.rate

    def off_reason(self, now: float, cfg: LightningConfig) -> Optional[str]:
        """Explain why the direct still-hold path has no usable evidence."""
        if self.rate is None:
            return "missing"
        if self.current_rate(now, cfg) is None:
            return "stale"
        if self.sample < cfg.lightning_base_rate_min_n:
            return "small"
        if self.rate < cfg.lightning_base_rate_floor:
            return "below_floor"
        return None

    def update(
        self, rows: List[Dict[str, Any]], now: float, cfg: LightningConfig
    ) -> None:
        """Commit only a complete sample after creator, time, and payout checks."""
        cutoff = now - cfg.lightning_base_rate_window_days * 86400
        outcomes = []
        seen = set()
        for row in rows:
            if row.get("id") in seen:
                continue
            if (
                not still_hold_rule(row.get("title", ""), row.get("creator", ""), cfg)
                or natural_yes_index(row) is None
                or not cutoff <= int(row["creationTimestamp"]) <= now
            ):
                continue
            payouts = row.get("payouts")
            if not isinstance(payouts, list) or len(payouts) != 2:
                continue
            try:
                yes, no = (int(value) for value in payouts)
            except (TypeError, ValueError):
                continue
            if min(yes, no) < 0 or (yes > 0) == (no > 0):
                continue
            outcomes.append(yes > 0)
            seen.add(row.get("id"))
        n, yes = len(outcomes), sum(outcomes)
        self.data = dict(
            n=n,
            yes=yes,
            rate=yes / n if n else None,
            refreshed_at=now,
            window_days=cfg.lightning_base_rate_window_days,
            creators=sorted(value.lower() for value in cfg.lightning_creators),
            template_id=2,
        )
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data))
        temporary.replace(self.path)
