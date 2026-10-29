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

"""Bound preview foreground work by its original paid-request timestamp."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastDecisionBehaviour,
    FastMechRequestBehaviour,
    FastMechResponseBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.preview import PreviewMixin, PreviewStore
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    evaluate_preview,
    make,
    run,
)
from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainCycleResult,
    OffchainRequestExecutor,
)
from packages.valory.skills.mech_interact_abci.states.base import (
    MechInteractionResponse,
)


def run_deadline_case(
    tmp_path: Path, request_round: bool, preview: bool, expected_at: float
) -> None:
    """Stop preview polls and hung requests at 300s; normal polls keep their budget."""
    cls = FastMechRequestBehaviour if request_round else FastMechResponseBehaviour
    behaviour = make(cls, tmp_path)
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.params.use_offchain = True
    behaviour.context.params.mech_marketplace_config.offchain_poll_timeout_seconds = 900
    behaviour.context.params.mech_marketplace_config.offchain_poll_interval_seconds = 1
    behaviour.context.params.mech_chain_id = "gnosis"
    behaviour.context.state.round_sequence.syncing_up = False
    behaviour.context.shared_state = {}
    behaviour.setup = lambda: None
    behaviour._check_round_timeout_fits_poll_budget = lambda: None
    sent = []
    callbacks = []
    received = []

    def request(executor: Any) -> Any:
        if not preview:
            return OffchainCycleResult(offchain_result="offchain_done")
        callbacks.append(behaviour.get_callback_request())
        yield from behaviour.wait_for_message()

    def finish(payload: Any) -> Any:
        sent.append(payload)
        if request_round and preview:
            callbacks.append(behaviour.get_callback_request())
            received.append((yield from behaviour.wait_for_message()))

    behaviour.finish_behaviour = finish
    behaviour.get_http_response = lambda **kwargs: done(
        SimpleNamespace(status_code=200, body=b'{"status":"processing"}')
    )

    def sleep(_seconds: float) -> Any:
        yield

    behaviour.sleep = sleep
    values: dict[str, Any] = {
        "lightning_preview_request": preview,
        "requested_at": 1000,
        "lightning_preview_title": "Will event X happen?",
    }
    pending: dict[str, Any] = {
        "request_id": "ab" * 32,
        "nonce": 7,
        "mech_address": "0x" + "aa" * 20,
        "mech_url": "https://m.test",
        "sender": "0x" + "bb" * 20,
        "delivery_rate": 1,
        "ipfs_hash": "0x" + "cc" * 31,
        "ipfs_data": "{}",
        "metadata_nonce": "meta-nonce",
    }
    synced = SimpleNamespace(
        db=SimpleNamespace(
            get=lambda key, default=None: values.get(key, default),
            get_strict=lambda key: values[key],
        ),
        safe_contract_address=pending["sender"],
        offchain_pending_request=pending,
        mech_responses=[
            MechInteractionResponse(data=pending["request_id"], nonce="meta-nonce")
        ],
    )
    clock = [1050.0]
    with (
        patch.object(
            cls, "synchronized_data", new_callable=PropertyMock, return_value=synced
        ),
        patch("time.time", side_effect=lambda: clock[0]),
        patch("time.monotonic", side_effect=lambda: clock[0]),
        patch.object(OffchainRequestExecutor, "run", request),
    ):
        while not sent and clock[0] <= expected_at + 1:
            behaviour.act()
            if not sent:
                clock[0] += 1
    assert len(sent) == 1
    assert clock[0] == expected_at
    if request_round and not preview:
        assert sent[0].offchain_result == "offchain_done"
    elif request_round:
        assert sent[0].offchain_result == "offchain_all_failed"
        assert sent[0].offchain_last_failure_reason == "preview_deadline"
        callbacks[0]("late request reply", behaviour)
        behaviour.act()
        assert received == []
        callbacks[1]("failure consensus reply", behaviour)
        behaviour.act()
        assert received == ["failure consensus reply"]
    else:
        assert json.loads(sent[0].information)[-1]["error"] == (
            "preview_deadline" if preview else "poll_timeout"
        )


def test_late_preview_answer_is_failed(tmp_path: Path) -> None:
    """A response beyond the preview deadline cannot enter the answer cache."""
    behaviour = make(FastDecisionBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.params.mech_marketplace_config.offchain_poll_timeout_seconds = 900
    behaviour._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.99)
    title = "Will event X happen?"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": title}]}}, 990, 990)
    store.reserve(title, 1000, "night", 15)
    result = evaluate_preview(
        behaviour,
        {
            "requested_at": 1000,
            "request_prices": [0.5, 0.5],
            "lightning_preview_request": True,
            "lightning_preview_title": title,
        },
        1300,
    )
    assert result["reason"] == "preview_answer_failed"
    latest = PreviewStore(store.path).entries[title]
    assert latest["failed_at"] == 1300
    assert latest["p_yes"] is None


@pytest.mark.parametrize("request_round", [False, True])
def test_preview_deadline_stops_real_scheduler_work(
    tmp_path: Path, request_round: bool
) -> None:
    """Bound preview work and retain the normal market's configured poll budget."""
    run_deadline_case(tmp_path, request_round, True, 1300)
    if request_round:
        run_deadline_case(tmp_path, True, False, 1050)
    else:
        run_deadline_case(tmp_path, False, False, 1950)


