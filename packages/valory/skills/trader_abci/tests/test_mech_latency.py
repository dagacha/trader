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
from packages.valory.skills.mech_interact_abci.behaviours.request import (
    MechRequestBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.response import (
    MechResponseBehaviour,
)
from packages.valory.skills.mech_interact_abci.payloads import JSONPayload
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


def test_polymarket_uses_existing_offchain_cycle() -> None:
    """Keep the Polymarket request and response behaviour unchanged."""
    ctx = context()
    ctx.params.is_running_on_polymarket = True
    request = latency.TraderMechRequestBehaviour(name="request", skill_context=ctx)
    with patch.object(
        MechRequestBehaviour, "_run_offchain_request_cycle", return_value=done(None)
    ) as base:
        run(request._run_offchain_request_cycle())
    base.assert_called_once()
    response = latency.TraderMechResponseBehaviour(name="response", skill_context=ctx)
    with patch.object(
        MechResponseBehaviour, "finish_behaviour", return_value=done(None)
    ):
        run(response.finish_behaviour(JSONPayload("agent", "[]")))
    assert latency.TIMINGS not in ctx.shared_state


@pytest.mark.parametrize(
    "dynamic,ranked,penalized,expected",
    [
        (True, [], set(), (None, None)),
        (True, ["0xselected"], set(), ("0xselected", "https://selected.test")),
        (True, ["0xselected"], {"0xselected"}, (None, None)),
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
        shared_state=SimpleNamespace(penalized_mechs=set()),
        synchronized_data=SimpleNamespace(
            ranked_mechs=[SimpleNamespace(address="0xselected", http_url=url)]
        ),
    )
    assert latency.TimedOffchainRequestExecutor(b)._pick_next_mech(attempted) == (
        None,
        None,
    )
