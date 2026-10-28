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

"""Test the selected tool and the paid request across retry and restart."""

import json
from string import Template
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from packages.valory.skills.abstract_round_abci.base import AbciAppDB
from packages.valory.skills.decision_maker_abci.behaviours.decision_request import (
    DecisionRequestBehaviour,
)
from packages.valory.skills.decision_maker_abci.states.decision_request import (
    DecisionRequestRound,
)
from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour
from packages.valory.skills.fresh_watch_abci.rounds import (
    FreshWatchRound,
    SelectionPayload,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.mech_interact_abci.behaviours.offchain_request import (
    OffchainRequestExecutor,
)
from packages.valory.skills.mech_interact_abci.behaviours.response import (
    MechResponseBehaviour,
)
from packages.valory.skills.mech_interact_abci.payloads import JSONPayload
from packages.valory.skills.mech_interact_abci.payment_types import PaymentType
from packages.valory.skills.mech_interact_abci.states.base import (
    Event,
    SynchronizedData,
)
from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound
from packages.valory.skills.omenstrat_fast_abci.composition import OmenstratFastAbciApp
from packages.valory.skills.trader_abci.mech_latency import (
    TraderMechRequestBehaviour as MechRequestBehaviour,
)
from packages.valory.skills.trader_abci.mech_latency import (
    TraderMechResponseBehaviour,
)

MECH = "0x552cEA7Bc33CbBEb9f1D90c1D11D2C6daefFd053"
SAFE = "0x" + "12" * 20


def agree(context: Any, cls: Any, payload: Any) -> Any:
    """Commit a sovereign payload through its real consensus round."""
    round_ = cls(context.state.synchronized_data, context)
    round_.process_payload(payload)
    outcome = round_.end_block()
    assert outcome is not None
    data, event = outcome
    context.state.synchronized_data = SynchronizedData(data.db)
    return event


def select_request(tmp_path: Any, offered: Any) -> Any:
    """Select a market and build its request through the consensus boundary."""
    watch = make(FreshWatchBehaviour, tmp_path)
    context = watch.context
    context.agent_address = "agent"
    context.benchmarking_mode.enabled = False
    context.params.slot_count = 2
    context.params.prompt_template = Template("$question ($yes / $no)")
    context.state.synchronized_data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                safe_contract_address=[SAFE],
                mechs_info=[
                    json.dumps(
                        [
                            dict(
                                id="975",
                                address=MECH,
                                karma=0,
                                service=dict(metadata=[], deliveries=[]),
                                relevant_tools=offered,
                            )
                        ]
                    )
                ],
            )
        )
    )
    watch._fetch_fresh = lambda *args: done([raw_market()])
    watch._get_pool_balances = lambda *args: done([100, 100])
    watch.finish_behaviour = lambda payload: done(
        agree(context, FreshWatchRound, payload)
    )
    run(watch.async_act())
    request: Any = DecisionRequestBehaviour(name="request", skill_context=context)
    request.setup()
    payloads = []

    def finish(payload: Any) -> Any:
        payloads.append(payload)
        return done(agree(context, DecisionRequestRound, payload))

    request.finish_behaviour = finish
    run(request.async_act())
    return watch, payloads[0]


