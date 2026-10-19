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


"""Regression test: every skill declares the AEA packages its modules import.

open-aea runs this check when it loads each component at agent start
(``aea.components.base._CheckUsedDependencies``). It scans every ``.py``
file of the package, tests included. An import of a package that the
skill's ``skill.yaml`` does not declare makes the agent fail at startup,
while the unit tests and ``aea build`` still pass.
"""

from pathlib import Path
from typing import List

import pytest
from aea.components.base import _CheckUsedDependencies
from aea.configurations.base import ComponentType
from aea.configurations.loader import load_component_configuration

# packages/valory/skills/trader_abci/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[4]


def _skill_dirs() -> List[Path]:
    """Return the directory of every skill under ``packages/*/skills/``.

    :return: the skill directories, sorted.
    """
    return sorted(path.parent for path in _PACKAGES_DIR.glob("*/skills/*/skill.yaml"))


@pytest.mark.parametrize(
    "skill_dir", _skill_dirs(), ids=lambda path: f"{path.parents[1].name}/{path.name}"
)
def test_skill_imports_are_declared_dependencies(skill_dir: Path) -> None:
    """Run open-aea's own load-time dependency check on the skill.

    :param skill_dir: the skill package directory.
    """
    configuration = load_component_configuration(
        ComponentType.SKILL, skill_dir, skip_consistency_check=True
    )
    configuration.directory = skill_dir
    _CheckUsedDependencies(configuration).run_check()
