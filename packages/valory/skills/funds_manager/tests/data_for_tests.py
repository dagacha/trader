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

"""This module contains test data for the funds manager skill."""

MOCK_AGENT_ADDRESS = "0x1111111111111111111111111111111111111111"
MOCK_SAFE_ADDRESS = "0x2222222222222222222222222222222222222222"
MOCK_SAFE_ADDRESS_CELO = "0x3333333333333333333333333333333333333333"


MOCK_RPC_URLS = {
    "optimism": "https://mock-rpc.com/optimism",
    "base": "https://mock-rpc.com/base",
    "mode": "https://mock-rpc.com/mode",
    "gnosis": "https://mock-rpc.com/gnosis",
}

OPTIMUS_INITIAL_FUND_REQUIREMENTS = {
    "optimism": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 5000000000000000,
                "threshold": 2500000000000000,
            },
            "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85": {"topup": 0, "threshold": 0},
        },
        "safe": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 15000000000000000,
                "threshold": 7500000000000000,
            },
            "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85": {
                "topup": 40000000,
                "threshold": 20000000,
            },
        },
    },
    "base": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 5000000000000000,
                "threshold": 2500000000000000,
            },
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": {"topup": 0, "threshold": 0},
        },
        "safe": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 15000000000000000,
                "threshold": 7500000000000000,
            },
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": {
                "topup": 40000000,
                "threshold": 20000000,
            },
        },
    },
    "mode": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 5000000000000000,
                "threshold": 2500000000000000,
            },
            "0xd988097fb8612cc24eeC14542bC03424c656005f": {"topup": 0, "threshold": 0},
        },
        "safe": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 15000000000000000,
                "threshold": 7500000000000000,
            },
            "0xd988097fb8612cc24eeC14542bC03424c656005f": {
                "topup": 40000000,
                "threshold": 20000000,
            },
        },
    },
}


OPTIMUS_MULTICALL_RETURN_VALUES = [
    # optimism - agent-eth, agent-usdc, safe-eth, safe-usdc, usdc decimals
    [0, 2500000000000000, 0, 0, 6],
    # base - agent-eth, agent-usdc, safe-eth, safe-usdc, usdc decimals
    [0, 0, 2500000000000000, 0, 6],
    # mode - agent-eth, agent-usdc, safe-eth, safe-usdc, usdc decimals
    [0, 0, 0, 0, 6],
]


OPTIMUS_FUNDS_RESPONSE = {
    "optimism": {
        "0x1111111111111111111111111111111111111111": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "0",
                "deficit": "5000000000000000",
                "decimals": 18,
            },
            "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85": {
                "balance": "2500000000000000",
                "deficit": "0",
                "decimals": 6,
            },
        },
        "0x2222222222222222222222222222222222222222": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "0",
                "deficit": "15000000000000000",
                "decimals": 18,
            },
            "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85": {
                "balance": "0",
                "deficit": "40000000",
                "decimals": 6,
            },
        },
    },
    "base": {
        "0x1111111111111111111111111111111111111111": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "0",
                "deficit": "5000000000000000",
                "decimals": 18,
            },
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": {
                "balance": "0",
                "deficit": "0",
                "decimals": 6,
            },
        },
        "0x2222222222222222222222222222222222222222": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "2500000000000000",
                "deficit": "12500000000000000",
                "decimals": 18,
            },
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913": {
                "balance": "0",
                "deficit": "40000000",
                "decimals": 6,
            },
        },
    },
    "mode": {
        "0x1111111111111111111111111111111111111111": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "0",
                "deficit": "5000000000000000",
                "decimals": 18,
            },
            "0xd988097fb8612cc24eeC14542bC03424c656005f": {
                "balance": "0",
                "deficit": "0",
                "decimals": 6,
            },
        },
        "0x2222222222222222222222222222222222222222": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "0",
                "deficit": "15000000000000000",
                "decimals": 18,
            },
            "0xd988097fb8612cc24eeC14542bC03424c656005f": {
                "balance": "0",
                "deficit": "40000000",
                "decimals": 6,
            },
        },
    },
}


TRADER_INITIAL_FUND_REQUIREMENTS = {
    "gnosis": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 100000000000000000,
                "threshold": 50000000000000000,
            }
        },
        "safe": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 5000000000000000000,
                "threshold": 2500000000000000000,
            }
        },
    }
}

TRADER_MULTICALL_RETURN_VALUES = [
    # gnosis - agent-eth balance, safe-eth balance (no decimals call: both tokens are native)
    [50000000000000000 - 1, 2500000000000000000]
]


TRADER_FUNDS_RESPONSE = {
    "gnosis": {
        "0x1111111111111111111111111111111111111111": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "49999999999999999",
                "deficit": "50000000000000001",
                "decimals": 18,
            }
        },
        "0x2222222222222222222222222222222222222222": {
            "0x0000000000000000000000000000000000000000": {
                "balance": "2500000000000000000",
                "deficit": "0",
                "decimals": 18,
            }
        },
    }
}


INVALID_TRADER_FUND_REQUIREMENTS = {
    "gnosis": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 100000000000000000,
                "threshold": 50000000000000000,
            }
        },
        "wrong_safe": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 5000000000000000000,
                "threshold": 2500000000000000000,
            }
        },
    }
}

INVALID_TRADER_TOKEN_REQUIREMENTS = {
    "gnosis": {
        "agent": {
            "0x0000000000000000000000000000000000000000": {
                "topup": 100000000000000000,
                "threshold": 50000000000000000,
            }
        },
        "safe": {
            "0x0000000000000000000000000000000000000000": {
                "wrong_topup": 5000000000000000000,
                "threshold": 2500000000000000000,
            }
        },
    }
}
