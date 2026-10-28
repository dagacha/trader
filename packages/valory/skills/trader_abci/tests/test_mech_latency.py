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

"""Test request timing in the shared Mech path."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainAttemptOutcome,
    OffchainAttemptResult,
    OffchainRequestExecutor,
)
from packages.valory.skills.mech_interact_abci.behaviours.offchain_response import (
    serialise_responses,
)
from packages.valory.skills.mech_interact_abci.behaviours.request import (
    MechRequestBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.response import (
    MechResponseBehaviour,
)
from packages.valory.skills.mech_interact_abci.payloads import JSONPayload
from packages.valory.skills.mech_interact_abci.payment_types import PaymentType
from packages.valory.skills.mech_interact_abci.states.base import (
    Event,
    MechInteractionResponse,
    MechMetadata,
    OFFCHAIN_503_ALL_MECHS,
    OFFCHAIN_BAD_RESPONSE,
)
from packages.valory.skills.trader_abci import mech_latency as latency


def done(value: Any) -> Any:
    """Return the result of a completed I/O step."""
    yield from ()
    return value


def run(generator: Any) -> Any:
    """Drive a finite cooperative step."""
    while True:
        try:
            next(generator)
        except StopIteration as result:
            return result.value


def context() -> Any:
    """Build a shared context with plain timing state."""
    ctx = MagicMock()
    ctx.shared_state = {}
    ctx.params.is_running_on_polymarket = False
    return ctx


def test_post_time_replaces_deposit_challenge_time() -> None:
    """Measure from each actual POST, including the retry after deposit."""
    ctx = context()
    behaviour = SimpleNamespace(context=ctx, params=ctx.params)
    executor = latency.TimedOffchainRequestExecutor(behaviour)
    meta = dict(tool="forecast", nonce="uuid", request_context={"market_id": "market"})
    with patch.object(
        OffchainRequestExecutor, "_post_signed_request", return_value=done(None)
    ):
        with patch.object(latency.time, "time", return_value=100):
            run(
                executor._post_signed_request(
                    ipfs_data=json.dumps(meta),
                    mech_address="0xselected",
                    mech_url="https://selected.test",
                    ipfs_hash="hash",
                    request_id_bytes=b"id",
                    signature_hex="signature",
                    nonce=0,
                    delivery_rate=1,
                    sender="safe",
                )
            )
    with patch.object(
        OffchainRequestExecutor, "_post_signed_request", return_value=done(None)
    ):
        with patch.object(latency.time, "time", return_value=150):
            run(
                executor._post_signed_request(
                    ipfs_data=json.dumps(meta),
                    mech_address="0xselected",
                    mech_url="https://selected.test",
                    ipfs_hash="hash",
                    request_id_bytes=b"id",
                    signature_hex="signature",
                    nonce=0,
                    delivery_rate=1,
                    sender="safe",
                )
            )
    assert ctx.shared_state[latency.TIMINGS]["uuid"]["sent_ts"] == 150


@pytest.mark.parametrize("market", ["market", "-"])
@pytest.mark.parametrize("offchain", [False, True])
def test_one_line_per_answer(market: str, offchain: bool) -> None:
    """Log normal and mech-only answers once, but do not log timeouts."""
    ctx = context()
    ctx.params.use_offchain = offchain
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market=market, sent_ts=100)
    }
    b = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    responses = [dict(nonce="uuid", result="answer", mech_address="0xselected")]
    payload = JSONPayload("agent", json.dumps(responses))
    with patch.object(
        MechResponseBehaviour, "finish_behaviour", side_effect=lambda p: done(None)
    ):
        with patch.object(latency.time, "time", return_value=131):
            run(b.finish_behaviour(payload))
            run(b.finish_behaviour(payload))
    lines = [
        call.args[0] % call.args[1:]
        for call in ctx.logger.info.call_args_list
        if call.args[0].startswith("[MECH_LATENCY]")
    ]
    assert lines == [
        f"[MECH_LATENCY] tool=forecast mech=0xselected offchain={str(offchain).lower()} sent_ts=100 answered_ts=131 latency_s=31.000 market={market}"
    ]
    assert ctx.shared_state[latency.TIMINGS]["uuid"]["latency_s"] == 31
    ctx.logger.reset_mock()
    payload = JSONPayload("agent", json.dumps([dict(nonce="uuid", result=None)]))
    with patch.object(
        MechResponseBehaviour, "finish_behaviour", side_effect=lambda p: done(None)
    ):
        run(b.finish_behaviour(payload))
    ctx.logger.info.assert_not_called()


def test_settlement_starts_onchain_clock() -> None:
    """Do not count transaction preparation as Mech answer time."""
    ctx = context()
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=None)
    }
    b = latency.TraderPostTxSettlementBehaviour(name="settlement", skill_context=ctx)
    with patch.object(
        type(b), "synchronized_data", new_callable=PropertyMock
    ) as synced:
        synced.return_value.tx_submitter = "mech_request_round"
        with patch.object(latency.time, "time", return_value=200):
            b._on_tx_settled()
    assert ctx.shared_state[latency.TIMINGS]["uuid"]["sent_ts"] == 200


@pytest.mark.parametrize("offchain", [False, True])
def test_serialised_answer_keeps_request_mech(offchain: bool) -> None:
    """Keep the request Mech when the real response has no Mech address."""
    ctx = context()
    ctx.params.use_offchain = offchain
    metadata = dict(
        tool="forecast", nonce="uuid", request_context={"market_id": "market"}
    )
    if offchain:
        executor = latency.TimedOffchainRequestExecutor(
            SimpleNamespace(context=ctx, params=ctx.params)
        )
        with (
            patch.object(
                OffchainRequestExecutor, "_post_signed_request", return_value=done(None)
            ),
            patch.object(latency.time, "time", return_value=100),
        ):
            run(
                executor._post_signed_request(
                    "https://selected.test",
                    "0xselected",
                    "hash",
                    json.dumps(metadata),
                    b"id",
                    "signature",
                    0,
                    1,
                    "safe",
                )
            )
    else:
        ctx.shared_state[latency.TIMINGS] = {
            "uuid": latency.request_timing(metadata, None)
        }
        ctx.state.last_called_mech = "0xselected"
        settlement = latency.TraderPostTxSettlementBehaviour(
            name="settlement", skill_context=ctx
        )
        with (
            patch.object(
                type(settlement), "synchronized_data", new_callable=PropertyMock
            ) as synced,
            patch.object(latency.time, "time", return_value=100),
        ):
            synced.return_value.tx_submitter = "mech_request_round"
            settlement._on_tx_settled()
    response = MechInteractionResponse(nonce="uuid", result="answer")
    payload = JSONPayload("agent", serialise_responses([response]))
    behaviour = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    behaviour.current_mech_response = response
    with (
        patch.object(
            MechResponseBehaviour, "finish_behaviour", side_effect=lambda p: done(None)
        ),
        patch.object(
            MechResponseBehaviour, "_get_response", side_effect=lambda: done(True)
        ),
        patch.object(latency.time, "time", return_value=131),
    ):
        if not offchain:
            assert run(behaviour._get_response())
        run(behaviour.finish_behaviour(payload))
        run(behaviour.finish_behaviour(payload))
    lines = [
        call.args[0] % call.args[1:]
        for call in ctx.logger.info.call_args_list
        if call.args[0].startswith("[MECH_LATENCY]")
    ]
    assert lines == [
        f"[MECH_LATENCY] tool=forecast mech=0xselected offchain={str(offchain).lower()} sent_ts=100 answered_ts=131 latency_s=31.000 market=market"
    ]
    assert ctx.shared_state[latency.TIMINGS]["uuid"]["mech"] == "0xselected"


def test_composition_uses_shared_timing_behaviours() -> None:
    """The trader uses one timed behaviour per Mech round."""
    from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour

    assert latency.TraderMechRequestBehaviour in TraderConsensusBehaviour.behaviours
    assert latency.TraderMechResponseBehaviour in TraderConsensusBehaviour.behaviours
    assert (
        latency.TraderPostTxSettlementBehaviour in TraderConsensusBehaviour.behaviours
    )
    assert MechRequestBehaviour not in TraderConsensusBehaviour.behaviours
    assert MechResponseBehaviour not in TraderConsensusBehaviour.behaviours


@pytest.mark.parametrize("polymarket", [False, True])
def test_setup_keeps_request_metadata(polymarket: bool) -> None:
    """Keep metadata before request preparation removes it from the list."""
    from packages.valory.skills.mech_interact_abci.states.base import MechMetadata

    ctx = context()
    ctx.params.is_running_on_polymarket = polymarket
    b = latency.TraderMechRequestBehaviour(name="request", skill_context=ctx)
    with (
        patch.object(MechRequestBehaviour, "setup"),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as synced,
    ):
        synced.return_value.mech_requests = [MechMetadata("prompt", "forecast", "uuid")]
        b.setup()
    assert (latency.TIMINGS in ctx.shared_state) is not polymarket
    if not polymarket:
        assert ctx.shared_state[latency.TIMINGS]["uuid"] == dict(
            tool="forecast", market="-", sent_ts=None
        )


@pytest.mark.parametrize("success", [False, True])
def test_onchain_answer_is_logged_at_read_time(success: bool) -> None:
    """Log at the IPFS read, before payload consensus starts."""
    from packages.valory.skills.mech_interact_abci.states.base import (
        MechInteractionResponse,
    )

    ctx = context()
    ctx.params.use_offchain = False
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=100)
    }
    b = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    b.current_mech_response = MechInteractionResponse(
        nonce="uuid", result="answer", mech_address="0xselected"
    )
    with (
        patch.object(
            MechResponseBehaviour, "_get_response", return_value=done(success)
        ),
        patch.object(latency.time, "time", return_value=123),
    ):
        assert run(b._get_response()) is success
    assert ("answered_ts" in ctx.shared_state[latency.TIMINGS]["uuid"]) is success


@pytest.mark.parametrize(
    "submitter",
    [
        "mech_request_round",
        "mech_request_round_offchain_deposit",
        "bet_placement_round",
    ],
)
def test_other_settlements_do_not_reset_clock(submitter: str) -> None:
    """Deposits, bets, and repeated settlement calls keep the send time."""
    ctx = context()
    ctx.shared_state[latency.TIMINGS] = {"uuid": dict(sent_ts=100)}
    b = latency.TraderPostTxSettlementBehaviour(name="settlement", skill_context=ctx)
    with patch.object(
        type(b), "synchronized_data", new_callable=PropertyMock
    ) as synced:
        synced.return_value.tx_submitter = submitter
        b._on_tx_settled()
    assert ctx.shared_state[latency.TIMINGS]["uuid"]["sent_ts"] == 100


@pytest.mark.parametrize("polymarket", [False, True])
def test_polymarket_uses_existing_offchain_cycle(polymarket: bool) -> None:
    """Use timed Omen requests and keep the existing Polymarket cycle."""
    ctx = context()
    ctx.params.is_running_on_polymarket = polymarket
    request = latency.TraderMechRequestBehaviour(name="request", skill_context=ctx)
    with (
        patch.object(
            MechRequestBehaviour,
            "_run_offchain_request_cycle",
            side_effect=lambda: done(None),
        ) as base,
        patch.object(request, "_check_round_timeout_fits_poll_budget"),
        patch.object(
            latency.TimedOffchainRequestExecutor,
            "run",
            side_effect=lambda: done(None),
        ) as timed,
        patch.object(request, "_payload_from_offchain_result"),
        patch.object(request, "finish_behaviour", side_effect=lambda p: done(None)),
    ):
        run(request._run_offchain_request_cycle())
    assert base.call_count == int(polymarket)
    assert timed.call_count == int(not polymarket)
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    with patch.object(
        MechResponseBehaviour, "finish_behaviour", return_value=done(None)
    ):
        run(response.finish_behaviour(JSONPayload("agent", "[]")))
    assert latency.TIMINGS not in ctx.shared_state


@pytest.mark.parametrize(
    "dynamic,ranked,penalized,expected",
    [
        (True, [], {}, (None, None)),
        (True, ["0xselected"], {}, ("0xselected", "https://selected.test")),
        (
            True,
            ["0xselected"],
            {"0xselected": 100},
            ("0xselected", "https://selected.test"),
        ),
        (False, [], set(), ("0xpriority", "https://priority.test")),
    ],
)
def test_fast_dynamic_does_not_fall_back_to_priority(
    dynamic: bool, ranked: Any, penalized: Any, expected: Any
) -> None:
    """Never send an unsupported tool to the static priority in dynamic mode."""
    ctx = context()
    ctx.params.fast_dynamic_mech = dynamic
    ctx.params.mech_marketplace_config = SimpleNamespace(
        use_dynamic_mech_selection=dynamic,
        offchain_url="https://priority.test",
        priority_mech_address="0xpriority",
    )
    ctx.state.penalized_mechs = penalized
    b = SimpleNamespace(
        context=ctx,
        params=ctx.params,
        shared_state=ctx.state,
        synchronized_data=SimpleNamespace(
            ranked_mechs=[
                SimpleNamespace(address=a, http_url="https://selected.test")
                for a in ranked
            ]
        ),
    )
    assert latency.TimedOffchainRequestExecutor(b)._pick_next_mech([]) == expected


@pytest.mark.parametrize(
    "attempted,url", [([], None), (["0xselected"], "https://selected.test")]
)
def test_fast_dynamic_never_uses_static_url(attempted: Any, url: Any) -> None:
    """A missing manifest URL cannot redirect a request to the priority Mech."""
    ctx = context()
    ctx.params.fast_dynamic_mech = True
    ctx.params.mech_marketplace_config = SimpleNamespace(
        use_dynamic_mech_selection=True,
        offchain_url="https://priority.test",
        priority_mech_address="0xpriority",
    )
    b = SimpleNamespace(
        context=ctx,
        params=ctx.params,
        shared_state=SimpleNamespace(penalized_mechs={}),
        synchronized_data=SimpleNamespace(
            ranked_mechs=[SimpleNamespace(address="0xselected", http_url=url)]
        ),
    )
    assert latency.TimedOffchainRequestExecutor(b)._pick_next_mech(attempted) == (
        None,
        None,
    )


def rotation_executor(safe: str = "0x01") -> Any:
    """Build a fast executor with ranked endpoints and local agent state."""
    ctx = context()
    ctx.params.fast_dynamic_mech = True
    ctx.params.fast_mech_start = "hash"
    ctx.params.fast_mech_rotate_latency_s = 90
    ctx.state.penalized_mechs = {}
    mechs = [
        SimpleNamespace(address=address, http_url=f"https://{address}.test")
        for address in ("0x03", "0x01", "0x02")
    ]
    b = SimpleNamespace(
        context=ctx,
        params=ctx.params,
        shared_state=ctx.state,
        synchronized_data=SimpleNamespace(
            ranked_mechs=mechs, safe_contract_address=safe
        ),
    )
    return latency.TimedOffchainRequestExecutor(b)


@pytest.mark.parametrize("safe,expected", [("0x01", "0x02"), ("0x03", "0x01")])
def test_hash_start_is_per_safe_and_sorted(safe: str, expected: str) -> None:
    """Use the safe address modulo the address-sorted eligible endpoints."""
    executor = rotation_executor(safe)
    assert executor._pick_next_mech([])[0] == expected
    assert rotation_executor(safe)._pick_next_mech([])[0] == expected


def test_ranked_start_stays_sticky() -> None:
    """Use today's ranking for ranked start, then retain the selected Mech."""
    executor = rotation_executor()
    executor._b.params.fast_mech_start = "ranked"
    assert executor._pick_next_mech([])[0] == "0x03"
    executor._synced.ranked_mechs.reverse()
    assert executor._pick_next_mech([])[0] == "0x03"


