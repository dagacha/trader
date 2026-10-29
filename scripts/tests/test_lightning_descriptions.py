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

"""Check user help for each optional lightning control."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["omenstrat_fast", "omenstrat_lightning"])
def test_lightning_controls_have_help(name):
    """Explain each control in one short sentence in both quickstart templates."""
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / "configs" / f"config_{name}.json").read_text())
    for key, entry in config["env_variables"].items():
        if key.startswith("LIGHTNING_") or key in (
            "STILL_HOLD_PRIOR",
            "OMEN_FPMM_FACTORY",
        ):
            description = entry["description"]
            assert 0 < len(description) <= 160, key
            assert description.endswith("."), key
