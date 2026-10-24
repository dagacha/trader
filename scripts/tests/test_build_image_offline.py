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

"""Check offline image tags without a Docker build."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("name", ["trader", "omenstrat_fast"])
def test_image_tag_uses_agent_name_and_hash(name):
    """Print the image tag for each agent without fetching or building it."""
    repo = Path(__file__).resolve().parents[2]
    agent_hash = "bafybeiaknbpkhjscvux5ikkskb4lstqu7cl44mi6affkzj6qqdga5io6bm"
    result = subprocess.run(
        [
            "bash",
            str(repo / "build_image_offline.sh"),
            "--print-tag",
            str(repo),
            f"valory/{name}:0.1.0:{agent_hash}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"valory/oar-{name}:{agent_hash}\n"
