# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2025-2026 Valory AG
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

"""This module contains the models for the 'funds_manager' skill."""

from typing import Any, Dict, ItemsView, Iterator, Optional

from aea.exceptions import enforce
from aea.skills.base import Model
from pydantic import (  # type: ignore[import-not-found]
    BaseModel,
    RootModel,
    ValidationError,
)

from packages.valory.skills.abstract_round_abci.utils import check_type

NATIVE_ADDRESSES = [
    "0x0000000000000000000000000000000000000000",
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
]

AGENT_ACCOUNT_NAME = "agent"
SAFE_ACCOUNT_NAME = "safe"
ACCOUNTS = frozenset({AGENT_ACCOUNT_NAME, SAFE_ACCOUNT_NAME})

BALANCE_KEY = "balance"
DEFICIT_KEY = "deficit"

DEFAULT_RPC_TIMEOUT_SECONDS = 10
DEFAULT_RPC_MAX_RETRIES = 2


class TokenRequirement(BaseModel):
    """Balance requirements for a specific token in an account."""

    topup: int
    threshold: int
    is_native: bool
    balance: Optional[int] = None
    deficit: Optional[int] = None
    decimals: Optional[int] = None


class AccountRequirements(BaseModel):
    """All token requirements for a single account address."""

    tokens: Dict[str, TokenRequirement] = {}


class ChainRequirements(BaseModel):
    """All account requirements for a single chain."""

    accounts: Dict[str, AccountRequirements] = {}


class FundRequirements(RootModel[Dict[str, ChainRequirements]]):
    """All fund requirements for all accounts."""

    def __getitem__(self, key: str) -> ChainRequirements:
        """Get chain requirements by chain name."""
        return self.root[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the chain names."""
        return iter(self.root)

    def items(self) -> ItemsView[str, ChainRequirements]:
        """Get the items view of the root dictionary."""
        return self.root.items()

    @classmethod
    def build_account_requirements(
        cls,
        tokens: Dict[str, Dict[str, int]],
    ) -> AccountRequirements:
        """Build an AccountRequirements from a {token_address: token_data} mapping."""
        token_objs = {}
        for token_address, token_data in tokens.items():
            is_native = token_address.lower() in NATIVE_ADDRESSES
            token_objs[token_address] = TokenRequirement(
                **token_data,
                is_native=is_native,
            )
        return AccountRequirements(tokens=token_objs)

    @classmethod
    def build_chain_requirements(
        cls,
        accounts: Dict[str, Dict[str, Dict[str, int]]],
    ) -> ChainRequirements:
        """Get the chain requirements for a specific chain."""
        chain_obj = {}
        for account_name, tokens in accounts.items():
            chain_obj[account_name] = cls.build_account_requirements(tokens)
        return ChainRequirements(accounts=chain_obj)

    @classmethod
    def from_dict(cls, fund_dict: Dict[str, Any]) -> "FundRequirements":
        """Create 'FundRequirements' from a dictionary."""
        if not fund_dict:
            raise ValueError("Fund requirements cannot be empty.")
        fund_requirements = {}
        validation_errors = []
        for chain, accounts in fund_dict.items():
            if accounts.keys() != ACCOUNTS:
                validation_errors.append(
                    f"{chain} chain can only have accounts {list(ACCOUNTS)}, got {list(accounts.keys())}."
                )
                continue
            try:
                fund_requirements[chain] = cls.build_chain_requirements(accounts)
            except ValidationError as e:
                validation_errors.append(f"Error in {chain} chain: {e}")
                continue

        if validation_errors:
            raise ValueError(" ".join(validation_errors))
        return cls(**fund_requirements)

    def get_response_body(self) -> Dict[str, Any]:
        """Convert to dict with flattened accounts/tokens and stringified balances/deficits.

        Balance and deficit are stringified to preserve precision for large
        token amounts. None is preserved for tokens whose balance is unknown
        (sub-call reverted, decode failed, or the whole-chain multicall failed).

        :return: the flattened, JSON-serializable response body.
        """
        raw = self.model_dump(
            exclude={
                "__all__": {
                    "accounts": {
                        "__all__": {
                            "tokens": {"__all__": {"topup", "threshold", "is_native"}}
                        }
                    }
                }
            }
        )

        def flatten(obj: Any) -> Any:
            if not isinstance(obj, dict):
                return obj

            new_obj = {}
            for k, v in obj.items():
                if isinstance(v, dict):
                    # flatten accounts/tokens layers
                    if "accounts" in v:
                        new_obj[k] = flatten(v["accounts"])
                    elif "tokens" in v:
                        new_obj[k] = flatten(v["tokens"])
                    else:
                        new_obj[k] = flatten(v)
                elif k in {BALANCE_KEY, DEFICIT_KEY} and isinstance(v, int):
                    new_obj[k] = str(v)
                else:
                    new_obj[k] = v
            return new_obj

        return flatten(raw)


class Params(Model):
    """Parameters."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the parameters' object."""
        self.rpc_urls: Dict[str, str] = self._ensure_get(
            "rpc_urls", kwargs, Dict[str, str]
        )
        self.safe_contract_addresses: Dict[str, str] = self._ensure_get(
            "safe_contract_addresses", kwargs, Dict[str, str]
        )
        self.fund_requirements: FundRequirements = FundRequirements.from_dict(
            self._ensure_get("fund_requirements", kwargs, Dict[str, Any])
        )
        self.rpc_timeout_seconds: int = kwargs.get(
            "rpc_timeout_seconds", DEFAULT_RPC_TIMEOUT_SECONDS
        )
        self.rpc_max_retries: int = kwargs.get(
            "rpc_max_retries", DEFAULT_RPC_MAX_RETRIES
        )

        self._validate_chain_keys()

        super().__init__(*args, **kwargs)

    def _validate_chain_keys(self) -> None:
        """Reject configs where a chain in fund_requirements has no rpc_url or safe."""
        fund_chains = set(self.fund_requirements.root.keys())
        missing_rpc = fund_chains - set(self.rpc_urls.keys())
        missing_safe = fund_chains - set(self.safe_contract_addresses.keys())
        errors = []
        if missing_rpc:
            errors.append(
                f"rpc_urls is missing chains declared in fund_requirements: {sorted(missing_rpc)}"
            )
        if missing_safe:
            errors.append(
                f"safe_contract_addresses is missing chains declared in fund_requirements: {sorted(missing_safe)}"
            )
        if errors:
            raise ValueError(" ".join(errors))

    @classmethod
    def _ensure_get(cls, key: str, kwargs: Dict, type_: Any) -> Any:
        """Ensure that the parameters are set, and return them without popping the key."""
        enforce("skill_context" in kwargs, "Only use on models!")
        skill_id = kwargs["skill_context"].skill_id
        enforce(
            key in kwargs,
            f"{key!r} of type {type_!r} required, but it is not set in `models.params.args` of `skill.yaml` of `{skill_id}`",
        )
        value = kwargs.get(key, None)
        try:
            check_type(key, value, type_)
        except TypeError:  # pragma: nocover
            enforce(
                False,
                f"{key!r} must be a {type_}, but type {type(value)} was found in `models.params.args` of `skill.yaml` of `{skill_id}`",
            )
        return value