@pytest.mark.parametrize(
    "offered,expected",
    [
        (list(FastConfig().tools), "superforcaster_calibrated_full_search"),
        (["superforcaster"], "superforcaster"),
    ],
)
def test_priority_tool_reaches_request(
    tmp_path: Any, offered: Any, expected: str
) -> None:
    """Send the selected priority tool in the actual decision request payload."""
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        watch, payload = select_request(tmp_path, offered)
    requests = json.loads(payload.mech_requests)
    assert len(requests) == 1
    assert requests[0]["tool"] == expected
    assert requests[0]["prompt"] == "Will it still hold? (Yes / No)"
    assert watch.synchronized_data.mech_requests[0].tool == expected


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("status", [200, 402, 503, None])
def test_paid_request_retry_and_restart(
    tmp_path: Any, monkeypatch: Any, status: Any, dynamic: bool
) -> None:
    """Keep one chargeable request per market after retry and disk reload."""
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        watch, _ = select_request(tmp_path, list(FastConfig().tools))
        context = watch.context
        context.shared_state = {}
        selected = "0xdb78159e9246ec738f51c2c9cb1169b5c0e45fee" if dynamic else MECH
        infos = json.loads(context.state.synchronized_data.db.get_strict("mechs_info"))
        infos[0]["address"] = selected
        infos[0]["http_url"] = "https://selected.test"
        context.state.synchronized_data = context.state.synchronized_data.update(
            mechs_info=json.dumps(infos), versioning_check_performed=True
        )
        context.state.penalized_mechs = set()
        context.params.use_offchain = True
        context.params.mech_chain_id = "gnosis"
        context.params.delivery_rate_caps = {PaymentType.NATIVE: 1}
        context.params.mech_marketplace_config = SimpleNamespace(
            mech_marketplace_address="0x" + "34" * 20,
            priority_mech_address=MECH,
            use_dynamic_mech_selection=dynamic,
            offchain_url="https://mech.test",
            offchain_failover_max_retries=0,
            offchain_nonce_retry_max=1,
            offchain_poll_timeout_seconds=300,
            auto_deposit_cap_per_cycle=1,
        )
        # SharedState.setup applies the service's 350-second round timeout.
        context.state.round_sequence.abci_app = SimpleNamespace(
            event_to_timeout={
                **OmenstratFastAbciApp.event_to_timeout,
                Event.ROUND_TIMEOUT: 350,
            }
        )
        posts = []
        accepted = []

        def http(**kwargs: Any) -> Any:
            form = parse_qs(kwargs["content"].decode())
            posts.append(form)
            assert (
                kwargs["url"]
                == ("https://selected.test" if dynamic else "https://mech.test")
                + "/send_signed_requests"
            )
            assert json.loads(form["ipfs_data"][0])["tool"] == FastConfig().tools[0]
            # The asked marker must be on disk before even the first POST.
            assert FreshQueue(tmp_path / "fresh_watch.json").asked == {"market": 1000}
            code = 200 if len(posts) > 1 else status
            if code == 200:
                accepted.append(form["request_id"][0])
            return done(
                None
                if code is None
                else SimpleNamespace(
                    status_code=code,
                    body=json.dumps(
                        dict(
                            payTo=SAFE,
                            asset="0x" + "00" * 20,
                            chainId=100,
                            currentBalance=0,
                            required=1,
                            error="insufficient balance",
                        )
                    ).encode(),
                )
            )

        # Simulate contract, signature, and deposit transaction I/O only.
        monkeypatch.setattr(
            OffchainRequestExecutor,
            "_read_contract_state",
            lambda self, **kw: done(
                {
                    "get_chain_id": 100,
                    "get_nonce": 0,
                    "get_payment_type": PaymentType.NATIVE.value,
                    "get_max_delivery_rate": 1,
                }[kw["contract_callable"]]
            ),
        )
        monkeypatch.setattr(
            OffchainRequestExecutor, "_sign_request_id", lambda *args: done("signature")
        )
        monkeypatch.setattr(
            OffchainRequestExecutor,
            "_validate_402_destination",
            lambda *args, **kw: done(SAFE),
        )
        monkeypatch.setattr(
            OffchainRequestExecutor, "_read_requester_balance", lambda *args: done(0)
        )
        monkeypatch.setattr(
            OffchainRequestExecutor,
            "_build_deposit_tx",
            lambda *args, **kw: done(SimpleNamespace(tx_hex="deposit")),
        )

        def send() -> Any:
            mech: Any = MechRequestBehaviour(name="mech", skill_context=context)
            mech.get_http_response = http
            events = []

            def finish(payload: Any) -> Any:
                events.append(agree(context, MechRequestRound, payload))
                return done(None)

            mech.finish_behaviour = finish
            run(mech.async_act())
            return events[0]

        event = send()
        if status == 402:
            assert event == Event.OFFCHAIN_DEPOSIT_NEEDED
            assert (
                context.state.synchronized_data.offchain_pending_request["mech_address"]
                == selected.lower()
            )
            # Reload the pending request from serialized consensus state.
            saved = tmp_path / "consensus.json"
            saved.write_text(context.state.synchronized_data.db.serialize())
            restored = AbciAppDB(setup_data={})
            restored.sync(saved.read_text())
            context.state.synchronized_data = SynchronizedData(restored)
            assert send() == Event.OFFCHAIN_DONE
            assert posts[0] == posts[1]
        else:
            assert event == (
                Event.OFFCHAIN_DONE if status == 200 else Event.OFFCHAIN_ALL_FAILED
            )

        # Retry discovery in this process, then restart from the durable queue.
        for foreground in (watch, make(FreshWatchBehaviour, tmp_path)):
            foreground.context.state.synchronized_data = context.state.synchronized_data
            foreground._fetch_fresh = lambda *args: done([raw_market()])
            foreground.context.state.fast_last_poll = 0
            foreground.sleep = lambda *args: done(None)
            selection = run(foreground.select())
            agree(
                context,
                FreshWatchRound,
                SelectionPayload("agent", json.dumps(selection)),
            )
            if selection["lane"] == "market":
                send()
            assert selection["lane"] != "market"
        assert len(posts) == (2 if status == 402 else 1)
        assert len(accepted) == (1 if status in (200, 402) else 0)
        assert FreshQueue(tmp_path / "fresh_watch.json").pending == []


