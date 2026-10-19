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

"""Check the Omen sweep after sharing its reserve reader with sizing."""

from pathlib import Path
from typing import Any, Generator, cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.chatui_abci.models import (
    WITHDRAWAL_STATE_COMPLETE,
    WITHDRAWAL_STATE_ERRORED,
)
from packages.valory.skills.decision_maker_abci.behaviours.omen_withdraw import (
    OmenWithdrawBehaviour,
    WXDAI,
)
from packages.valory.skills.decision_maker_abci.states.base import Event
from packages.valory.skills.market_manager_abci.graph_tooling.utils import (
    WithdrawablePosition,
)


def _result(value: Any) -> Generator[None, None, Any]:
    """Yield once and then return a result."""
    yield
    return value


def _drain(gen: Generator[None, None, Any]) -> Any:
    """Run a generator to completion."""
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value


def _behaviour() -> OmenWithdrawBehaviour:
    """Build a sweep with a local context and an in-memory store."""
    behaviour = object.__new__(OmenWithdrawBehaviour)
    behaviour._context = MagicMock()
    behaviour._store_cache = MagicMock()
    behaviour.context.params.withdrawal_slippage = 0.01
    behaviour.context.params.withdrawal_return_buffer = 0.05
    behaviour.context.params.dust_epsilon_wxdai = 0
    return behaviour


def _position() -> WithdrawablePosition:
    """Return a binary Omen position."""
    return WithdrawablePosition("0xfpmm", 0, 1000, "0xcondition", 1, "1")


def test_store_is_cached(tmp_path: Path) -> None:
    """Build one persistent store and reuse it."""
    behaviour = _behaviour()
    del behaviour._store_cache
    behaviour.context.params.store_path = tmp_path
    assert behaviour._store is behaviour._store


def test_async_sends_payload() -> None:
    """Send the built payload and complete the round."""
    behaviour = _behaviour()
    with (
        patch.object(
            behaviour, "_build_payload", side_effect=lambda: _result("payload")
        ),
        patch.object(
            behaviour, "send_a2a_transaction", side_effect=lambda payload: _result(None)
        ) as send,
        patch.object(
            behaviour, "wait_until_round_end", side_effect=lambda: _result(None)
        ),
        patch.object(behaviour, "set_done") as done,
    ):
        _drain(behaviour.async_act())
    send.assert_called_once_with("payload")
    done.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["positions", "markets", "empty", "sizing", "sizing_error", None]
)
def test_sweep_payload(failure: Any) -> None:
    """Retain the sweep's short circuits and approval deduplication."""
    behaviour = _behaviour()
    cast(MagicMock, behaviour._store_cache).has_errors.return_value = (
        failure == "sizing_error"
    )
    responses: list[Any] = [
        None if failure == "positions" else [],
        None if failure == "markets" else [],
    ]
    positions = [] if failure == "empty" else [_position(), _position(), _position()]
    batches = [None, [MagicMock()], [MagicMock()]]
    if failure in ("sizing", "sizing_error"):
        batches = [None, None, None]
    with (
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=MagicMock(safe_contract_address="0xsafe"),
        ),
        patch.object(
            type(behaviour), "tx_hex", new_callable=PropertyMock, return_value="0xtx"
        ),
        patch.object(
            behaviour,
            "_with_top_level_retry",
            side_effect=lambda name, fn: _result(responses.pop(0)),
        ),
        patch(
            "packages.valory.skills.decision_maker_abci.behaviours.omen_withdraw.get_withdrawable_positions",
            return_value=positions,
        ),
        patch.object(
            behaviour,
            "_size_and_build_position",
            side_effect=lambda pos, **kw: _result(batches.pop(0)),
        ) as size,
        patch.object(
            behaviour,
            "wait_for_condition_with_sleep",
            side_effect=lambda step: _result(None),
        ),
    ):
        payload = _drain(behaviour._build_payload())
    if failure is None:
        assert payload.event == Event.PREPARE_TX.value
        assert payload.tx_hash == "0xtx"
        assert [c.kwargs["include_approval"] for c in size.call_args_list] == [
            True,
            True,
            False,
        ]
    else:
        assert payload.event == Event.WITHDRAWAL_DONE.value
        assert payload.tx_hash is None
        terminal = (
            WITHDRAWAL_STATE_ERRORED
            if failure in ("positions", "markets", "sizing_error")
            else WITHDRAWAL_STATE_COMPLETE
        )
        cast(MagicMock, behaviour._store_cache).set_state.assert_called_with(terminal)


