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

"""Compose the fast trading lane and the periodic housekeeping lane."""

from enum import Enum
from typing import Optional, Set, Tuple

from packages.valory.skills.abstract_round_abci.abci_app_chain import (
    AbciAppTransitionMapping,
    chain,
)
from packages.valory.skills.abstract_round_abci.base import (
    AppState,
    BaseSynchronizedData,
)
from packages.valory.skills.agent_performance_summary_abci.rounds import (
    AgentPerformanceSummaryAbciApp,
    FetchPerformanceDataRound,
    FinishedFetchPerformanceDataRound,
)
from packages.valory.skills.chatui_abci.rounds import (
    ChatuiAbciApp,
    ChatuiLoadRound,
    FinishedChatuiLoadRound,
)
from packages.valory.skills.fresh_watch_abci.rounds import (
    FastDecisionRound,
    FinishedFastTxRound,
    FinishedHousekeepingRound,
    FinishedRequestRound,
    FreshWatchAbciApp,
    FreshWatchRound,
    PostBetUpdateRound,
)
from packages.valory.skills.mech_interact_abci.rounds import MechInteractAbciApp
from packages.valory.skills.mech_interact_abci.states import final_states as mech
from packages.valory.skills.mech_interact_abci.states.mech_info import (
    MechInformationRound,
)
from packages.valory.skills.mech_interact_abci.states.purchase_subscription import (
    MechPurchaseSubscriptionRound,
)
from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound
from packages.valory.skills.mech_interact_abci.states.response import MechResponseRound
from packages.valory.skills.registration_abci.rounds import (
    AgentRegistrationAbciApp,
    FinishedRegistrationRound,
)
from packages.valory.skills.staking_abci.rounds import (
    CallCheckpointRound,
    CheckpointCallPreparedRound,
    FinishedStakingRound,
    ServiceEvictedRound,
    StakingAbciApp,
)
from packages.valory.skills.trader_abci.composition import termination_config
from packages.valory.skills.transaction_settlement_abci.rounds import (
    FailedRound,
    FinishedTransactionSubmissionRound,
    RandomnessTransactionSubmissionRound,
    TransactionSubmissionAbciApp,
)
from packages.valory.skills.tx_settlement_multiplexer_abci import rounds as tx

_MECH_ROUNDS: Set[AppState] = {
    MechInformationRound,
    MechRequestRound,
    MechResponseRound,
    MechPurchaseSubscriptionRound,
}
_MECH_FINALS: Set[AppState] = {
    target
    for source in _MECH_ROUNDS
    for target in MechInteractAbciApp.transition_function[source].values()
} - _MECH_ROUNDS


class FastMechInteractAbciApp(MechInteractAbciApp):
    """Enter mech discovery once, without version detection."""

    initial_round_cls = MechInformationRound
    initial_states = _MECH_ROUNDS - {MechPurchaseSubscriptionRound}
    final_states = _MECH_FINALS
    transition_function = {
        state: MechInteractAbciApp.transition_function[state]
        for state in _MECH_ROUNDS | _MECH_FINALS
    }
    db_post_conditions = {
        state: MechInteractAbciApp.db_post_conditions[state] for state in _MECH_FINALS
    }
    db_pre_conditions = {
        **MechInteractAbciApp.db_pre_conditions,
        MechInformationRound: set(),
    }


class FastPreTxSettlementRound(tx.PreTxSettlementRound):
    """Clear completed transaction state without a reset round."""

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Keep the prepared transaction and clear the previous attempt's state."""
        result = super().end_block()
        if result is None:
            return None
        data, event = result
        if event == tx.Event.CHECKS_PASSED:
            data = data.update(
                tx_hashes_history="",
                blacklisted_keepers="",
                keepers="",
                final_verification_status=None,
                final_tx_hash=None,
                missed_messages=dict.fromkeys(data.all_participants, 0),
                late_arriving_tx_hashes="{}",
                suspects=(),
            )
        return data, event


class FastTxSettlementMultiplexerAbciApp(tx.TxSettlementMultiplexerAbciApp):
    """Use a fresh transaction context for each settlement entry."""

    initial_round_cls = FastPreTxSettlementRound
    initial_states = {FastPreTxSettlementRound, tx.PostTxSettlementRound}
    transition_function = {
        (FastPreTxSettlementRound if state == tx.PreTxSettlementRound else state): {
            event: (
                FastPreTxSettlementRound
                if target == tx.PreTxSettlementRound
                else target
            )
            for event, target in transitions.items()
        }
        for state, transitions in tx.TxSettlementMultiplexerAbciApp.transition_function.items()
    }
    db_pre_conditions = {
        FastPreTxSettlementRound: {"tx_submitter"},
        tx.PostTxSettlementRound: {"tx_submitter"},
    }


abci_app_transition_mapping: AbciAppTransitionMapping = {
    FinishedRegistrationRound: MechInformationRound,
    mech.FinishedMechInformationRound: FreshWatchRound,
    mech.FailedMechInformationRound: MechInformationRound,
    FinishedRequestRound: MechRequestRound,
    mech.FinishedOffchainMechRequestRound: MechResponseRound,
    mech.FinishedMechRequestRound: FastPreTxSettlementRound,
    mech.FinishedMechPurchaseSubscriptionRound: FastPreTxSettlementRound,
    tx.FinishedSubscriptionTxRound: FreshWatchRound,
    mech.FinishedOffchainMechDepositNeededRound: FastPreTxSettlementRound,
    tx.FinishedOffchainMechDepositSettledRound: MechRequestRound,
    tx.FinishedMechRequestTxRound: MechResponseRound,
    mech.FailedOffchainMechRequestRound: FastDecisionRound,
    mech.FinishedMechRequestSkipRound: FastDecisionRound,
    mech.FinishedMechResponseRound: FastDecisionRound,
    mech.FinishedMechResponseTimeoutRound: FastDecisionRound,
    FinishedFastTxRound: FastPreTxSettlementRound,
    tx.ChecksPassedRound: RandomnessTransactionSubmissionRound,
    FinishedTransactionSubmissionRound: tx.PostTxSettlementRound,
    FailedRound: FreshWatchRound,
    tx.FailedMultiplexerRound: FreshWatchRound,
    tx.FinishedBetPlacementTxRound: PostBetUpdateRound,
    tx.FinishedRedeemingTxRound: CallCheckpointRound,
    FinishedHousekeepingRound: CallCheckpointRound,
    CheckpointCallPreparedRound: FastPreTxSettlementRound,
    tx.FinishedStakingTxRound: FetchPerformanceDataRound,
    FinishedStakingRound: FetchPerformanceDataRound,
    ServiceEvictedRound: FetchPerformanceDataRound,
    FinishedFetchPerformanceDataRound: ChatuiLoadRound,
    FinishedChatuiLoadRound: FreshWatchRound,
}

OmenstratFastAbciApp = chain(
    (
        AgentRegistrationAbciApp,
        FreshWatchAbciApp,
        FastMechInteractAbciApp,
        TransactionSubmissionAbciApp,
        FastTxSettlementMultiplexerAbciApp,
        StakingAbciApp,
        AgentPerformanceSummaryAbciApp,
        ChatuiAbciApp,
    ),
    abci_app_transition_mapping,
).add_background_app(termination_config)