@pytest.mark.parametrize("elapsed,threshold", [(89, 90), (90, 90), (100, 0)])
def test_keep_fast_or_disabled_rotation(elapsed: int, threshold: int) -> None:
    """Retain the current Mech at the limit and when rotation is disabled."""
    executor = rotation_executor()
    executor._b.params.fast_mech_rotate_latency_s = threshold
    executor._b.context.shared_state["fast_mech_rotation"] = dict(
        current_mech="0x02",
        last_latency_s=elapsed,
        last_answer_mech="0x02",
        request_count=3,
    )
    assert executor._pick_next_mech([])[0] == "0x02"
    lines = [
        call.args[0] % call.args[1:]
        for call in executor._b.context.logger.info.call_args_list
    ]
    assert f"[MECH_ROTATE] keep=0x02 latency_s={elapsed}" in lines


def test_slow_answer_rotates_reproducibly_to_another_eligible_mech() -> None:
    """A slow answer skips the current and penalized Mechs on the next request."""
    choices = []
    for _ in range(2):
        executor = rotation_executor()
        executor._b.shared_state.penalized_mechs = {"0x03": 100}
        state = dict(
            current_mech="0x02",
            last_latency_s=91,
            last_answer_mech="0x02",
            request_count=3,
        )
        executor._b.context.shared_state["fast_mech_rotation"] = state
        choices.append(executor._pick_next_mech([])[0])
        assert choices[-1] == "0x01"
        assert state["current_mech"] == "0x01"
        assert state["last_latency_s"] == 91
        lines = [
            call.args[0] % call.args[1:]
            for call in executor._b.context.logger.info.call_args_list
        ]
        assert (
            "[MECH_ROTATE] from=0x02 to=0x01 reason=latency_s=91 threshold=90" in lines
        )
        assert executor._pick_next_mech([])[0] == "0x01"
    assert choices[0] == choices[1]


