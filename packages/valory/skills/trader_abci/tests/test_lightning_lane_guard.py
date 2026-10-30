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

"""Keep FSM Safe transactions behind an in-flight lightning shot."""

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.abstract_round_abci.base import AbstractRound
from packages.valory.skills.abstract_round_abci.behaviour_utils import BaseBehaviour
from packages.valory.skills.staking_abci.lane_guard import LaneGuardMixin


class GuardedBehaviour(LaneGuardMixin, BaseBehaviour):
    """Supply the real framework methods below the guard mixin."""

    matching_round = AbstractRound

    def async_act(self) -> Any:
        """Run a finite test action."""
        yield from ()


def make_guard(enabled: bool = True, sent_at: float = 100.0) -> Any:
    """Build the shared guard without a live agent."""
    shared = {"lightning_lane_inflight": {"tx": "0xshot", "sent_at": sent_at}}
    context = SimpleNamespace(
        shared_state=shared, agent_address="0xowner", logger=MagicMock()
    )
    params = SimpleNamespace(
        lightning=SimpleNamespace(
            lightning_lane=enabled,
            lightning_rpc_url="https://rpc.invalid/secret",
            lightning_lane_inflight_timeout_s=60,
        )
    )
    context.params = params
    guard = object.__new__(GuardedBehaviour)
    guard._context = context
    return guard


@pytest.mark.parametrize("enabled", [False, None])
def test_guard_off_has_no_io(enabled: Any) -> None:
    """Keep existing agents unchanged when the lane is disabled or absent."""
    guard = make_guard(enabled)
    if enabled is None:
        guard.context.params = SimpleNamespace()
    guard.sleep = MagicMock(side_effect=AssertionError("unexpected sleep"))
    assert list(LaneGuardMixin.wait_for_lightning_lane(guard)) == []
    assert "lightning_lane_inflight" in guard.context.shared_state


def test_guard_resumes_after_receipt_clears_inflight() -> None:
    """Poll at half-second intervals until the receipt watcher releases the Safe."""
    guard = make_guard()
    sleeps = []

    def sleep(seconds: float) -> Any:
        sleeps.append(seconds)
        yield
        guard.context.shared_state.pop("lightning_lane_inflight")

    guard.sleep = sleep
    with patch("time.time", return_value=101):
        assert list(LaneGuardMixin.wait_for_lightning_lane(guard)) == [None]
    assert sleeps == [0.5]


def test_guard_timeout_refreshes_pending_nonce_before_clear() -> None:
    """Refresh the pending EOA nonce through the private RPC before release."""
    guard = make_guard()
    calls = []

    def build(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return "message", "dialogue"

    def request(*args: Any, **kwargs: Any) -> Any:
        assert "lightning_lane_inflight" in guard.context.shared_state
        yield
        return SimpleNamespace(status_code=200, body=b'{"result":"0x9"}')

    guard._build_http_request_message = build
    guard._do_request = request
    with patch("time.time", return_value=161):
        assert list(LaneGuardMixin.wait_for_lightning_lane(guard)) == [None]
    assert "lightning_lane_inflight" not in guard.context.shared_state
    assert guard.context.shared_state["lightning_lane_eoa_nonce"] == {
        "nonce": 9,
        "read_at": 161,
    }
    assert calls[0]["url"] == guard.params.lightning.lightning_rpc_url
    assert json.loads(calls[0]["content"]) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionCount",
        "params": ["0xowner", "pending"],
    }
    assert not guard.context.logger.warning.called


@pytest.mark.parametrize(
    "body,status",
    [
        (b'{"error":{"message":"private URL must not be logged"}}', 200),
        (b"not JSON", 200),
        (b'{"result":"0x9"}', 503),
    ],
)
def test_guard_failed_nonce_refresh_keeps_safe_blocked(
    body: bytes, status: int
) -> None:
    """Do not release a Safe transaction after a failed timeout nonce read."""
    guard = make_guard()
    guard._build_http_request_message = lambda **kwargs: ("message", "dialogue")

    def request(*args: Any, **kwargs: Any) -> Any:
        yield from ()
        return SimpleNamespace(status_code=status, body=body)

    guard._do_request = request
    guard.sleep = lambda seconds: iter([None])
    with patch("time.time", return_value=161):
        generator = LaneGuardMixin.wait_for_lightning_lane(guard)
        assert next(generator) is None
        assert "lightning_lane_inflight" in guard.context.shared_state
        generator.close()
    assert "private" not in str(guard.context.logger.warning.call_args)


def test_guard_reserves_safe_for_preparation_in_whitelisted_round() -> None:
    """Block new lane shots once an off-chain request starts a Safe deposit."""
    guard = make_guard()
    guard.context.shared_state.clear()
    guard.matching_round = SimpleNamespace(auto_round_id=lambda: "background_round")
    assert list(LaneGuardMixin.wait_for_lightning_lane(guard, reserve=True)) == []
    assert guard.context.shared_state["lightning_lane_fsm_preparing"] is True

    assert (
        guard.context.shared_state["lightning_lane_fsm_preparing_round"]
        == "background_round"
    )


def test_scheduler_guard_off_does_not_inspect_matching_round() -> None:
    """Keep custom and management behaviours unchanged when the lane is off."""
    guard = make_guard(False)
    guard.matching_round = object()
    guard._is_started = True
    guard._is_done = False
    guard.context.state = SimpleNamespace(
        round_sequence=SimpleNamespace(syncing_up=False)
    )
    sentinel = object()
    guard.async_act = lambda: iter([sentinel])
    assert list(LaneGuardMixin.async_act_wrapper(guard)) == [sentinel]


def test_failed_safe_hash_read_releases_preparation_reservation() -> None:
    """Keep a failed preparation from blocking shots until another settlement."""
    from packages.valory.protocols.contract_api import ContractApiMessage

    guard = make_guard()
    guard.context.shared_state.clear()
    guard.wait_for_lightning_lane = (
        lambda reserve=False: LaneGuardMixin.wait_for_lightning_lane(guard, reserve)
    )
    dialogue = SimpleNamespace()
    guard.context.contract_api_dialogues = SimpleNamespace(
        create=lambda **kwargs: ("message", dialogue)
    )
    guard.context.default_ledger_id = "ethereum"
    guard.context.requests = SimpleNamespace(request_id_to_callback={})
    guard.context.outbox = SimpleNamespace(put_message=lambda **kwargs: None)
    guard._get_default_terms = lambda: None
    guard._get_request_nonce_from_dialogue = lambda value: "nonce"
    guard.get_callback_request = lambda: None
    response = SimpleNamespace(performative=ContractApiMessage.Performative.ERROR)

    def wait() -> Any:
        yield from ()
        assert guard.context.shared_state["lightning_lane_fsm_preparing"] is True
        return response

    guard.wait_for_message = wait
    assert (
        list(
            LaneGuardMixin.get_contract_api_response(
                guard,
                cast(
                    ContractApiMessage.Performative,
                    ContractApiMessage.Performative.GET_STATE,
                ),
                "safe",
                "valory/gnosis_safe:0.1.0",
                "get_raw_safe_transaction_hash",
            )
        )
        == []
    )
    assert "lightning_lane_fsm_preparing" not in guard.context.shared_state
