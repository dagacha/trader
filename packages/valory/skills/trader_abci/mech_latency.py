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

"""Measure Mech answers in the shared trader request and response path."""

import json
import random
import time
from dataclasses import asdict
from typing import Any, Dict, Generator, List, Optional, Tuple

from packages.valory.skills.abstract_round_abci.base import BaseTxPayload
from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainAttemptOutcome,
    OffchainAttemptResult,
    OffchainCycleResult,
    OffchainRequestExecutor,
    PaymentChallenge,
    PendingRequest,
    _DepositBuildResult,
)
from packages.valory.skills.mech_interact_abci.behaviours.purchase_subcription import (
    MechPurchaseSubscriptionBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.request import (
    MechRequestBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.response import (
    MechResponseBehaviour,
)
from packages.valory.skills.mech_interact_abci.states.base import Event
from packages.valory.skills.staking_abci.lane_guard import LaneGuardMixin
from packages.valory.skills.tx_settlement_multiplexer_abci.behaviours import (
    PostTxSettlementBehaviour,
)

TIMINGS = "mech_latency"


def request_timing(
    metadata: Dict[str, Any], sent_ts: Optional[float]
) -> Dict[str, Any]:
    """Build local timing fields from request metadata.

    :param metadata: tool metadata.
    :param sent_ts: POST time or None until settlement.
    :return: local timing record.
    """
    return dict(
        tool=metadata["tool"],
        market=(metadata.get("request_context") or {}).get("market_id", "-"),
        sent_ts=sent_ts,
    )


class TimedOffchainRequestExecutor(OffchainRequestExecutor):
    """Record each POST time, including the retry after a deposit."""

    def _build_deposit_tx(
        self,
        challenge: PaymentChallenge,
        payment_type: bytes,
        delivery_rate: int,
        tracker_balance: int,
    ) -> Generator[None, None, _DepositBuildResult]:
        """Reserve the Safe before an off-chain request prepares a deposit.

        :param challenge: validated payment challenge.
        :param payment_type: payment selector.
        :param delivery_rate: current Mech fee.
        :param tracker_balance: current tracker balance.
        :yield: control while the lane settles and the deposit is prepared.
        :return: prepared deposit or its failure reason.
        """
        yield from self._b.wait_for_lightning_lane(reserve=True)
        return (
            yield from super()._build_deposit_tx(
                challenge, payment_type, delivery_rate, tracker_balance
            )
        )

    def run(self) -> Generator[None, None, OffchainCycleResult]:
        """Release the last fast target when a request cycle fails."""
        self._cycle_mech: Optional[str] = None
        result = yield from super().run()
        if (
            getattr(self._b.params, "fast_dynamic_mech", False) is True
            and self._cycle_mech is not None
        ):
            state = self._b.context.shared_state.setdefault("fast_mech_rotation", {})
            if result.offchain_result not in (
                Event.OFFCHAIN_DONE.value,
                Event.OFFCHAIN_DEPOSIT_NEEDED.value,
            ):
                state["failed_mech"] = self._cycle_mech
            elif (
                result.offchain_result == Event.OFFCHAIN_DONE.value
                and (state.get("failed_mech") or "").lower() == self._cycle_mech.lower()
            ):
                state.pop("failed_mech", None)
        return result

    def _retry_pending(
        self, pending: PendingRequest
    ) -> Generator[None, None, OffchainCycleResult]:
        """Track the pending target even if its retry fails before the POST."""
        self._cycle_mech = pending.mech_address
        return (yield from super()._retry_pending(pending))

    def _post_signed_request(
        self,
        mech_url: str,
        mech_address: str,
        ipfs_hash: str,
        ipfs_data: str,
        request_id_bytes: bytes,
        signature_hex: str,
        nonce: int,
        delivery_rate: int,
        sender: str,
    ) -> Generator[None, None, OffchainAttemptResult]:
        """Start the clock immediately before the shared POST path."""
        metadata = json.loads(ipfs_data)
        timings = self._b.context.shared_state.setdefault(TIMINGS, {})
        timings[metadata["nonce"]] = request_timing(metadata, time.time())
        timings[metadata["nonce"]]["mech"] = mech_address
        attempt = yield from super()._post_signed_request(
            mech_url,
            mech_address,
            ipfs_hash,
            ipfs_data,
            request_id_bytes,
            signature_hex,
            nonce,
            delivery_rate,
            sender,
        )
        if getattr(
            self._b.params, "fast_dynamic_mech", False
        ) is True and attempt.outcome in (
            OffchainAttemptOutcome.TIMEOUT,
            OffchainAttemptOutcome.SERVER_BUSY,
            OffchainAttemptOutcome.BAD_RESPONSE,
        ):
            self._b.shared_state.penalize_mech(mech_address)
            self._logger.info(
                "[MECH_ROTATE] penalize=%s reason=post_%s",
                mech_address,
                attempt.outcome.value,
            )
        return attempt

    def _pick_next_mech(
        self, attempted: List[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Keep the priority fallback out of fast dynamic selection."""
        if getattr(self._b.params, "fast_dynamic_mech", False) is not True:
            return super()._pick_next_mech(attempted)
        attempted_lc = {address.lower() for address in attempted}
        penalties = {
            address.lower(): timestamp
            for address, timestamp in self._b.shared_state.penalized_mechs.items()
        }
        candidates = [
            mech
            for mech in self._synced.ranked_mechs
            if mech.http_url and mech.address.lower() not in attempted_lc
        ]
        eligible = [
            mech for mech in candidates if mech.address.lower() not in penalties
        ]
        if not eligible:
            if not candidates:
                return None, None
            eligible = [
                min(
                    candidates,
                    key=lambda mech: penalties[mech.address.lower()],
                )
            ]
            self._logger.warning(
                "Fast Mech fallback=%s reason=all_penalized penalized_at=%s",
                eligible[0].address,
                penalties[eligible[0].address.lower()],
            )
        state = self._b.context.shared_state.setdefault("fast_mech_rotation", {})
        current = (state.get("current_mech") or "").lower()
        elapsed = state.get("last_latency_s")
        threshold = self._b.params.fast_mech_rotate_latency_s
        selected = next(
            (mech for mech in eligible if mech.address.lower() == current),
            None,
        )
        alternatives = [mech for mech in eligible if mech.address.lower() != current]
        rotate = (
            selected is not None
            and elapsed is not None
            and (state.get("last_answer_mech") or "").lower() == current
            and threshold > 0
            and elapsed > threshold
            and alternatives
        )
        count = state.get("request_count", 0)
        unavailable = bool(current) and selected is None
        failed = (
            bool(current) and current == (state.pop("failed_mech", None) or "").lower()
        )
        if rotate or unavailable or (failed and alternatives):
            selected = random.Random(  # nosec B311 - spread requests, not secrets
                f"{self._synced.safe_contract_address.lower()}:{count}"
            ).choice(sorted(alternatives, key=lambda mech: mech.address.lower()))
            if failed:
                self._logger.info(
                    "[MECH_ROTATE] from=%s to=%s reason=request_failed",
                    current,
                    selected.address,
                )
            elif unavailable:
                self._logger.info(
                    "[MECH_ROTATE] from=%s to=%s reason=unavailable",
                    current,
                    selected.address,
                )
            else:
                self._logger.info(
                    "[MECH_ROTATE] from=%s to=%s reason=latency_s=%s threshold=%s",
                    current,
                    selected.address,
                    elapsed,
                    threshold,
                )
        else:
            if selected is None:
                selected = eligible[0]
                if self._b.params.fast_mech_start == "hash":
                    ordered = sorted(eligible, key=lambda mech: mech.address.lower())
                    selected = ordered[
                        int(self._synced.safe_contract_address, 16) % len(ordered)
                    ]
            self._logger.info(
                "[MECH_ROTATE] keep=%s latency_s=%s", selected.address, elapsed
            )
        state.update(current_mech=selected.address, request_count=count + 1)
        self._cycle_mech = selected.address
        return selected.address, selected.http_url


class TraderMechPurchaseSubscriptionBehaviour(
    LaneGuardMixin, MechPurchaseSubscriptionBehaviour
):
    """Guard the Safe before preparing an on-chain Mech subscription."""

    behaviour_id = MechPurchaseSubscriptionBehaviour.auto_behaviour_id()


class TraderMechRequestBehaviour(LaneGuardMixin, MechRequestBehaviour):
    """Use the shared request code with local timing records."""

    def setup(self) -> None:
        """Keep tool metadata before on-chain preparation consumes it."""
        super().setup()
        if self.params.is_running_on_polymarket is not True:
            self.context.shared_state[TIMINGS] = {
                meta.nonce: request_timing(asdict(meta), None)
                for meta in self.synchronized_data.mech_requests
            }

    def _run_offchain_request_cycle(self) -> Generator:
        """Use the shared executor with timing around its POST calls."""
        if self.params.is_running_on_polymarket is True:
            yield from super()._run_offchain_request_cycle()
            return
        self._check_round_timeout_fits_poll_budget()
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            result = yield from TimedOffchainRequestExecutor(self).run()
            payload = self._payload_from_offchain_result(result)
        yield from self.finish_behaviour(payload)


class TraderPostTxSettlementBehaviour(PostTxSettlementBehaviour):
    """Start on-chain answer timing when request settlement completes."""

    def _on_tx_settled(self) -> None:
        """Stamp request settlement, then run the existing handlers."""
        if self.synchronized_data.tx_submitter == "mech_request_round":
            now = time.time()
            for timing in self.context.shared_state.get(TIMINGS, {}).values():
                if timing["sent_ts"] is None:
                    timing["sent_ts"] = now
                    timing["mech"] = self.shared_state.last_called_mech
        super()._on_tx_settled()


class TraderMechResponseBehaviour(MechResponseBehaviour):
    """Log each Mech answer once, including mech-only requests."""

    def _log_answer(self, response: Dict[str, Any]) -> None:
        """Write timing fields for an answer with a known send time."""
        timing = self.context.shared_state.get(TIMINGS, {}).get(response.get("nonce"))
        if (
            not timing
            or timing.get("answered_ts") is not None
            or timing["sent_ts"] is None
        ):
            return
        if response.get("result") is None:
            self._release_on_missing_result(response, timing)
            return
        now = time.time()
        timing.update(
            answered_ts=now,
            latency_s=now - timing["sent_ts"],
            mech=response.get("mech_address") or timing.get("mech") or "-",
        )
        if (
            getattr(self.params, "fast_dynamic_mech", False) is True
            and timing["mech"] != "-"
        ):
            state = self.context.shared_state.setdefault("fast_mech_rotation", {})
            state.update(
                current_mech=timing["mech"],
                last_answer_mech=timing["mech"],
                last_latency_s=timing["latency_s"],
            )
        self.context.logger.info(
            "[MECH_LATENCY] tool=%s mech=%s offchain=%s sent_ts=%s answered_ts=%s latency_s=%.3f market=%s",
            timing["tool"],
            timing["mech"],
            str(self.params.use_offchain is True).lower(),
            timing["sent_ts"],
            now,
            timing["latency_s"],
            timing["market"],
        )

    def _release_on_missing_result(
        self, response: Dict[str, Any], timing: Dict[str, Any]
    ) -> None:
        """Release the current fast target when a terminal poll has no result."""
        if getattr(self.params, "fast_dynamic_mech", False) is not True:
            return
        mech = response.get("mech_address") or timing.get("mech") or ""
        if not mech or mech == "-":
            return
        state = self.context.shared_state.setdefault("fast_mech_rotation", {})
        state["failed_mech"] = mech

    def _get_response(self) -> Generator[None, None, bool]:
        """Log on-chain answers as soon as the result is read."""
        success = yield from super()._get_response()
        if success:
            self._log_answer(asdict(self.current_mech_response))
        return success

    def finish_behaviour(self, payload: BaseTxPayload) -> Generator:
        """Log off-chain answers before consensus; skip duplicate answers."""
        if self.params.is_running_on_polymarket is not True:
            for response in json.loads(getattr(payload, "information", None) or "[]"):
                self._log_answer(response)
        yield from super().finish_behaviour(payload)
