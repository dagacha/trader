# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024-2026 Valory AG
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

"""Tests for the behaviours module of the trader_abci skill."""

# pylint: skip-file

from typing import Any, Generator, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.behaviours import BaseBehaviour
from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour
from packages.valory.skills.transaction_settlement_abci.behaviours import (
    ValidateTransactionBehaviour,
)


def test_behaviours_set_contains_base_behaviours() -> None:
    """Test that all elements in the behaviours set are BaseBehaviour subclasses."""
    for behaviour_cls in TraderConsensusBehaviour.behaviours:
        assert issubclass(
            behaviour_cls, BaseBehaviour
        ), f"{behaviour_cls} is not a subclass of BaseBehaviour"


TX_HASH = "0x" + "ab" * 32


def _return(value: Any) -> Generator[None, None, Any]:
    """Return a value from a generator."""
    yield from ()
    return value


@pytest.fixture
def validation() -> Generator[ValidateTransactionBehaviour, None, None]:
    """Use the validation behaviour selected by the composed trader."""
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        ValidateTransactionRound,
    )

    classes = [
        cls
        for cls in TraderConsensusBehaviour.behaviours
        if cls.matching_round is ValidateTransactionRound
    ]
    assert len(classes) == 1
    behaviour = object.__new__(classes[0])
    context = MagicMock()
    context.params.retry_attempts = 2
    context.params.retry_timeout = 3
    context.params.default_chain_id = "gnosis"
    synced = context.state.synchronized_data
    synced.to_be_validated_tx_hash = TX_HASH
    synced.get_chain_id.side_effect = lambda default: default
    with patch.object(classes[0], "context", new_callable=PropertyMock) as prop:
        prop.return_value = context
        yield behaviour


def _receipt(status: Optional[int]) -> LedgerApiMessage:
    """Build a raw ledger response."""
    return LedgerApiMessage(
        performative=LedgerApiMessage.Performative.STATE,  # type: ignore[arg-type]
        ledger_id="ethereum",
        state=LedgerApiMessage.State("ethereum", {"status": status}),
    )


def _verified(value: bool) -> ContractApiMessage:
    """Build a Safe verification response."""
    return ContractApiMessage(
        performative=ContractApiMessage.Performative.STATE,  # type: ignore[arg-type]
        state=ContractApiMessage.State("ethereum", {"verified": value}),
    )


def _result(generator: Generator[Any, None, Any]) -> Any:
    """Read a generator that must finish without a wait."""
    with pytest.raises(StopIteration) as exc:
        next(generator)
    return exc.value.value


def test_reverted_receipt_fails_without_replay(
    validation: ValidateTransactionBehaviour,
) -> None:
    """A mined revert must produce a negative vote without receipt enrichment."""
    with (
        patch.object(
            validation, "get_ledger_api_response", return_value=_return(_receipt(0))
        ) as raw,
        patch.object(
            validation,
            "get_transaction_receipt",
            side_effect=ValueError("The given transaction has not been reverted!"),
        ),
        patch.object(validation, "_verify_tx") as verify,
    ):
        assert _result(validation.has_transaction_been_sent()) is False
        raw.assert_called_once_with(
            performative=LedgerApiMessage.Performative.GET_STATE,
            ledger_callable="get_transaction_receipt",
            transaction_hash=TX_HASH,
            chain_id="gnosis",
        )
        verify.assert_not_called()


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("chain_id", ["gnosis", "polygon"])
def test_successful_receipt_still_checks_safe_payload(
    validation: ValidateTransactionBehaviour, verified: bool, chain_id: str
) -> None:
    """A mined success must still pass Safe payload verification on its chain."""
    validation.context.params.default_chain_id = chain_id
    with (
        patch.object(
            validation, "get_ledger_api_response", return_value=_return(_receipt(1))
        ) as raw,
        patch.object(
            validation, "_verify_tx", return_value=_return(_verified(verified))
        ) as verify,
    ):
        assert _result(validation.has_transaction_been_sent()) is verified
        assert raw.call_args.kwargs["chain_id"] == chain_id
        verify.assert_called_once_with(TX_HASH)