def test_preview_success_can_finish_consensus_after_deadline(tmp_path: Path) -> None:
    """Do not replace an on-time response payload while its consensus is pending."""
    from packages.valory.skills.fresh_watch_abci.behaviours import (
        FastMechResponseBehaviour,
    )
    from packages.valory.skills.mech_interact_abci.behaviours.base import (
        MechInteractBaseBehaviour,
    )

    behaviour = make(FastMechResponseBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.params.use_offchain = True
    behaviour.context.params.mech_marketplace_config.offchain_poll_timeout_seconds = 900
    behaviour.context.state.round_sequence.syncing_up = False
    behaviour.setup = lambda: None
    behaviour._check_round_timeout_fits_poll_budget = lambda: None
    values: dict[str, Any] = {
        "lightning_preview_request": True,
        "requested_at": 1000,
        "lightning_preview_title": "Will event X happen?",
    }
    synced = SimpleNamespace(
        db=SimpleNamespace(
            get=lambda key, default=None: values.get(key, default),
            get_strict=lambda key: values[key],
        ),
        offchain_pending_request=None,
        mech_responses=[
            MechInteractionResponse(
                nonce="meta", result="answer", mech_address="0xmech"
            )
        ],
    )
    behaviour.context.shared_state = {
        "mech_latency": {
            "meta": {"tool": "superforcaster", "market": "market", "sent_ts": 1200}
        }
    }
    behaviour.context.params.fast_dynamic_mech = True
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest(
        {"result": {"questions": [{"question": values["lightning_preview_title"]}]}},
        990,
        990,
    )
    store.reserve(values["lightning_preview_title"], 1000, "night", 15)
    sent = []

    def finish(_behaviour: Any, payload: Any) -> Any:
        sent.append(payload)
        while True:
            yield

    clock = [1299]
    with (
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=synced,
        ),
        patch("time.time", side_effect=lambda: clock[0]),
        patch.object(MechInteractBaseBehaviour, "finish_behaviour", finish),
    ):
        behaviour.act()
        assert len(sent) == 1
        assert behaviour.context.shared_state["mech_latency"]["meta"]["latency_s"] == 99
        assert (
            behaviour.context.shared_state["fast_mech_rotation"]["last_answer_mech"]
            == "0xmech"
        )
        clock[0] = 1301
        behaviour.act()
    assert len(sent) == 1
    assert (
        "failed_at"
        not in PreviewStore(store.path).entries[values["lightning_preview_title"]]
    )


