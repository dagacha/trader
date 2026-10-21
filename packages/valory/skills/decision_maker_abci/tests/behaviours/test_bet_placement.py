# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023-2026 Valory AG
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

"""Tests for BetPlacementBehaviour."""

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Generator, cast
from unittest.mock import MagicMock, PropertyMock, mock_open, patch

import pytest

from packages.valory.skills.decision_maker_abci.behaviours.base import WXDAI
from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_gen():  # type: ignore[no-untyped-def]
    """A no-op generator that yields once."""
    yield  # type: ignore[no-untyped-def]


def _return_gen(value):  # type: ignore[no-untyped-def]
    """A generator that yields once and returns a value."""
    yield  # type: ignore[no-untyped-def]
    return value


def _make_behaviour():  # type: ignore[no-untyped-def]
    """Return a BetPlacementBehaviour with mocked dependencies."""
    behaviour = object.__new__(BetPlacementBehaviour)  # type: ignore[no-untyped-def]
    behaviour.buy_amount = 0
    behaviour.quoted_tokens = 1000

    context = MagicMock()
    context.agent_address = "test_agent"
    context.params.is_running_on_polymarket = False
    context.params.placement_min_edge = 0.0
    context.benchmarking_mode.enabled = False
    behaviour.__dict__["_context"] = context

    benchmarking_mode = MagicMock()
    benchmarking_mode.enabled = False

    return behaviour, benchmarking_mode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBetPlacementBehaviour:
    """Tests for BetPlacementBehaviour."""

    def test_init_sets_buy_amount(self) -> None:
        """__init__ should set buy_amount to 0."""
        with patch(
            "packages.valory.skills.decision_maker_abci.behaviours.bet_placement.DecisionMakerBaseBehaviour.__init__",
            return_value=None,
        ):
            behaviour = BetPlacementBehaviour(name="test", skill_context=MagicMock())
            assert behaviour.buy_amount == 0

    def test_w_xdai_deficit_property(self) -> None:
        """w_xdai_deficit should return investment_amount - token_balance."""
        behaviour, bm = _make_behaviour()
        behaviour.token_balance = 100

        with patch.object(
            type(behaviour), "investment_amount", new_callable=PropertyMock
        ) as mock_inv:
            mock_inv.return_value = 250
            assert behaviour.w_xdai_deficit == 150

    def test_build_exchange_tx_success(self) -> None:
        """_build_exchange_tx should return True on success and append a batch."""
        behaviour, bm = _make_behaviour()
        behaviour.multisend_batches = []

        with patch.object(
            type(behaviour), "collateral_token", new_callable=PropertyMock
        ) as mock_ct:
            mock_ct.return_value = WXDAI
            with patch.object(
                type(behaviour), "w_xdai_deficit", new_callable=PropertyMock
            ) as mock_deficit:
                mock_deficit.return_value = 100
                with patch.object(
                    type(behaviour), "params", new_callable=PropertyMock
                ) as mock_params:
                    mock_params.return_value = MagicMock(mech_chain_id="gnosis")

                    # Mock get_contract_api_response
                    response_msg = MagicMock()
                    from packages.valory.protocols.contract_api import (
                        ContractApiMessage,
                    )

                    response_msg.performative = ContractApiMessage.Performative.STATE
                    response_msg.state.body = {"data": "0xdeadbeef"}

                    behaviour.get_contract_api_response = lambda **kwargs: _return_gen(  # type: ignore[method-assign]
                        response_msg
                    )

                    gen = behaviour._build_exchange_tx()
                    result = None
                    try:
                        while True:
                            next(gen)
                    except StopIteration as e:
                        result = e.value

                    assert result is True
                    assert len(behaviour.multisend_batches) == 1

    def test_build_exchange_tx_failure_bad_performative(self) -> None:
        """_build_exchange_tx should return False on bad performative."""
        behaviour, bm = _make_behaviour()

        with patch.object(
            type(behaviour), "params", new_callable=PropertyMock
        ) as mock_params:
            mock_params.return_value = MagicMock(mech_chain_id="gnosis")

            response_msg = MagicMock()
            from packages.valory.protocols.contract_api import ContractApiMessage

            response_msg.performative = ContractApiMessage.Performative.ERROR

            behaviour.get_contract_api_response = lambda **kwargs: _return_gen(  # type: ignore[method-assign]
                response_msg
            )

            gen = behaviour._build_exchange_tx()
            result = None
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                result = e.value

            assert result is False

    def test_build_exchange_tx_failure_no_data(self) -> None:
        """_build_exchange_tx should return False when data is None."""
        behaviour, bm = _make_behaviour()

        with patch.object(
            type(behaviour), "params", new_callable=PropertyMock
        ) as mock_params:
            mock_params.return_value = MagicMock(mech_chain_id="gnosis")

            response_msg = MagicMock()
            from packages.valory.protocols.contract_api import ContractApiMessage

            response_msg.performative = ContractApiMessage.Performative.STATE
            response_msg.state.body = {"data": None}

            behaviour.get_contract_api_response = lambda **kwargs: _return_gen(  # type: ignore[method-assign]
                response_msg
            )

            gen = behaviour._build_exchange_tx()
            result = None
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                result = e.value

            assert result is False

    def test_build_approval_tx(self) -> None:
        """_build_approval_tx should delegate to build_approval_tx."""
        behaviour, bm = _make_behaviour()

        def mock_build_approval(amount, spender, token) -> None:  # type: ignore[no-untyped-def, misc]
            """Mock build approval tx."""
            yield  # type: ignore[no-untyped-def]
            return True

        behaviour.build_approval_tx = mock_build_approval  # type: ignore[method-assign]

        with patch.object(
            type(behaviour), "investment_amount", new_callable=PropertyMock
        ) as mock_inv:
            mock_inv.return_value = 500
            with patch.object(
                type(behaviour),
                "market_maker_contract_address",
                new_callable=PropertyMock,
            ) as mock_mca:
                mock_mca.return_value = "0xmarket"
                with patch.object(
                    type(behaviour), "collateral_token", new_callable=PropertyMock
                ) as mock_ct:
                    mock_ct.return_value = WXDAI

                    gen = behaviour._build_approval_tx()
                    result = None
                    try:
                        while True:
                            next(gen)
                    except StopIteration as e:
                        result = e.value

                    assert result is True

    def test_prepare_safe_tx_calc_fails(self) -> None:
        """_prepare_safe_tx should return None when _calc_buy_amount fails."""
        behaviour, bm = _make_behaviour()

        def mock_wait(condition) -> None:  # type: ignore[no-untyped-def, misc]
            """Mock wait for condition."""
            yield  # type: ignore[no-untyped-def]

        behaviour.wait_for_condition_with_sleep = mock_wait  # type: ignore[method-assign]

        def mock_calc() -> None:  # type: ignore[no-untyped-def, misc]
            """Mock calc buy amount that fails."""
            yield  # type: ignore[no-untyped-def]
            return False

        behaviour._calc_buy_amount = mock_calc  # type: ignore[method-assign]

        gen = behaviour._prepare_safe_tx()
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

        assert result is None

    def test_prepare_safe_tx_success(self) -> None:
        """_prepare_safe_tx should return tx_hex on success."""
        behaviour, bm = _make_behaviour()
        behaviour.buy_amount = 1000

        def mock_wait(condition) -> None:  # type: ignore[no-untyped-def, misc]
            """Mock wait for condition."""
            yield  # type: ignore[no-untyped-def]

        behaviour.wait_for_condition_with_sleep = mock_wait  # type: ignore[method-assign]

        def mock_calc() -> None:  # type: ignore[no-untyped-def, misc]
            """Mock calc buy amount."""
            yield  # type: ignore[no-untyped-def]
            return True

        behaviour._calc_buy_amount = mock_calc  # type: ignore[method-assign]

        mock_bet = MagicMock()
        mock_bet.get_outcome.return_value = "Yes"

        with patch.object(
            type(behaviour), "sampled_bet", new_callable=PropertyMock
        ) as mock_sb:
            mock_sb.return_value = mock_bet
            with patch.object(
                type(behaviour), "outcome_index", new_callable=PropertyMock
            ) as mock_oi:
                mock_oi.return_value = 0
                with patch.object(
                    type(behaviour), "investment_amount", new_callable=PropertyMock
                ) as mock_inv:
                    mock_inv.return_value = 500
                    with patch.object(
                        type(behaviour), "synchronized_data", new_callable=PropertyMock
                    ) as mock_sd:
                        mock_sd.return_value = MagicMock(
                            confidence=0.8,
                            mech_responses=[
                                MagicMock(
                                    result=json.dumps(
                                        dict(
                                            p_yes=0.8,
                                            p_no=0.2,
                                            confidence=0.8,
                                            info_utility=1.0,
                                        )
                                    )
                                )
                            ],
                        )
                        with patch.object(
                            type(behaviour), "tx_hex", new_callable=PropertyMock
                        ) as mock_tx:
                            mock_tx.return_value = "0xfinalhash"

                            behaviour._collateral_amount_info = lambda x: f"{x} WEI"  # type: ignore[method-assign]

                            gen = behaviour._prepare_safe_tx()
                            result = None
                            try:
                                while True:
                                    next(gen)
                            except StopIteration as e:
                                result = e.value

        assert result == "0xfinalhash"

    def test_async_act_benchmarking_mode(self) -> None:
        """In benchmarking mode, async_act should send payload with mocking_mode=True."""
        behaviour, bm = _make_behaviour()
        bm.enabled = True

        payloads_sent = []

        behaviour.send_a2a_transaction = lambda payload: (  # type: ignore[method-assign]
            payloads_sent.append(payload) or (yield)  # type: ignore[func-returns-value]
        )
        behaviour.wait_until_round_end = lambda: _noop_gen()  # type: ignore[func-returns-value, method-assign]
        behaviour.set_done = MagicMock()  # type: ignore[method-assign]
        behaviour.update_bet_transaction_information = MagicMock()  # type: ignore[method-assign]
        behaviour.wallet_balance = 1000

        # After benchmarking finish_behaviour completes, code falls through.
        # Mock everything needed by the non-benchmarking path too.
        behaviour.wait_for_condition_with_sleep = lambda cond: _noop_gen()  # type: ignore[method-assign]
        behaviour.token_balance = 0

        with patch.object(
            type(behaviour), "benchmarking_mode", new_callable=PropertyMock
        ) as mock_bm:
            mock_bm.return_value = bm
            with patch.object(
                type(behaviour), "investment_amount", new_callable=PropertyMock
            ) as mock_inv:
                mock_inv.return_value = 100
                with patch.object(
                    type(behaviour), "is_wxdai", new_callable=PropertyMock
                ) as mock_wxdai:
                    mock_wxdai.return_value = False

                    gen = behaviour.async_act()
                    try:
                        while True:
                            next(gen)
                    except StopIteration:
                        pass

        # First payload should be the benchmarking payload with mocking_mode=True
        assert len(payloads_sent) >= 1
        payload = payloads_sent[0]
        assert isinstance(payload, BetPlacementPayload)
        assert payload.mocking_mode is True
        behaviour.update_bet_transaction_information.assert_called_once()

    def test_async_act_sufficient_balance(self) -> None:
        """When token_balance >= investment_amount, should prepare safe tx."""
        behaviour, bm = _make_behaviour()

        payloads_sent = []

        behaviour.send_a2a_transaction = lambda payload: (  # type: ignore[method-assign]
            payloads_sent.append(payload) or (yield)  # type: ignore[func-returns-value]
        )
        behaviour.wait_until_round_end = lambda: _noop_gen()  # type: ignore[func-returns-value, method-assign]
        behaviour.set_done = MagicMock()  # type: ignore[method-assign]
        behaviour.wallet_balance = 2000
        behaviour.token_balance = 500
        behaviour.check_balance = MagicMock()  # type: ignore[method-assign]

        behaviour.wait_for_condition_with_sleep = lambda cond: _noop_gen()  # type: ignore[method-assign]

        def mock_prepare() -> None:  # type: ignore[no-untyped-def, misc]
            """Mock prepare safe tx."""
            yield  # type: ignore[no-untyped-def]
            return "0xsafetxhash"

        behaviour._prepare_safe_tx = mock_prepare  # type: ignore[method-assign]

        with patch.object(
            type(behaviour), "benchmarking_mode", new_callable=PropertyMock
        ) as mock_bm:
            mock_bm.return_value = MagicMock(enabled=False)
            with patch.object(
                type(behaviour), "investment_amount", new_callable=PropertyMock
            ) as mock_inv:
                mock_inv.return_value = 100
                with patch.object(
                    type(behaviour), "is_wxdai", new_callable=PropertyMock
                ) as mock_wxdai:
                    mock_wxdai.return_value = False
                    with patch.object(
                        type(behaviour),
                        "synchronized_data",
                        new_callable=PropertyMock,
                    ) as mock_sd:
                        mock_sd.return_value = MagicMock(is_policy_set=False)

                        gen = behaviour.async_act()
                        try:
                            while True:
                                next(gen)
                        except StopIteration:
                            pass

        assert len(payloads_sent) >= 1
        payload = payloads_sent[-1]
        assert isinstance(payload, BetPlacementPayload)
        assert payload.tx_hash == "0xsafetxhash"

    def test_async_act_policy_increment_on_success(self) -> None:
        """When betting_tx_hex is not None and is_policy_set, policy should be incremented and written to disk."""
        behaviour, bm = _make_behaviour()

        payloads_sent = []

        behaviour.send_a2a_transaction = lambda payload: (  # type: ignore[method-assign]
            payloads_sent.append(payload) or (yield)  # type: ignore[func-returns-value]
        )
        behaviour.wait_until_round_end = lambda: _noop_gen()  # type: ignore[func-returns-value, method-assign]
        behaviour.set_done = MagicMock()  # type: ignore[method-assign]
        behaviour.wallet_balance = 2000
        behaviour.token_balance = 500
        behaviour.check_balance = MagicMock()  # type: ignore[method-assign]

        behaviour.wait_for_condition_with_sleep = lambda cond: _noop_gen()  # type: ignore[method-assign]

        def mock_prepare() -> None:  # type: ignore[no-untyped-def, misc]
            """Mock prepare safe tx."""
            yield  # type: ignore[no-untyped-def]
            return "0xsafetxhash"

        behaviour._prepare_safe_tx = mock_prepare  # type: ignore[method-assign]

        mock_policy = MagicMock()
        mock_policy.serialize.return_value = '{"serialized": "policy"}'

        mock_sd = MagicMock(
            is_policy_set=True,
            policy=mock_policy,
            mech_tool="tool1",
            mech_tool_key="tool1",
        )

        store_path = Path("/tmp/test_store")  # nosec

        with patch.object(
            type(behaviour), "benchmarking_mode", new_callable=PropertyMock
        ) as mock_bm:
            mock_bm.return_value = MagicMock(enabled=False)
            with patch.object(
                type(behaviour), "investment_amount", new_callable=PropertyMock
            ) as mock_inv:
                mock_inv.return_value = 100
                with patch.object(
                    type(behaviour), "is_wxdai", new_callable=PropertyMock
                ) as mock_wxdai:
                    mock_wxdai.return_value = False
                    with patch.object(
                        type(behaviour),
                        "synchronized_data",
                        new_callable=PropertyMock,
                    ) as mock_sd_prop:
                        mock_sd_prop.return_value = mock_sd
                        with patch.object(
                            type(behaviour),
                            "params",
                            new_callable=PropertyMock,
                        ) as mock_params:
                            mock_params.return_value = MagicMock(store_path=store_path)
                            with patch("builtins.open", mock_open()) as mocked_file:
                                gen = behaviour.async_act()
                                try:
                                    while True:
                                        next(gen)
                                except StopIteration:
                                    pass

        assert len(payloads_sent) >= 1
        payload = payloads_sent[-1]
        assert isinstance(payload, BetPlacementPayload)
        assert payload.tx_hash == "0xsafetxhash"
        # Verify policy was incremented and serialized
        mock_policy.tool_used.assert_called_once_with("tool1")
        mock_policy.serialize.assert_called_once()
        assert payload.policy == '{"serialized": "policy"}'
        # Verify file was written
        mocked_file.assert_called_once()
        mocked_file().write.assert_called_once_with('{"serialized": "policy"}')

    def test_async_act_insufficient_balance_can_exchange(self) -> None:
        """When token_balance < investment but can exchange, should build exchange tx."""
        behaviour, bm = _make_behaviour()

        payloads_sent = []

        behaviour.send_a2a_transaction = lambda payload: (  # type: ignore[method-assign]
            payloads_sent.append(payload) or (yield)  # type: ignore[func-returns-value]
        )
        behaviour.wait_until_round_end = lambda: _noop_gen()  # type: ignore[func-returns-value, method-assign]
        behaviour.set_done = MagicMock()  # type: ignore[method-assign]
        behaviour.wallet_balance = 2000
        behaviour.token_balance = 50
        behaviour.check_balance = MagicMock()  # type: ignore[method-assign]

        wait_calls = []

        def mock_wait(cond) -> None:  # type: ignore[no-untyped-def, misc]
            """Mock wait for condition with sleep."""
            wait_calls.append(cond)  # type: ignore[no-untyped-def]
            yield

        behaviour.wait_for_condition_with_sleep = mock_wait  # type: ignore[method-assign]

        def mock_prepare() -> None:  # type: ignore[no-untyped-def, misc]
            """Mock prepare safe tx."""
            yield  # type: ignore[no-untyped-def]
            return "0xexchangehash"

        behaviour._prepare_safe_tx = mock_prepare  # type: ignore[method-assign]

        with patch.object(
            type(behaviour), "benchmarking_mode", new_callable=PropertyMock
        ) as mock_bm:
            mock_bm.return_value = MagicMock(enabled=False)
            with patch.object(
                type(behaviour), "investment_amount", new_callable=PropertyMock
            ) as mock_inv:
                mock_inv.return_value = 100
                with patch.object(
                    type(behaviour), "is_wxdai", new_callable=PropertyMock
                ) as mock_wxdai:
                    mock_wxdai.return_value = True
                    with patch.object(
                        type(behaviour), "w_xdai_deficit", new_callable=PropertyMock
                    ) as mock_deficit:
                        mock_deficit.return_value = 50
                        with patch.object(
                            type(behaviour),
                            "synchronized_data",
                            new_callable=PropertyMock,
                        ) as mock_sd:
                            mock_sd.return_value = MagicMock(is_policy_set=False)

                            gen = behaviour.async_act()
                            try:
                                while True:
                                    next(gen)
                            except StopIteration:
                                pass

        assert len(payloads_sent) >= 1

    def test_async_act_insufficient_balance_no_exchange(self) -> None:
        """When token_balance < investment and cannot exchange, should send None tx."""
        behaviour, bm = _make_behaviour()

        payloads_sent = []

        behaviour.send_a2a_transaction = lambda payload: (  # type: ignore[method-assign]
            payloads_sent.append(payload) or (yield)  # type: ignore[func-returns-value]
        )
        behaviour.wait_until_round_end = lambda: _noop_gen()  # type: ignore[func-returns-value, method-assign]
        behaviour.set_done = MagicMock()  # type: ignore[method-assign]
        behaviour.wallet_balance = 10
        behaviour.token_balance = 5
        behaviour.check_balance = MagicMock()  # type: ignore[method-assign]

        behaviour.wait_for_condition_with_sleep = lambda cond: _noop_gen()  # type: ignore[method-assign]

        with patch.object(
            type(behaviour), "benchmarking_mode", new_callable=PropertyMock
        ) as mock_bm:
            mock_bm.return_value = MagicMock(enabled=False)
            with patch.object(
                type(behaviour), "investment_amount", new_callable=PropertyMock
            ) as mock_inv:
                mock_inv.return_value = 100
                with patch.object(
                    type(behaviour), "is_wxdai", new_callable=PropertyMock
                ) as mock_wxdai:
                    mock_wxdai.return_value = False

                    gen = behaviour.async_act()
                    try:
                        while True:
                            next(gen)
                    except StopIteration:
                        pass

        assert len(payloads_sent) >= 1
        payload = payloads_sent[-1]
        assert payload.tx_submitter is None
        assert payload.tx_hash is None