def test_pending_receipt_waits_then_validates(
    validation: ValidateTransactionBehaviour,
) -> None:
    """A missing receipt must wait before the next lookup."""
    pending = LedgerApiMessage(
        performative=LedgerApiMessage.Performative.ERROR,  # type: ignore[arg-type]
        code=500,
        message="Transaction not found",
        data=b"",
    )

    def wait(seconds: int) -> Generator[str, None, None]:
        assert seconds == 3
        yield "waiting"

    with (
        patch.object(
            validation,
            "get_ledger_api_response",
            side_effect=[_return(pending), _return(_receipt(1))],
        ) as raw,
        patch.object(validation, "sleep", side_effect=wait),
        patch.object(validation, "_verify_tx", return_value=_return(_verified(True))),
    ):
        action = validation.has_transaction_been_sent()
        assert next(action) == "waiting"
        assert raw.call_count == 1
        assert _result(action) is True
        assert raw.call_count == 2


@pytest.mark.parametrize("rpc_error", [False, True])
def test_missing_receipt_keeps_polling_without_vote(
    validation: ValidateTransactionBehaviour, rpc_error: bool
) -> None:
    """Missing receipts must wait past the retry count without a payload."""
    response = (
        LedgerApiMessage(
            performative=LedgerApiMessage.Performative.ERROR,  # type: ignore[arg-type]
            code=500,
            message="Transaction not found",
            data=b"",
        )
        if rpc_error
        else _receipt(None)
    )

    def wait(seconds: int) -> Generator[str, None, None]:
        assert seconds == validation.params.retry_timeout
        yield "waiting"

    with (
        patch.object(
            validation,
            "get_ledger_api_response",
            side_effect=lambda **kwargs: _return(response),
        ) as raw,
        patch.object(validation, "sleep", side_effect=wait),
        patch.object(validation, "_verify_tx") as verify,
        patch.object(
            validation, "send_a2a_transaction", return_value=_return(None)
        ) as send,
        patch.object(validation, "wait_until_round_end", return_value=_return(None)),
    ):
        action = validation.async_act()
        try:
            for count in range(1, validation.params.retry_attempts + 4):
                assert next(action) == "waiting"
                assert raw.call_count == count
                send.assert_not_called()
                verify.assert_not_called()
        finally:
            action.close()


def test_safe_verification_error_is_negative(
    validation: ValidateTransactionBehaviour,
) -> None:
    """Keep the existing negative vote when Safe verification fails."""
    error = ContractApiMessage(
        performative=ContractApiMessage.Performative.ERROR,  # type: ignore[arg-type]
        code=500,
        message="Cannot verify",
        data=b"",
    )
    with (
        patch.object(
            validation, "get_ledger_api_response", return_value=_return(_receipt(1))
        ),
        patch.object(validation, "_verify_tx", return_value=_return(error)),
    ):
        assert _result(validation.has_transaction_been_sent()) is False


def test_reverted_receipt_sends_negative_vote_without_timeout(
    validation: ValidateTransactionBehaviour,
) -> None:
    """A reverted receipt starts the existing failure route in the same action."""
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.decision_maker_abci.states.handle_failed_tx import (
        HandleFailedTxRound,
    )
    from packages.valory.skills.trader_abci.composition import TraderAbciApp
    from packages.valory.skills.transaction_settlement_abci.rounds import (
        CheckTransactionHistoryRound,
        Event,
        SynchronizedData,
        ValidateTransactionRound,
    )

    validation.context.agent_address = "agent"
    with (
        patch.object(
            validation, "get_ledger_api_response", return_value=_return(_receipt(0))
        ),
        patch.object(
            validation, "send_a2a_transaction", return_value=_return(None)
        ) as send,
        patch.object(validation, "wait_until_round_end", return_value=_return(None)),
    ):
        assert _result(validation.async_act()) is None
        payload = send.call_args.args[0]
        assert payload.vote is False
        assert validation.is_done()

    synced = SynchronizedData(
        AbciAppDB(
            setup_data={
                "participants": [("agent",)],
                "all_participants": [("agent",)],
                "consensus_threshold": [1],
            }
        )
    )
    round_ = ValidateTransactionRound(synced, validation.context)
    round_.process_payload(payload)
    result = round_.end_block()
    assert result is not None
    assert result[1] == Event.NEGATIVE
    transitions = TraderAbciApp.transition_function
    assert (
        transitions[ValidateTransactionRound][Event.NEGATIVE]
        is CheckTransactionHistoryRound
    )
    assert transitions[CheckTransactionHistoryRound][Event.NONE] is HandleFailedTxRound