def test_all_penalized_uses_soonest_expiry() -> None:
    """Keep making requests when all tool endpoints are penalized."""
    executor = rotation_executor()
    executor._b.shared_state.penalized_mechs = {
        "0x03": 300,
        "0x01": 100,
        "0x02": 200,
    }
    assert executor._pick_next_mech([])[0] == "0x01"
    executor._b.context.logger.warning.assert_called_once_with(
        "Fast Mech fallback=%s reason=all_penalized penalized_at=%s", "0x01", 100
    )
    assert executor._pick_next_mech(["0x01"])[0] == "0x02"


def test_answer_state_survives_behaviour_replacement() -> None:
    """Keep the last answer latency in local state across period behaviours."""
    executor = rotation_executor()
    ctx = executor._b.context
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=100, mech="0x02")
    }
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    with patch.object(latency.time, "time", return_value=191):
        response._log_answer(dict(nonce="uuid", result="answer"))
    state = ctx.shared_state["fast_mech_rotation"]
    assert state["current_mech"] == "0x02"
    assert state["last_latency_s"] == 91
    replacement = latency.TimedOffchainRequestExecutor(executor._b)
    assert replacement._pick_next_mech([])[0] != "0x02"


@pytest.mark.parametrize(
    "rotation_state",
    [
        None,
        dict(
            current_mech="0x02",
            last_answer_mech="0x02",
            last_latency_s=22,
            request_count=3,
        ),
    ],
)
def test_unknown_answer_mech_keeps_rotation_state(rotation_state: Any) -> None:
    """Log unknown Mechs without changing the rotation state."""
    executor = rotation_executor()
    ctx = executor._b.context
    ctx.params.use_offchain = True
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=100)
    }
    if rotation_state is not None:
        ctx.shared_state["fast_mech_rotation"] = rotation_state.copy()
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    with patch.object(latency.time, "time", return_value=191):
        response._log_answer(dict(nonce="uuid", result="answer"))
    assert ctx.shared_state.get("fast_mech_rotation") == rotation_state
    ctx.logger.info.assert_called_once_with(
        "[MECH_LATENCY] tool=%s mech=%s offchain=%s sent_ts=%s answered_ts=%s latency_s=%.3f market=%s",
        "forecast",
        "-",
        "true",
        100,
        191,
        91,
        "-",
    )