def test_preview_requires_offchain_and_logs_once(tmp_path: Path) -> None:
    """Disable preview discovery, reservations and deadlines on the on-chain route."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour

    watch = make(FreshWatchBehaviour, tmp_path)
    watch.context.shared_state = {}
    watch.context.params.lightning = LightningConfig(lightning_preview=True)
    watch.context.params.use_offchain = False
    watch.context.state.fast_chain_fallback = False
    watch._fast_tool = lambda: "superforcaster"
    watch._fetch_fresh = lambda *_args: done([])
    watch.preview_poll = MagicMock(
        side_effect=AssertionError("on-chain mode must not poll preview events")
    )
    title = "Will event X happen?"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": title}]}}, 990, 990)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    assert watch._preview_selection(1050) is None
    assert watch._preview_selection(1051) is None
    assert run(watch._poll_markets(queue, 1050)) == []
    run(PreviewMixin.preview_poll(watch, 1050))
    assert watch._discovery_period() == watch.params.fast.poll_s
    assert PreviewStore(store.path).counts == {}
    disabled_logs = [
        call
        for call in watch.context.logger.info.call_args_list
        if "requires USE_OFFCHAIN=true" in str(call)
    ]
    assert len(disabled_logs) == 1

    response = make(FastMechResponseBehaviour, tmp_path)
    response.context.params.lightning = watch.params.lightning
    response.context.params.use_offchain = False
    response.context.shared_state = watch.context.shared_state
    response.context.state.round_sequence.syncing_up = False
    response.setup = lambda: None
    ordinary_work = []

    def ordinary_response() -> Any:
        ordinary_work.append("on-chain")
        yield from ()

    response.async_act = ordinary_response
    values = {
        "lightning_preview_request": True,
        "requested_at": 1000,
        "lightning_preview_title": title,
    }
    synced = SimpleNamespace(
        db=SimpleNamespace(
            get=lambda key, default=None: values.get(key, default),
            get_strict=lambda key: values[key],
        )
    )
    with (
        patch.object(
            type(response),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=synced,
        ),
        patch("time.time", return_value=1300),
    ):
        response.act()
    assert ordinary_work == ["on-chain"]
    assert "failed_at" not in PreviewStore(store.path).entries[title]
    watch.context.params.use_offchain = True
    assert watch._discovery_period() == 5.0
    selection = watch._preview_selection(1050)
    assert selection["lane"] == "preview_request"
    assert list(PreviewStore(store.path).counts.values()) == [1]


def test_preview_deadline_reuses_unanswered_slot(tmp_path: Path) -> None:
    """Keep an accepted slot reserved and let the next request use it once."""
    from packages.valory.skills.mech_interact_abci.nonce_allocator import (
        MECH_SLOT_ABANDONED,
        MECH_SLOT_REGISTRY,
        note_slot_accepted,
        reserve_slot,
    )
    from packages.valory.skills.mech_interact_abci.tests.behaviours import (
        test_offchain_request_helpers as helpers,
    )

    stub = helpers._StubBehaviour(
        ranked_mechs=[helpers._FakeMechInfo("0x" + "aa" * 20, "https://m.test")],
        contract_api_responses=[
            helpers._state_resp({"data": 100}),
            helpers._state_resp({"data": 7}),
            helpers._state_resp({"payment_type": helpers._NATIVE_PAYMENT_TYPE}),
            helpers._state_resp({"max_delivery_rate": 10**16}),
        ],
        http_responses=[helpers._make_http_response(200)],
    )
    chain = stub.params.mech_chain_id
    safe = stub.synchronized_data.safe_contract_address
    shared = stub.context.shared_state
    reserve_slot(shared, chain=chain, safe=safe, on_chain_nonce=7)
    note_slot_accepted(shared, chain=chain, safe=safe, slot=7)
    behaviour = make(FastMechResponseBehaviour, tmp_path)
    behaviour.context.shared_state = shared
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.params.use_offchain = True
    behaviour.context.params.mech_chain_id = chain
    behaviour.context.params.mech_marketplace_config.offchain_poll_timeout_seconds = 900
    behaviour.context.params.mech_marketplace_config.offchain_poll_interval_seconds = 1
    behaviour.context.state.round_sequence.syncing_up = False
    behaviour.setup = lambda: None
    behaviour._check_round_timeout_fits_poll_budget = lambda: None
    behaviour.get_http_response = lambda **_kwargs: done(
        SimpleNamespace(status_code=200, body=b'{"status":"processing"}')
    )

    def sleep(_seconds: float) -> Any:
        yield

    behaviour.sleep = sleep
    sent = []

    def finish(payload: Any) -> Any:
        sent.append(payload)
        yield from ()

    behaviour.finish_behaviour = finish
    values = {
        "lightning_preview_request": True,
        "requested_at": 1000,
        "lightning_preview_title": "Will event X happen?",
    }
    pending = {
        "request_id": "ab" * 32,
        "nonce": 7,
        "mech_address": "0x" + "aa" * 20,
        "mech_url": "https://m.test",
        "sender": safe,
        "delivery_rate": 1,
        "ipfs_hash": "0x" + "cc" * 31,
        "ipfs_data": "{}",
        "metadata_nonce": "meta-nonce",
    }
    synced = SimpleNamespace(
        db=SimpleNamespace(
            get=lambda key, default=None: values.get(key, default),
            get_strict=lambda key: values[key],
        ),
        offchain_pending_request=pending,
        mech_responses=[MechInteractionResponse(data=pending["request_id"])],
    )
    clock = [1299]
    with (
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=synced,
        ),
        patch("time.time", side_effect=lambda: clock[0]),
        patch("time.monotonic", side_effect=lambda: clock[0]),
    ):
        behaviour.act()
        assert sent == []
        clock[0] = 1300
        behaviour.act()
    assert len(sent) == 1
    assert json.loads(sent[0].information)[-1]["error"] == "preview_deadline"
    assert shared.get(MECH_SLOT_ABANDONED) == {(chain, safe, 7)}
    assert shared[MECH_SLOT_REGISTRY].live[(chain, safe)] == {7}
    behaviour.context.state.penalize_mech.assert_called_once_with(
        pending["mech_address"]
    )
    result = run(OffchainRequestExecutor(stub)._fresh_cycle())
    assert result.offchain_result == "offchain_done"
    assert helpers._form_field(stub.posted_bodies[0], "nonce") == "7"
    assert shared[MECH_SLOT_REGISTRY].live[(chain, safe)] == {7}
    assert shared[MECH_SLOT_ABANDONED] == set()
