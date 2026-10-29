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

"""Check the lightning deployment defaults and local release entry point."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTROLS = {
    "lightning_still_hold": ("bool", False),
    "lightning_still_hold_max_price": ("float", 0.93),
    "lightning_base_rate_window_days": ("int", 21),
    "lightning_base_rate_min_n": ("int", 60),
    "lightning_base_rate_floor": ("float", 0.90),
    "still_hold_prior": ("float", 0.85),
    "lightning_chain_detect": ("bool", False),
    "omen_fpmm_factory": ("str", "0x9083A2B699c0a4AD06F63580BDE2635d26a3eeF0"),
    "lightning_rpc_fallback_s": ("int", 60),
    "lightning_preview": ("bool", False),
    "lightning_preview_max_per_night": ("int", 15),
    "lightning_creators": (
        "list",
        [
            "0x89c5cc945dd550bcffb72fe42bff002429f46fec",
            "0xffc8029154ecd55abed15bd428ba596e7d23f557",
        ],
    ),
}


def params(relative):
    """Read the fast model defaults or deployment override."""
    docs = list(yaml.safe_load_all((ROOT / "packages/valory" / relative).read_text()))
    config = (
        docs[0]
        if relative.startswith("skills/")
        else next(
            doc
            for doc in docs[1:]
            if doc["public_id"].startswith("valory/omenstrat_fast_abci:")
        )
    )
    return config["models"]["params"]["args"]


@pytest.mark.parametrize(
    "relative",
    [
        "skills/fresh_watch_abci/skill.yaml",
        "skills/omenstrat_fast_abci/skill.yaml",
        "agents/omenstrat_fast/aea-config.yaml",
        "services/omenstrat_fast/service.yaml",
        "agents/omenstrat_lightning/aea-config.yaml",
        "services/omenstrat_lightning/service.yaml",
    ],
)
def test_lightning_controls_are_declared(relative):
    """Enable lightning only in its own service and agent."""
    args = params(relative)
    lightning = "omenstrat_lightning/" in relative
    for key, (type_, default) in CONTROLS.items():
        if lightning:
            if key in (
                "lightning_still_hold",
                "lightning_chain_detect",
                "lightning_preview",
            ):
                default = True
            if key == "still_hold_prior":
                default = 0.95
        if relative.startswith("skills/"):
            assert args[key] == default
        else:
            value = (
                json.dumps(default, separators=(",", ":"))
                if type_ != "str"
                else default
            )
            assert args[key] == "${" + key.upper() + ":" + type_ + ":" + value + "}"


def test_lightning_quickstart_enables_controls():
    """Ship a complete lightning template that uses the new service."""
    config = json.loads((ROOT / "configs/config_omenstrat_lightning.json").read_text())
    assert config["name"] == "omenstrat_lightning"
    env = config["env_variables"]
    for key, (_, default) in CONTROLS.items():
        if key in (
            "lightning_still_hold",
            "lightning_chain_detect",
            "lightning_preview",
        ):
            default = True
        if key == "still_hold_prior":
            default = 0.95
        raw = env[key.upper()]["value"]
        assert (raw if isinstance(default, str) else json.loads(raw)) == default
    assert env["SERVICE_ID"]["value"] == "omenstrat_lightning"
    assert float(env["MAX_PRICE"]["value"]) == 0.93
    service = yaml.safe_load_all(
        (ROOT / "packages/valory/services/omenstrat_lightning/service.yaml").read_text()
    )
    assert next(service)["agent"].startswith("valory/omenstrat_lightning:")


def test_release_accepts_lightning_without_running_release():
    """Accept the service name before a deliberate local parser stop."""
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "release_service.sh"),
            "--service",
            "omenstrat_lightning",
            "--lightning-test-stop",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "ERROR: unknown argument: --lightning-test-stop"