def test_rotation_seed_spreads_multiple_alternatives() -> None:
    """Repeat each seeded choice and spread successive requests."""
    choices = []
    for count in range(8):
        repeated = []
        for _ in range(2):
            executor = rotation_executor()
            executor._b.context.shared_state["fast_mech_rotation"] = dict(
                current_mech="0x03",
                last_answer_mech="0x03",
                last_latency_s=91,
                request_count=count,
            )
            choice = executor._pick_next_mech([])[0]
            assert choice in {"0x01", "0x02"}
            repeated.append(choice)
        assert repeated[0] == repeated[1]
        choices.append(repeated[0])
    assert set(choices) == {"0x01", "0x02"}


def test_slow_single_mech_is_kept() -> None:
    """Keep the sole endpoint when no different Mech is eligible."""
    executor = rotation_executor()
    executor._synced.ranked_mechs = executor._synced.ranked_mechs[:1]
    executor._b.context.shared_state["fast_mech_rotation"] = dict(
        current_mech="0x03", last_answer_mech="0x03", last_latency_s=91
    )
    assert executor._pick_next_mech([])[0] == "0x03"
    executor._b.context.logger.info.assert_called_once_with(
        "[MECH_ROTATE] keep=%s latency_s=%s", "0x03", 91
    )


def test_timeout_penalty_selects_another_endpoint() -> None:
    """A poll penalty removes the current endpoint from the next choice."""
    executor = rotation_executor()
    executor._b.shared_state.penalized_mechs = {"0X03": 100}
    executor._b.context.shared_state["fast_mech_rotation"] = dict(
        current_mech="0x03", last_answer_mech="0x03", last_latency_s=22
    )
    selected = executor._pick_next_mech([])[0]
    assert selected == "0x01"
    assert selected.lower() not in {
        address.lower() for address in executor._b.shared_state.penalized_mechs
    }
    executor._b.context.logger.info.assert_called_once_with(
        "[MECH_ROTATE] from=%s to=%s reason=unavailable", "0x03", "0x01"
    )


