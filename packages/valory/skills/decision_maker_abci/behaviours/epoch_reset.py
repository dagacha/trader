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

"""Behaviour that resets the trade cap when a new staking epoch starts."""

import json
from typing import Generator

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import EpochResetPayload
from packages.valory.skills.decision_maker_abci.states.epoch_reset import (
    EpochResetRound,
)


class EpochResetBehaviour(DecisionMakerBaseBehaviour):
    """Reset the trade counter and queue at a new staking epoch boundary."""

    matching_round = EpochResetRound

    def async_act(self) -> Generator:
        """Reset the trade counter and clear the Mech-only queue at the epoch boundary."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            # The durable reader resets only a dated counter from another period.
            # A checkpoint flag can also result from a stale file after restart.
            count = self.durable_trade_count()
            queue = self.synchronized_data.mech_only_queue
            if self.synchronized_data.is_checkpoint_reached:
                queue = []
                self.context.logger.info(
                    "Staking checkpoint detected; clearing the Mech-only queue."
                )
            payload = EpochResetPayload(
                self.context.agent_address,
                successful_trade_count=count,
                mech_only_queue=json.dumps(queue),
            )
        yield from self.finish_behaviour(payload)