def _drain(gen: Generator[None, None, Any]) -> Any:
    """Run a behavior generator and return its result."""
    try:
        while True:
            next(gen)
    except StopIteration as exc:
        return exc.value


@pytest.mark.parametrize(
    "p_yes, vote, investment, quoted, floor, proceeds",
    [
        (0.60, 0, 86, 100, 0.0, False),
        (0.90, 0, 86, 100, 0.0, True),
        (0.10, 1, 86, 100, 0.0, True),
        (0.90, 1, 86, 100, 0.0, False),
        (0.75, 0, 50, 100, 0.25, True),
        (0.60, 0, 86, 100, -0.3, True),
        (0.90, 0, 86, 0, 0.0, False),
    ],
)
def test_edge_recheck(
    p_yes: float, vote: int, investment: int, quoted: int, floor: float, proceeds: bool
) -> None:
    """Compare the bought side's probability with the raw quote's price."""
    behaviour, _ = _make_behaviour()
    behaviour.quoted_tokens = quoted
    behaviour.buy_amount = int(quoted * 0.99)
    behaviour.context.params.placement_min_edge = floor
    prediction = dict(p_yes=p_yes, p_no=1 - p_yes, confidence=0.8, info_utility=1.0)
    sd = MagicMock(
        vote=vote,
        bet_amount=investment,
        mech_responses=[MagicMock(result=json.dumps(prediction))],
    )
    bet = MagicMock(id="0xmarket", outcomeTokenMarginalPrices=[0.534, 0.466])
    with ExitStack() as stack:
        for name, value in {
            "sampled_bet": bet,
            "synchronized_data": sd,
            "tx_hex": "0xtx",
        }.items():
            stack.enter_context(
                patch.object(
                    type(behaviour), name, new_callable=PropertyMock, return_value=value
                )
            )
        wait = stack.enter_context(
            patch.object(
                behaviour,
                "wait_for_condition_with_sleep",
                side_effect=lambda step: _noop_gen(),
            )
        )
        stack.enter_context(
            patch.object(
                behaviour, "_calc_buy_amount", side_effect=lambda: _return_gen(True)
            )
        )
        result = _drain(behaviour._prepare_safe_tx())
    assert result == ("0xtx" if proceeds else None)
    assert behaviour.buy_amount == int(quoted * 0.99)
    assert wait.call_count == (4 if proceeds else 1)
    if not proceeds:
        assert any(
            "[EDGE_RECHECK]" in str(c)
            for c in behaviour.context.logger.info.call_args_list
        )