def test_current_mech_address_is_case_insensitive() -> None:
    """Retain an endpoint when an answer uses another address case."""
    executor = rotation_executor()
    executor._b.params.fast_mech_start = "ranked"
    executor._b.context.shared_state["fast_mech_rotation"] = dict(
        current_mech="0X02", last_answer_mech="0x02", last_latency_s=22
    )
    assert executor._pick_next_mech([])[0] == "0x02"
    assert executor._b.context.shared_state["fast_mech_rotation"]["request_count"] == 1


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("outcome", list(OffchainAttemptOutcome))
def test_post_penalty_only_for_fast_endpoint_failures(
    dynamic: bool, outcome: OffchainAttemptOutcome
) -> None:
    """Penalize failed fast endpoints, but retain payment and nonce behavior."""
    executor = rotation_executor()
    executor._b.params.fast_dynamic_mech = dynamic
    attempt = OffchainAttemptResult(outcome, "0x02", "https://0x02.test")
    with patch.object(
        OffchainRequestExecutor, "_post_signed_request", return_value=done(attempt)
    ):
        result = run(
            executor._post_signed_request(
                "https://0x02.test",
                "0x02",
                "hash",
                json.dumps(dict(tool="forecast", nonce="uuid")),
                b"id",
                "signature",
                0,
                1,
                "safe",
            )
        )
    assert result is attempt
    penalized = dynamic and outcome in {
        OffchainAttemptOutcome.TIMEOUT,
        OffchainAttemptOutcome.SERVER_BUSY,
        OffchainAttemptOutcome.BAD_RESPONSE,
    }
    if penalized:
        executor._b.shared_state.penalize_mech.assert_called_once_with("0x02")
        executor._logger.info.assert_called_once_with(
            "[MECH_ROTATE] penalize=%s reason=post_%s", "0x02", outcome.value
        )
    else:
        executor._b.shared_state.penalize_mech.assert_not_called()
        executor._logger.info.assert_not_called()


