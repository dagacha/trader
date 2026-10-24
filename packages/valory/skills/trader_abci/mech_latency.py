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
import time
from dataclasses import asdict
from typing import Any, Dict, Generator, List, Optional, Tuple

from packages.valory.skills.abstract_round_abci.base import BaseTxPayload
from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainAttemptResult,
    OffchainRequestExecutor,
)
from packages.valory.skills.mech_interact_abci.behaviours.request import (
    MechRequestBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.response import (
    MechResponseBehaviour,
)
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
        return (
            yield from super()._post_signed_request(
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
        )

    def _pick_next_mech(
        self, attempted: List[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Keep the priority fallback out of fast dynamic selection."""
        if getattr(self._b.params, "fast_dynamic_mech", False) is not True:
            return super()._pick_next_mech(attempted)
        excluded = {
            address.lower()
            for address in [*attempted, *self._b.shared_state.penalized_mechs]
        }
        for mech in self._synced.ranked_mechs:
            if mech.http_url and mech.address.lower() not in excluded:
                return mech.address, mech.http_url
        return None, None


class TraderMechRequestBehaviour(MechRequestBehaviour):
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
        super()._on_tx_settled()


class TraderMechResponseBehaviour(MechResponseBehaviour):
    """Log each Mech answer once, including mech-only requests."""

    def _log_answer(self, response: Dict[str, Any]) -> None:
        """Write timing fields for an answer with a known send time."""
        timing = self.context.shared_state.get(TIMINGS, {}).get(response.get("nonce"))
        if (
            not timing
            or timing.get("answered_ts") is not None
            or response.get("result") is None
            or timing["sent_ts"] is None
        ):
            return
        now = time.time()
        timing.update(
            answered_ts=now,
            latency_s=now - timing["sent_ts"],
            mech=response.get("mech_address") or "-",
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
