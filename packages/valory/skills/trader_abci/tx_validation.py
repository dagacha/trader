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

"""Read raw receipts before validating trader transactions."""

from typing import Generator

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.transaction_settlement_abci.behaviours import (
    ValidateTransactionBehaviour,
)


class TraderValidateTransactionBehaviour(ValidateTransactionBehaviour):
    """Validate receipts without replaying reverted transactions."""

    def has_transaction_been_sent(self) -> Generator[None, None, bool]:
        """Wait for a receipt, reject a revert, and verify a successful Safe call."""
        tx_hash = self.synchronized_data.to_be_validated_tx_hash
        chain_id = self.synchronized_data.get_chain_id(self.params.default_chain_id)
        # Let the round timeout select the transaction-history check.
        while True:
            # GET_STATE calls Web3 directly. The enriched receipt API can discard
            # a mined revert when its replay at the parent block succeeds.
            response = yield from self.get_ledger_api_response(
                performative=LedgerApiMessage.Performative.GET_STATE,  # type: ignore[arg-type]
                ledger_callable="get_transaction_receipt",
                transaction_hash=tx_hash,
                chain_id=chain_id,
            )
            if response.performative == LedgerApiMessage.Performative.STATE:
                status = response.state.body.get("status")
                if status == 0:
                    self.context.logger.warning(f"Transaction {tx_hash} reverted.")
                    return False
                if status == 1:
                    verification = yield from self._verify_tx(tx_hash)
                    if (
                        verification.performative
                        != ContractApiMessage.Performative.STATE
                    ):
                        self.context.logger.error(
                            f"verify_tx unsuccessful! Received: {verification}"
                        )
                        return False
                    return bool(verification.state.body["verified"])
            yield from self.sleep(self.params.retry_timeout)