def test_failed_post_after_slow_answer_rotates_on_next_run() -> None:
    """A terminal plain 503 must not pin later requests to the failed Mech."""
    executor = rotation_executor()
    behaviour = executor._b
    ctx = behaviour.context
    safe = "0x" + "cc" * 20
    addresses = ["0x" + byte * 20 for byte in ("aa", "bb")]
    executor._synced.ranked_mechs = [
        SimpleNamespace(address=address, http_url=f"https://{address}.test")
        for address in addresses
    ]
    executor._synced.safe_contract_address = safe
    executor._synced.offchain_pending_request = None
    executor._synced.approved_mech_rates = {}
    behaviour.params.fast_mech_start = "ranked"
    behaviour.params.mech_chain_id = "gnosis"
    behaviour.params.delivery_rate_caps = {PaymentType.NATIVE: 1}
    behaviour.params.mech_marketplace_config = SimpleNamespace(
        mech_marketplace_address="0x" + "ff" * 20,
        offchain_failover_max_retries=0,
        offchain_nonce_retry_max=0,
        offchain_poll_timeout_seconds=300,
    )
    behaviour.shared_state.penalize_mech.side_effect = (
        lambda address: behaviour.shared_state.penalized_mechs.update({address: 191})
    )
    responses = iter(
        [
            SimpleNamespace(status_code=200, body=b""),
            SimpleNamespace(status_code=503, body=b"Service unavailable"),
            SimpleNamespace(status_code=200, body=b""),
        ]
    )
    behaviour.get_http_response = MagicMock(
        side_effect=lambda **_: done(next(responses))
    )
    with (
        patch.object(executor, "_resolve_chain_id_int", side_effect=lambda: done(100)),
        patch.object(executor, "_read_on_chain_nonce", side_effect=lambda: done(0)),
        patch.object(
            executor,
            "_resolve_payment_type",
            side_effect=lambda _: done(bytes.fromhex(PaymentType.NATIVE.value[2:])),
        ),
        patch.object(executor, "_resolve_delivery_rate", side_effect=lambda _: done(1)),
        patch.object(executor, "_sign_request_id", side_effect=lambda *_: done("sig")),
    ):
        executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "first")]
        with patch.object(latency.time, "time", return_value=100):
            assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
        answer = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
        with patch.object(latency.time, "time", return_value=191):
            answer._log_answer(dict(nonce="first", result="answer"))
        assert ctx.shared_state["fast_mech_rotation"]["last_latency_s"] == 91

        executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "second")]
        failed = run(executor.run())
        assert failed.offchain_result == Event.OFFCHAIN_ALL_FAILED.value
        assert failed.last_failure_reason == OFFCHAIN_503_ALL_MECHS
        assert ctx.shared_state["fast_mech_rotation"]["current_mech"] == addresses[1]

        executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "third")]
        assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    posted_urls = [
        call.kwargs["url"] for call in behaviour.get_http_response.call_args_list
    ]
    assert posted_urls == [
        f"https://{address}.test/send_signed_requests"
        for address in (addresses[0], addresses[1], addresses[0])
    ]


@pytest.fixture
def cycle_executor(monkeypatch: Any) -> Any:
    """Use real cycle logic with contract, signature, and HTTP I/O replaced."""
    executor = rotation_executor()
    b = executor._b
    executor._synced.ranked_mechs = [
        SimpleNamespace(address="0x" + byte * 20, http_url=f"https://{byte}.test")
        for byte in ("aa", "bb")
    ]
    executor._synced.safe_contract_address = "0x" + "cc" * 20
    executor._synced.offchain_pending_request = None
    executor._synced.approved_mech_rates = {}
    executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "first")]
    b.params.fast_mech_start = "ranked"
    b.params.mech_chain_id = "gnosis"
    b.params.delivery_rate_caps = {PaymentType.NATIVE: 1}
    b.params.mech_marketplace_config = SimpleNamespace(
        mech_marketplace_address="0x" + "ff" * 20,
        offchain_failover_max_retries=0,
        offchain_nonce_retry_max=0,
        offchain_poll_timeout_seconds=300,
        auto_deposit_cap_per_cycle=1,
    )
    reads = {
        "get_chain_id": 100,
        "get_nonce": 0,
        "get_payment_type": PaymentType.NATIVE.value,
        "get_max_delivery_rate": 1,
        "get_balance_tracker_for_mech_type": "0x" + "dd" * 20,
        "get_requester_balance": 1,
    }
    monkeypatch.setattr(
        executor,
        "_read_contract_state",
        lambda **kw: done(reads[kw["contract_callable"]]),
    )
    monkeypatch.setattr(executor, "_sign_request_id", lambda *_: done("signature"))
    b.get_http_response = MagicMock(
        side_effect=lambda **_: done(SimpleNamespace(status_code=200, body=b""))
    )
    return executor, reads


