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

"""Recheck raw pool prices before a fast bet can be placed."""

import json
from dataclasses import asdict, replace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.protocols.contract_api.message import ContractApiMessage
from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import BetPlacementPayload
from packages.valory.skills.fresh_watch_abci.behaviours import FastBetPlacementBehaviour
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, decide
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.market_manager_abci.bets import Bet


@pytest.mark.parametrize(
    "case,side,requested,live,reason",
    [
        ("still_hold", 0, 0.70, 0.45, "still_hold_price_floor"),
        ("ordinary", 1, 0.70, 0.45, "price_dropped"),
        ("ordinary", 0, 0.45, 0.45, ""),
        ("normal", 0, 0.50, 0.50, ""),
        ("disabled", 0, 0.70, 0.45, ""),
        ("still_hold", 0, 0.70, 0.60, ""),
        ("still_hold", 0, 0.70, 0.60 - 1e-13, "still_hold_price_floor"),
        ("ordinary", 0, 0.70, 0.55, ""),
        ("ordinary", 0, 0.70, 0.55 - 5e-13, ""),
        ("ordinary", 0, 0.70, 0.55 - 2e-12, "price_dropped"),
        ("noncanonical", 0, 0.45, 0.45, ""),
        ("wrong_template", 0, 0.45, 0.45, ""),
        ("wrong_creator", 0, 0.45, 0.45, ""),
        ("other_title", 0, 0.45, 0.45, ""),
        ("max_price", 0, 0.70, 0.45, "max_price"),
        ("drop_before_slip", 0, 0.50, 0.30, "price_dropped"),
    ],
)
def test_final_raw_price_guards(
    tmp_path: Any, case: str, side: int, requested: float, live: float, reason: str
) -> None:
    """Use live reserves, preserve guard order, and log each rejected buy."""
    cfg = replace(FastConfig(), prior_weight=1)
    if case == "disabled":
        cfg = replace(cfg, still_hold_min_price=0, max_price_drop=1)
    raw = raw_market()
    raw.pop("condition")
    raw.pop("question")
    raw["condition_id"] = "condition"
    if case == "noncanonical":
        raw["outcomes"] = ["No", "Yes"]
    if case == "wrong_creator":
        raw["creator"] = "other"
    if case == "other_title":
        raw["title"] = "Will it remain?"
    bet = Bet(**raw, market="omen_subgraph")
    yes_only = case in {"still_hold", "disabled", "max_price"}
    prices = [requested, 1 - requested]
    if side:
        prices.reverse()
    p_hat = 0.6 if case == "normal" else 0.95 if side == 0 else 0.05
    decision_price = 0.65 if requested == 0.70 else requested
    decision_balances = [1 - decision_price, decision_price]
    if side:
        decision_balances.reverse()
    decision = decide(
        bet.title,
        p_hat,
        [int(value * 10**27) for value in decision_balances],
        prices,
        cfg,
        0,
        yes_only=yes_only,
    )
    assert decision.reason == ""
    assert decision.side == side
    assert decision.stake > 0
    balances = [int((1 - live) * 10**27), int(live * 10**27)]
    if side:
        balances.reverse()
    live_decision = decide(bet.title, p_hat, balances, prices, cfg, 0, yes_only)
    if reason in {"still_hold_price_floor", "price_dropped"}:
        assert live_decision.reason == reason
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.fast = cfg
    b.context.params.slippage = 0.01
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=case not in {"ordinary", "normal", "drop_before_slip"}
    )
    quote_price = (
        0.95 if case == "max_price" else 0.8 if case == "drop_before_slip" else 0.5
    )
    b.get_contract_api_response = MagicMock(
        side_effect=lambda **kwargs: done(
            _response(kwargs["contract_callable"], balances, int(50 / quote_price))
        )
    )
    values = dict(
        request_prices=prices,
        fast_decision=json.dumps(asdict(decision)),
        question_template_id=0 if case == "wrong_template" else 2,
    )
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch.object(
            BetPlacementBehaviour, "finish_behaviour", return_value=done(None)
        ),
    ):
        sd.return_value.vote = side
        sd.return_value.bet_amount = 50
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        sd.return_value.db.get.side_effect = values.get
        assert run(b._calc_buy_amount()) is (not reason)
        if reason:
            assert b._placement_failure == reason
            assert b.buy_amount == 0
            assert b._min_tokens_out is None
            run(b.finish_behaviour(BetPlacementPayload("agent", "placement")))
            log = b.context.logger.info.call_args.args
            assert log[0] == "[FAST_DECISION] %s"
            assert json.loads(log[1])["reason"] == reason
        else:
            assert b.buy_amount > 0
            if case == "normal":
                assert b.buy_amount == 88
    calls = [
        call.kwargs["contract_callable"]
        for call in b.get_contract_api_response.call_args_list
    ]
    assert calls == ["calc_buy_amount"] + (
        [] if case == "max_price" else ["get_pool_balances_via_ct"]
    )


def _response(method: str, balances: Any, tokens: int = 100) -> Any:
    """Return the quote or the later raw reserve read."""
    response = MagicMock()
    if method == "calc_buy_amount":
        response.performative = ContractApiMessage.Performative.RAW_TRANSACTION
        response.raw_transaction.body = {"amount": tokens}
    else:
        assert method == "get_pool_balances_via_ct"
        response.performative = (
            ContractApiMessage.Performative.ERROR
            if balances is None
            else ContractApiMessage.Performative.STATE
        )
        response.state.body = {"balances": balances}
    return response


@pytest.mark.parametrize(
    "balances", [None, [], [1], [0, 0], [0, 100], [100, 0], [-1, 100], ["bad", 100]]
)
def test_final_reserves_fail_closed(tmp_path: Any, balances: Any) -> None:
    """Abort preparation without a buy or retry when reserves are unusable."""
    b = make(FastBetPlacementBehaviour, tmp_path)
    b.context.params.slippage = 0.01
    b.get_contract_api_response = MagicMock(
        side_effect=lambda **kwargs: done(
            _response(kwargs["contract_callable"], balances)
        )
    )
    raw = raw_market()
    raw.pop("condition")
    raw.pop("question")
    bet = Bet(**raw, condition_id="condition", market="omen_subgraph")
    b.wait_for_condition_with_sleep = lambda *args: done(None)
    b._build_buy_tx = MagicMock(side_effect=AssertionError("Buy must not be built."))
    with (
        patch.object(
            type(b), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
    ):
        sd.return_value.vote = 0
        sd.return_value.bet_amount = 50
        values = dict(
            request_prices=[0.5, 0.5], fast_decision=json.dumps({"p_hat": 0.9})
        )
        sd.return_value.db.get_strict.side_effect = values.__getitem__
        assert run(b._prepare_safe_tx()) is None
    assert b._placement_failure == "quote"
    assert b.buy_amount == 0
    assert b._min_tokens_out is None
    b._build_buy_tx.assert_not_called()
    assert b.get_contract_api_response.call_count == 2
