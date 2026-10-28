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
from packages.valory.skills.mech_interact_abci.states.base import (
    MechInteractionResponse,
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
    behaviour = SimpleNamespace(context=ctx)
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
        executor = latency.TimedOffchainRequestExecutor(SimpleNamespace(context=ctx))
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