def slow_answer_then_rotate(executor: Any) -> None:
    """Accept on A and record its slow answer before the next cycle."""
    with patch.object(latency.time, "time", return_value=100):
        assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    answer = latency.TraderMechResponseBehaviour(
        name="response", skill_context=executor._b.context
    )
    with patch.object(latency.time, "time", return_value=191):
        answer._log_answer(dict(nonce="first", result="answer"))
    executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "second")]


@pytest.mark.parametrize("failure", ["already_funded", "wrong_pay_to"])
def test_valid_402_cycle_soft_releases_mech(cycle_executor: Any, failure: str) -> None:
    """A refused structured 402 rotates once without a penalty."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    challenge = dict(
        payTo=(
            "0x" + "ee" * 20
            if failure == "wrong_pay_to"
            else reads["get_balance_tracker_for_mech_type"]
        ),
        asset="0x" + "00" * 20,
        chainId=100,
        currentBalance=0,
        required=1,
        error="insufficient balance",
    )
    executor._b.get_http_response.side_effect = lambda **_: done(
        SimpleNamespace(status_code=402, body=json.dumps(challenge).encode())
    )
    failed = run(executor.run())
    assert failed.offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    assert failed.last_failure_reason == OFFCHAIN_BAD_RESPONSE
    state = executor._b.context.shared_state["fast_mech_rotation"]
    a, b = executor._synced.ranked_mechs
    assert state["current_mech"] == b.address
    executor._b.shared_state.penalize_mech.assert_not_called()
    assert executor._b.shared_state.penalized_mechs == {}
    assert state.get("failed_mech") == b.address

    executor._synced.mech_requests = [MechMetadata("prompt", "forecast", "third")]
    executor._b.get_http_response.side_effect = lambda **_: done(
        SimpleNamespace(status_code=200, body=b"")
    )
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    assert executor._b.get_http_response.call_args.kwargs["url"] == (
        a.http_url + "/send_signed_requests"
    )
    assert "failed_mech" not in state
    executor._logger.info.assert_any_call(
        "[MECH_ROTATE] from=%s to=%s reason=request_failed", b.address, a.address
    )


def test_cycle_failure_before_post_soft_releases_mech(cycle_executor: Any) -> None:
    """Release the last pick even when the cycle fails before its POST."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    reads["get_max_delivery_rate"] = None
    result = run(executor.run())
    assert result.offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    state = executor._b.context.shared_state["fast_mech_rotation"]
    a, b = executor._synced.ranked_mechs
    assert state.get("failed_mech") == b.address
    assert latency.TimedOffchainRequestExecutor(executor._b)._pick_next_mech([])[0] == (
        a.address
    )
    assert "failed_mech" not in state


@pytest.mark.parametrize("dynamic", [False, True])
def test_success_cycle_does_not_release_mech(
    cycle_executor: Any, dynamic: bool
) -> None:
    """Keep accepted requests and avoid rotation state outside fast mode."""
    executor, _ = cycle_executor
    executor._b.params.fast_dynamic_mech = dynamic
    executor._config.use_dynamic_mech_selection = True
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    assert "failed_mech" not in executor._b.context.shared_state.get(
        "fast_mech_rotation", {}
    )


def test_non_fast_failure_does_not_release_mech(cycle_executor: Any) -> None:
    """A failed standard cycle must not create fast rotation state."""
    executor, reads = cycle_executor
    executor._b.params.fast_dynamic_mech = False
    executor._config.use_dynamic_mech_selection = True
    reads["get_max_delivery_rate"] = None
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    assert "fast_mech_rotation" not in executor._b.context.shared_state


def test_failure_before_selection_does_not_release_previous_mech(
    cycle_executor: Any,
) -> None:
    """Reset the cycle target before a failure that occurs before selection."""
    executor, reads = cycle_executor
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    reads["get_chain_id"] = None
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    assert "failed_mech" not in executor._b.context.shared_state["fast_mech_rotation"]


def test_failed_pending_retry_releases_pending_mech(cycle_executor: Any) -> None:
    """Release the pending target when its retry fails before the POST."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    accepted = run(executor.run())
    assert accepted.offchain_result == Event.OFFCHAIN_DONE.value
    executor._synced.offchain_pending_request = json.loads(
        accepted.pending_request_json
    )
    reads["get_chain_id"] = None
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    state = executor._b.context.shared_state["fast_mech_rotation"]
    a, b = executor._synced.ranked_mechs
    assert state.get("failed_mech") == b.address
    assert executor._pick_next_mech([])[0] == a.address


def test_exhausted_failovers_release_last_actual_pick(cycle_executor: Any) -> None:
    """Keep the last target when a later selection has no candidates."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    executor._config.offchain_failover_max_retries = 2
    reads["get_payment_type"] = None
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    state = executor._b.context.shared_state["fast_mech_rotation"]
    a, b = executor._synced.ranked_mechs
    assert state.get("failed_mech") == a.address
    assert executor._pick_next_mech([])[0] == b.address