@pytest.mark.parametrize(
    "responses",
    [[], [MagicMock(result=None)], [MagicMock(result="bad")], [MagicMock(result="{}")]],
)
def test_edge_recheck_missing_prediction(responses: list) -> None:
    """Do not place an Omen bet without the decision probability."""
    behaviour, _ = _make_behaviour()
    with (
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=MagicMock(mech_responses=responses),
        ),
        patch.object(
            behaviour,
            "wait_for_condition_with_sleep",
            side_effect=lambda step: _noop_gen(),
        ),
        patch.object(
            behaviour, "_calc_buy_amount", side_effect=lambda: _return_gen(True)
        ),
    ):
        assert _drain(behaviour._prepare_safe_tx()) is None


def test_edge_abort_uses_failed_tx_route() -> None:
    """An edge abort leaves the policy unchanged and reaches blacklisting."""
    from packages.valory.skills.abstract_round_abci.base import AbciAppDB
    from packages.valory.skills.decision_maker_abci.payloads import (
        DecisionReceivePayload,
        HandleFailedTxPayload,
    )
    from packages.valory.skills.decision_maker_abci.rounds import DecisionMakerAbciApp
    from packages.valory.skills.decision_maker_abci.states.base import (
        Event,
        SynchronizedData,
    )
    from packages.valory.skills.decision_maker_abci.states.bet_placement import (
        BetPlacementRound,
    )
    from packages.valory.skills.decision_maker_abci.states.blacklisting import (
        BlacklistingRound,
    )
    from packages.valory.skills.decision_maker_abci.states.decision_receive import (
        DecisionReceiveRound,
    )
    from packages.valory.skills.decision_maker_abci.states.handle_failed_tx import (
        HandleFailedTxRound,
    )

    behaviour, _ = _make_behaviour()
    behaviour.token_balance = 100
    behaviour.wallet_balance = 100
    behaviour.quoted_tokens = 100
    policy = MagicMock()
    sd = SynchronizedData(
        AbciAppDB(
            setup_data=AbciAppDB.data_to_lists(
                {
                    "participants": ("test_agent",),
                    "all_participants": ("test_agent",),
                    "consensus_threshold": 1,
                    "bet_amount": 86,
                    "vote": 0,
                    "confidence": 0.8,
                    "review_bets_for_selling": False,
                    "mech_responses": json.dumps(
                        [
                            {
                                "result": json.dumps(
                                    dict(
                                        p_yes=0.60,
                                        p_no=0.40,
                                        confidence=0.8,
                                        info_utility=1.0,
                                    )
                                )
                            }
                        ]
                    ),
                }
            )
        )
    )
    decision_context = MagicMock()
    decision_context.params.is_running_on_polymarket = False
    decision = DecisionReceiveRound(sd, decision_context)
    decision.process_payload(
        DecisionReceivePayload(
            sender="test_agent",
            bets_hash="bets",
            is_profitable=True,
            vote=0,
            confidence=0.8,
            bet_amount=86,
            next_mock_data_row=None,
            policy=None,
            decision_received_timestamp=100,
            should_be_sold=False,
        )
    )
    decision_result = decision.end_block()
    assert decision_result is not None
    sd, event = decision_result
    assert event == Event.DONE
    assert (
        DecisionMakerAbciApp.transition_function[DecisionReceiveRound][event]
        is BetPlacementRound
    )
    sent = []

    def finish(payload: BetPlacementPayload) -> Generator[None, None, Any]:
        """Capture the payload sent by the behavior."""
        sent.append(payload)
        yield

    with (
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=sd,
        ),
        patch.object(
            SynchronizedData,
            "is_policy_set",
            new_callable=PropertyMock,
            return_value=True,
        ),
        patch.object(
            SynchronizedData, "policy", new_callable=PropertyMock, return_value=policy
        ),
        patch.object(
            type(behaviour),
            "sampled_bet",
            new_callable=PropertyMock,
            return_value=MagicMock(id="0xmarket"),
        ),
        patch.object(
            behaviour,
            "wait_for_condition_with_sleep",
            side_effect=lambda step: _noop_gen(),
        ),
        patch.object(
            behaviour, "_calc_buy_amount", side_effect=lambda: _return_gen(True)
        ),
        patch.object(
            behaviour,
            "finish_behaviour",
            side_effect=finish,
        ),
        patch.object(
            type(behaviour), "is_wxdai", new_callable=PropertyMock, return_value=False
        ),
    ):
        _drain(behaviour.async_act())
    policy.tool_used.assert_not_called()
    assert sent[0].tx_submitter == BetPlacementRound.auto_round_id()
    assert sent[0].tx_hash is None
    placement = BetPlacementRound(sd, MagicMock())
    placement.process_payload(sent[0])
    placement_result = placement.end_block()
    assert placement_result is not None
    updated, event = placement_result
    updated = cast(SynchronizedData, updated)
    assert event == Event.CALC_BUY_AMOUNT_FAILED
    assert (
        DecisionMakerAbciApp.transition_function[BetPlacementRound][event]
        is HandleFailedTxRound
    )
    assert updated.mech_responses[0].result == sd.mech_responses[0].result
    failed = HandleFailedTxRound(updated, MagicMock())
    failed.process_payload(
        HandleFailedTxPayload("test_agent", True, sent[0].tx_submitter)
    )
    failed_result = failed.end_block()
    assert failed_result is not None
    _, event = failed_result
    assert event == Event.BLACKLIST
    assert (
        DecisionMakerAbciApp.transition_function[HandleFailedTxRound][event]
        is BlacklistingRound
    )


@pytest.mark.parametrize("polymarket, benchmarking", [(True, False), (False, True)])
def test_placement_guard_bypassed(polymarket: bool, benchmarking: bool) -> None:
    """Keep the placement guard off for Polymarket and benchmarking."""
    behaviour, _ = _make_behaviour()
    behaviour.context.params.is_running_on_polymarket = polymarket
    behaviour.context.benchmarking_mode.enabled = benchmarking
    with (
        patch.object(behaviour, "_get_decision_prediction") as prediction,
        patch.object(
            behaviour,
            "wait_for_condition_with_sleep",
            side_effect=lambda step: _noop_gen(),
        ),
        patch.object(
            behaviour, "_calc_buy_amount", side_effect=lambda: _return_gen(True)
        ),
        patch.object(type(behaviour), "sampled_bet", new_callable=PropertyMock),
        patch.object(
            type(behaviour),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=MagicMock(bet_amount=100, vote=0),
        ),
        patch.object(
            type(behaviour), "tx_hex", new_callable=PropertyMock, return_value="0xtx"
        ),
    ):
        assert _drain(behaviour._prepare_safe_tx()) == "0xtx"
        prediction.assert_not_called()