@pytest.mark.parametrize("values", [["success"], [None, "success"], [None, None, None]])
def test_retry_schedule(values: list) -> None:
    """Retry failed reads with only the configured backoff entries."""
    behaviour = _behaviour()
    behaviour.context.params.withdrawal_max_fak_attempts = 3
    behaviour.context.params.withdrawal_fak_backoff_s = [0]
    results = iter(values)
    with patch.object(
        behaviour, "sleep", side_effect=lambda delay: _result(None)
    ) as sleep:
        result = _drain(
            behaviour._with_top_level_retry("read", lambda: _result(next(results)))
        )
    assert result == values[-1]
    assert sleep.call_count == (len(values) > 1)
    assert cast(MagicMock, behaviour._store_cache).record_top_level_error.called == (
        result is None
    )


@pytest.mark.parametrize(
    "failure", ["pool", "dust", "calc", "zero", "approval", "sell"]
)
def test_sizing_failure(failure: str) -> None:
    """Keep per-position failures out of the withdrawal transaction."""
    behaviour = _behaviour()
    if failure == "dust":
        behaviour.context.params.dust_epsilon_wxdai = 1000
    with (
        patch.object(
            behaviour,
            "_read_pool_balances",
            side_effect=lambda pos: _result(
                None if failure == "pool" else [1000, 1000]
            ),
        ),
        patch.object(
            behaviour,
            "_calc_sell_amount_static",
            side_effect=lambda *args: _result(
                None if failure == "calc" else 0 if failure == "zero" else 100
            ),
        ),
        patch.object(
            behaviour,
            "_build_set_approval_batch",
            side_effect=lambda fpmm: _result(
                None if failure == "approval" else MagicMock()
            ),
        ),
        patch.object(
            behaviour,
            "_build_sell_batch",
            side_effect=lambda *args: _result(
                None if failure == "sell" else MagicMock()
            ),
        ),
    ):
        assert _drain(behaviour._size_and_build_position(_position())) is None
    assert cast(MagicMock, behaviour._store_cache).record_error.called == (
        failure != "dust"
    )


@pytest.mark.parametrize(
    "method",
    [
        "_read_pool_balances",
        "_calc_sell_amount_static",
        "_build_set_approval_batch",
        "_build_sell_batch",
    ],
)
@pytest.mark.parametrize("response_type", ["error", "missing", "success"])
def test_contract_reads(method: str, response_type: str) -> None:
    """Preserve the contract calls and their failure results."""
    behaviour = _behaviour()
    response = MagicMock()
    response.performative = (
        ContractApiMessage.Performative.ERROR
        if response_type == "error"
        else (
            ContractApiMessage.Performative.RAW_TRANSACTION
            if method == "_calc_sell_amount_static"
            else ContractApiMessage.Performative.STATE
        )
    )
    body = (
        {}
        if response_type == "missing"
        else {"balances": ["10", "20"], "amount": "100", "data": "0x1234"}
    )
    response.state.body = body
    response.raw_transaction.body = body
    args = {
        "_read_pool_balances": (_position(),),
        "_calc_sell_amount_static": ("0xfpmm", 100, 0),
        "_build_set_approval_batch": ("0xfpmm",),
        "_build_sell_batch": (_position(), 100, 200),
    }[method]
    with patch.object(
        behaviour,
        "get_contract_api_response",
        side_effect=lambda **kw: _result(response),
    ) as read:
        result = _drain(getattr(behaviour, method)(*args))
    if response_type != "success":
        assert result is None
    elif method == "_read_pool_balances":
        assert result == [10, 20]
        assert read.call_args.kwargs["collateral_token"] == WXDAI
    elif method == "_calc_sell_amount_static":
        assert result == 100
    else:
        assert result.data == bytes.fromhex("1234")