def test_failed_single_mech_is_kept_and_marker_cleared() -> None:
    """Consume the failure marker even when no other Mech is eligible."""
    executor = rotation_executor()
    executor._synced.ranked_mechs = executor._synced.ranked_mechs[:1]
    state = dict(current_mech="0x03", failed_mech="0X03")
    executor._b.context.shared_state["fast_mech_rotation"] = state
    assert executor._pick_next_mech([])[0] == "0x03"
    assert "failed_mech" not in state


@pytest.mark.parametrize("dynamic", [False, True])
def test_missing_result_releases_fast_dynamic_mech(dynamic: bool) -> None:
    """Mark the fast dynamic Mech as failed when the terminal poll has no result."""
    executor = rotation_executor()
    ctx = executor._b.context
    ctx.params.fast_dynamic_mech = dynamic
    ctx.params.use_offchain = True
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=100, mech="0x02")
    }
    ctx.shared_state["fast_mech_rotation"] = dict(
        current_mech="0x02",
        last_answer_mech="0x03",
        last_latency_s=22,
        request_count=5,
    )
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    response._log_answer(dict(nonce="uuid", result=None, mech_address="0x02"))
    state = ctx.shared_state["fast_mech_rotation"]
    if dynamic:
        assert state["failed_mech"] == "0x02"
        assert executor._pick_next_mech([])[0] != "0x02"
    else:
        assert "failed_mech" not in state


def test_missing_result_without_known_mech_keeps_state() -> None:
    """Skip the release when the answer carries no Mech identity."""
    executor = rotation_executor()
    ctx = executor._b.context
    ctx.shared_state[latency.TIMINGS] = {
        "uuid": dict(tool="forecast", market="-", sent_ts=100)
    }
    ctx.shared_state["fast_mech_rotation"] = dict(
        current_mech="0x02", last_answer_mech="0x03", last_latency_s=22
    )
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    response._log_answer(dict(nonce="uuid", result=None))
    assert "failed_mech" not in ctx.shared_state["fast_mech_rotation"]


def test_successful_pending_retry_clears_failure_marker(cycle_executor: Any) -> None:
    """A succeeding pending retry on the previously failed Mech clears the marker."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    accepted = run(executor.run())
    assert accepted.offchain_result == Event.OFFCHAIN_DONE.value
    pending_json = accepted.pending_request_json
    executor._synced.offchain_pending_request = json.loads(pending_json)
    reads["get_chain_id"] = None
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_ALL_FAILED.value
    state = executor._b.context.shared_state["fast_mech_rotation"]
    _, b = executor._synced.ranked_mechs
    assert state.get("failed_mech") == b.address
    reads["get_chain_id"] = 100
    executor._synced.offchain_pending_request = json.loads(pending_json)
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    assert "failed_mech" not in state


def test_deposit_in_progress_keeps_mech(cycle_executor: Any, monkeypatch: Any) -> None:
    """Keep the pending target through deposit settlement and retry."""
    executor, reads = cycle_executor
    slow_answer_then_rotate(executor)
    reads["get_requester_balance"] = 0
    executor._b.get_http_response.side_effect = lambda **_: done(
        SimpleNamespace(
            status_code=402,
            body=json.dumps(
                dict(
                    payTo=reads["get_balance_tracker_for_mech_type"],
                    currentBalance=0,
                    required=1,
                )
            ).encode(),
        )
    )
    monkeypatch.setattr(
        executor,
        "_build_deposit_tx",
        lambda **_: done(SimpleNamespace(tx_hex="deposit")),
    )
    result = run(executor.run())
    assert result.offchain_result == Event.OFFCHAIN_DEPOSIT_NEEDED.value
    assert result.tx_hash == "deposit"
    state = executor._b.context.shared_state["fast_mech_rotation"]
    assert "failed_mech" not in state
    pending = json.loads(result.pending_request_json)
    assert pending["mech_address"] == state["current_mech"]
    executor._synced.offchain_pending_request = pending
    executor._b.get_http_response.side_effect = lambda **_: done(
        SimpleNamespace(status_code=200, body=b"")
    )
    assert run(executor.run()).offchain_result == Event.OFFCHAIN_DONE.value
    assert executor._b.get_http_response.call_args.kwargs["url"] == (
        pending["mech_url"] + "/send_signed_requests"
    )
    assert "failed_mech" not in state
