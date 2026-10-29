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

"""Check RPC deadlines with scheduler ticks and no response messages."""

import datetime
from types import SimpleNamespace
from typing import Any, Generator, cast
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import AsyncBehaviour
from packages.valory.skills.abstract_round_abci.handlers import (
    ContractApiHandler,
    LedgerApiHandler,
)
from packages.valory.skills.fresh_watch_abci.behaviours import FastBetPlacementBehaviour
from packages.valory.skills.fresh_watch_abci.chain_discovery import (
    ChainDiscoveryMixin,
    ChainRPCError,
)


class ScheduledRPC(ChainDiscoveryMixin, AsyncBehaviour):
    """Run an actual chain or preflight wait through the async scheduler."""

    def __init__(self, preflight: bool, contract: bool = False) -> None:
        """Set up a local request context with no connection."""
        super().__init__()
        dialogue = MagicMock()
        dialogue.dialogue_label.dialogue_reference = ("nonce", "")
        self.context = SimpleNamespace(
            default_ledger_id="ethereum",
            ledger_api_dialogues=MagicMock(),
            requests=SimpleNamespace(request_id_to_callback={}),
            outbox=MagicMock(),
        )
        self.context.ledger_api_dialogues.create.return_value = (MagicMock(), dialogue)
        self.context.contract_api_dialogues = self.context.ledger_api_dialogues
        self.params = SimpleNamespace(
            mech_chain_id="gnosis",
            lightning=SimpleNamespace(omen_fpmm_factory="0x" + "12" * 20),
        )
        self.contract = contract
        self.preflight = preflight
        self.completed = False
        self.result: Any = "pending"

    def _get_default_terms(self) -> None:
        """Use no payment terms for this local read."""

    @staticmethod
    def _get_request_nonce_from_dialogue(dialogue: Any) -> str:
        """Read the nonce in the same way as the base behaviour."""
        return str(dialogue.dialogue_label.dialogue_reference[0])

    def get_callback_request(self) -> Any:
        """Use the normal callback delivery contract for the old wait."""
        return lambda message, current_behaviour: self.try_send(message)

    def async_act_wrapper(self) -> Generator:
        """Run one request without the application round wrapper."""
        yield from self.async_act()

    def async_act(self) -> Generator:
        """Run the real request implementation and record its outcome."""
        try:
            if self.preflight:
                self.result = yield from FastBetPlacementBehaviour._estimate_gas(
                    cast(FastBetPlacementBehaviour, self),
                    {"from": "0x0", "to": "0x0", "data": "0x"},
                    "gnosis",
                )
            else:
                self.result = yield from self._chain_rpc(
                    "get_logs" if self.contract else "get_block",
                    block_identifier="latest",
                )
        except ChainRPCError as exc:
            self.result = exc
        self.completed = True


@pytest.mark.parametrize("preflight", [False, True])
def test_no_message_rpc_deadline_expires_on_scheduler_ticks(preflight: bool) -> None:
    """End a hung RPC on normal act ticks without sending any message."""
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1)

    class Clock(datetime.datetime):
        """Advance local sleeps with the scheduler clock."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the time of the current scheduler tick."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    behaviour = ScheduledRPC(preflight)
    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
    ):
        behaviour.act()
        assert "nonce" in behaviour.context.requests.request_id_to_callback
        for tick in range(1, 32):
            seconds[0] = tick * 0.2
            behaviour.act()
            if behaviour.completed:
                break
    assert behaviour.completed, "The scheduler must resume the deadline without a reply"
    if preflight:
        assert behaviour.result is None
    else:
        assert isinstance(behaviour.result, ChainRPCError)
    assert "nonce" in behaviour.context.requests.request_id_to_callback
    behaviour.context.requests.request_id_to_callback.pop("nonce")(None, behaviour)
    assert behaviour.context.requests.request_id_to_callback == {}
    behaviour.context.outbox.put_message.assert_called_once()


@pytest.mark.parametrize("preflight", [False, True])
def test_late_rpc_reply_after_deadline_is_rejected(preflight: bool) -> None:
    """Reject a reply received after the deadline before the next act tick."""
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1)

    class Clock(datetime.datetime):
        """Advance local sleeps with the scheduler clock."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the time of the current scheduler tick."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    behaviour = ScheduledRPC(preflight)
    response = SimpleNamespace(
        performative=LedgerApiMessage.Performative.STATE,
        state=SimpleNamespace(body={"number": 42, "estimate_gas_result": 21000}),
    )
    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
    ):
        behaviour.act()
        for tick in range(1, 25):
            seconds[0] = tick * 0.2
            behaviour.act()
        seconds[0] = 5.1
        behaviour.context.requests.request_id_to_callback.pop("nonce")(
            response, behaviour
        )
        seconds[0] = 5.2
        behaviour.act()
    assert behaviour.completed
    if preflight:
        assert behaviour.result is None
    else:
        assert isinstance(behaviour.result, ChainRPCError)
    assert behaviour.context.requests.request_id_to_callback == {}


@pytest.mark.parametrize("contract", [False, True])
def test_handler_drops_reply_after_scheduler_timeout(contract: bool) -> None:
    """A late reply must not stop the skill after the scheduler ends its wait."""
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1)

    class Clock(datetime.datetime):
        """Advance both the scheduler sleep and RPC deadline."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the current scheduler time."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    behaviour = ScheduledRPC(False, contract=contract)
    message_class: Any = ContractApiMessage if contract else LedgerApiMessage
    handler_class = ContractApiHandler if contract else LedgerApiHandler
    context = behaviour.context
    context.skill_id = "valory/fresh_watch_abci:0.1.0"
    context.logger = MagicMock()
    context.behaviours = SimpleNamespace(
        main=SimpleNamespace(current_behaviour=behaviour)
    )
    if contract:
        context.contract_api_dialogues = context.ledger_api_dialogues
    dialogue = context.ledger_api_dialogues.create.return_value[1]
    context.ledger_api_dialogues.update.return_value = dialogue
    response = message_class(
        performative=message_class.Performative.STATE,
        dialogue_reference=("nonce", "reply"),
        message_id=2,
        target=1,
        state=message_class.State(ledger_id="ethereum", body={"number": 42}),
        **({"ledger_id": "ethereum"} if not contract else {}),
    )
    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
    ):
        behaviour.act()
        for tick in range(1, 32):
            seconds[0] = tick * 0.2
            behaviour.act()
            if behaviour.completed:
                break
        assert isinstance(behaviour.result, ChainRPCError)
        handler_class(name="", skill_context=context).handle(response)
    assert context.requests.request_id_to_callback == {}
    context.logger.exception.assert_not_called()
    assert isinstance(behaviour.result, ChainRPCError)