def test_fast_request_rotates_after_slow_answer_and_failed_post(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Rotate after a slow answer, then leave a mech whose POST fails."""
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        watch, _ = select_request(tmp_path, list(FastConfig().tools))
    context = watch.context
    context.shared_state = {}
    other = "0xdb78159e9246ec738f51c2c9cb1169b5c0e45fee"
    infos = json.loads(context.state.synchronized_data.db.get_strict("mechs_info"))
    infos[0]["http_url"] = "https://first.test"
    infos[0]["max_delivery_rate"] = 1
    infos.append(dict(infos[0], id="976", address=other, http_url="https://other.test"))
    context.state.synchronized_data = context.state.synchronized_data.update(
        mechs_info=json.dumps(infos),
        preferred_mechs=[MECH.lower()],
        versioning_check_performed=True,
    )
    context.state.penalized_mechs = {}
    context.state.penalize_mech.side_effect = lambda address: (
        context.state.penalized_mechs.update({address: 1081})
    )
    context.params.use_offchain = True
    context.params.fast_dynamic_mech = True
    context.params.fast_mech_start = "ranked"
    context.params.fast_mech_rotate_latency_s = 30
    context.params.mech_chain_id = "gnosis"
    context.params.delivery_rate_caps = {PaymentType.NATIVE: 1}
    context.params.mech_marketplace_config = SimpleNamespace(
        mech_marketplace_address="0x" + "34" * 20,
        priority_mech_address=MECH,
        use_dynamic_mech_selection=True,
        offchain_failover_max_retries=0,
        offchain_nonce_retry_max=1,
        offchain_poll_timeout_seconds=300,
    )
    context.state.round_sequence.abci_app = SimpleNamespace(
        event_to_timeout={Event.ROUND_TIMEOUT: 350}
    )
    requests = context.state.synchronized_data.mech_requests
    posts = []

    def http(**kwargs: Any) -> Any:
        posts.append(kwargs["url"])
        return done(
            SimpleNamespace(status_code=503 if len(posts) == 2 else 200, body=b"busy")
        )

    monkeypatch.setattr(
        OffchainRequestExecutor,
        "_read_contract_state",
        lambda self, **kw: done(
            {
                "get_chain_id": 100,
                "get_nonce": len(posts),
                "get_payment_type": PaymentType.NATIVE.value,
                "get_max_delivery_rate": 1,
            }[kw["contract_callable"]]
        ),
    )
    monkeypatch.setattr(
        OffchainRequestExecutor, "_sign_request_id", lambda *args: done("signature")
    )

    def send() -> Any:
        context.state.synchronized_data = context.state.synchronized_data.update(
            mech_requests=json.dumps(
                [dict(vars(requests[0]), nonce=f"request-{len(posts)}")]
            ),
            offchain_pending_request=None,
        )
        mech: Any = MechRequestBehaviour(name="mech", skill_context=context)
        mech.get_http_response = http
        events = []

        def finish(payload: Any) -> Any:
            events.append(agree(context, MechRequestRound, payload))
            return done(None)

        mech.finish_behaviour = finish
        run(mech.async_act())
        return events[0]

    with patch(
        "packages.valory.skills.trader_abci.mech_latency.time.time", return_value=1050
    ):
        assert send() == Event.OFFCHAIN_DONE
    nonce = context.state.synchronized_data.mech_requests[0].nonce
    response = TraderMechResponseBehaviour(name="response", skill_context=context)
    with (
        patch.object(
            MechResponseBehaviour, "finish_behaviour", return_value=done(None)
        ),
        patch(
            "packages.valory.skills.trader_abci.mech_latency.time.time",
            return_value=1081,
        ),
    ):
        run(
            response.finish_behaviour(
                JSONPayload("agent", json.dumps([dict(nonce=nonce, result="answer")]))
            )
        )
    assert context.shared_state["fast_mech_rotation"]["last_latency_s"] == 31
    assert len(context.state.synchronized_data.ranked_mechs) == 2
    assert send() == Event.OFFCHAIN_ALL_FAILED
    assert posts[:2] == [
        "https://first.test/send_signed_requests",
        "https://other.test/send_signed_requests",
    ]
    assert send() == Event.OFFCHAIN_DONE
    assert posts[2] == "https://first.test/send_signed_requests"
