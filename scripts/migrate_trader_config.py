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

"""Migrate off-chain settings and Omen strategy defaults during a release.

Use only with an Omen quickstart and Omen cached services. The profit threshold
uses 18-decimal xDAI units. Do not run against Polymarket or mixed-service caches.
"""

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

KEYS = {
    "use_offchain": ("USE_OFFCHAIN", "Use offchain"),
    "offchain_deposit_target_calls": (
        "OFFCHAIN_DEPOSIT_TARGET_CALLS",
        "Offchain deposit target calls",
    ),
}
MARKETPLACE = "MECH_MARKETPLACE_CONFIG"
STRATEGIES = "STRATEGIES_KWARGS"
STRATEGY_HASHES = "FILE_HASH_TO_STRATEGIES"
MIN_EXPECTED_PROFIT = 60000000000000000
DECODER = json.JSONDecoder()


def scalar(value):
    """Convert a setting to an env string."""
    return value if isinstance(value, str) else json.dumps(value)


def objects(value):
    """Yield all stored mappings, including operate's user params."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def edit_json(text, desired, value=None, addition=None):
    """Accept only an edit that gives the expected parsed JSON document."""
    pattern = r":\s*" if addition is None else r"\{"
    for match in re.finditer(pattern, text):
        start = match.end() if addition is None else match.start()
        try:
            old, end = DECODER.raw_decode(text, start)
        except ValueError:
            continue
        if addition is None:
            candidate = text[:start] + json.dumps(value) + text[end:]
        else:
            key, entry = addition
            end_value = len(text[: end - 1].rstrip())
            indent = re.search(r"\n([ \t]*)\S", text[start:end])
            prefix = ("\r\n" if "\r\n" in text else "\n") + indent[1] if indent else " "
            inserted = (
                ("," if old else "")
                + prefix
                + json.dumps(key)
                + ": "
                + json.dumps(entry)
            )
            candidate = text[:end_value] + inserted + text[end_value:]
        try:
            if json.loads(candidate) == desired:
                return candidate
        except ValueError:
            continue
    raise ValueError("Could not locate a unique JSON value to edit")


def add_min_expected_profit(strategy, changes):
    """Add the 18-decimal Omen profit threshold only when it is absent."""
    if "min_expected_profit" in strategy:
        return False
    strategy["min_expected_profit"] = MIN_EXPECTED_PROFIT
    changes.append(f"add {STRATEGIES}.min_expected_profit={MIN_EXPECTED_PROFIT}")
    return True


def update_strategy_hashes(mapping, changes, hashes):
    """Replace hash keys only for known single-strategy entries."""
    changed = False
    for old, strategies in list(mapping.items()):
        for name, current in hashes.items():
            if strategies == [name] and old != current:
                mapping[current] = mapping.pop(old)
                changes.append(f"{STRATEGY_HASHES}: {old} -> {current}")
                changed = True
    return changed


def migrate_json(text, priorities, hashes):
    """Change marketplace settings and add missing strategy defaults."""
    data = json.loads(text)
    changes = []
    for env in objects(data):
        for variable in (STRATEGIES, STRATEGY_HASHES):
            if variable not in env:
                continue
            strategy_entry = env[variable]
            value = strategy_entry.get("value")
            if value is not None and value != "":
                strategy = json.loads(value) if isinstance(value, str) else value
                changed = (
                    add_min_expected_profit(strategy, changes)
                    if variable == STRATEGIES
                    else update_strategy_hashes(strategy, changes, hashes)
                )
                if changed:
                    value = json.dumps(strategy) if isinstance(value, str) else strategy
                    strategy_entry["value"] = value
                    text = edit_json(text, data, value)
        if MARKETPLACE not in env:
            continue
        entry = env[MARKETPLACE]
        value = entry.get("value")
        if not value:
            continue
        mech = json.loads(value) if isinstance(value, str) else value
        priorities.append(mech)
        removed = {key: mech[key] for key in KEYS if key in mech}
        if not removed:
            continue
        clean = {key: val for key, val in mech.items() if key not in removed}
        entry["value"] = json.dumps(clean)
        text = edit_json(text, data, json.dumps(clean))
        for key, val in removed.items():
            target, name = KEYS[key]
            changes.append(f"remove {MARKETPLACE}.{key}={scalar(val)}")
            if target in env:
                existing = env[target]
                current = existing.get("value")
                if current is None or current == "":
                    existing["value"] = scalar(val)
                    text = edit_json(text, data, scalar(val))
                    changes.append(f"set {target}={scalar(val)}")
                    continue
                changes.append(f"keep existing {target}")
                continue
            new_entry = {
                "name": name,
                "description": "",
                "value": scalar(val),
                "provision_type": entry.get("provision_type", "computed"),
            }
            env[target] = new_entry
            text = edit_json(text, data, addition=(target, new_entry))
            changes.append(f"add {target}={scalar(val)}")
    # Stored user args can also contain a direct marketplace mapping.
    priorities.extend(obj for obj in objects(data) if "priority_mech_address" in obj)
    return text, changes


def decode_env(raw):
    """Read JSON, optionally wrapped in a YAML or shell string."""
    raw = re.sub(r"\\\r?\n\s*", "", raw).replace("\r\n", " ").replace("\n", " ").strip()
    if raw.startswith("'") and raw.endswith("'"):
        raw = raw[1:-1].replace("''", "'")
    value = json.loads(raw)
    return json.loads(value) if isinstance(value, str) else value


def migrate_env(text, priorities, hashes):
    """Edit env assignments, compose mappings, and Kubernetes env pairs."""
    lines = text.splitlines(keepends=True)
    changes = []
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        match = re.match(
            r"(\s*(?:-\s*)?)(MECH_MARKETPLACE_CONFIG|STRATEGIES_KWARGS|"
            r"FILE_HASH_TO_STRATEGIES)"
            r"(=|:\s*)(.*?)(\r?\n)?$",
            line,
        )
        pair = re.match(
            r"(\s*)- name: (MECH_MARKETPLACE_CONFIG|STRATEGIES_KWARGS|"
            r"FILE_HASH_TO_STRATEGIES)\s*$",
            line,
        )
        if pair:
            variable = pair[2]
            value_index = index + 1
            match = re.match(r"(\s*value)(:\s*)(.*?)(\r?\n)?$", lines[value_index])
            if not match:
                raise ValueError("Missing marketplace value after Kubernetes env name")
        elif match:
            variable = match[2]
            value_index = index
        else:
            continue
        if pair:
            prefix, separator, raw, newline = match.groups()
        else:
            prefix, _, separator, raw, newline = match.groups()
        if variable in (STRATEGIES, STRATEGY_HASHES) and raw.strip() in ("", "''", '""'):
            continue
        scalar_end = value_index
        while True:
            try:
                mech = decode_env(raw)
                break
            except ValueError:
                scalar_end += 1
                if scalar_end >= len(lines) or len(lines[scalar_end]) - len(
                    lines[scalar_end].lstrip()
                ) <= len(line) - len(line.lstrip()):
                    raise
                raw += "\n" + lines[scalar_end].rstrip("\r\n")
        removed = {}
        if variable == STRATEGIES:
            if not add_min_expected_profit(mech, changes):
                continue
        elif variable == STRATEGY_HASHES:
            if not update_strategy_hashes(mech, changes, hashes):
                continue
        else:
            priorities.append(mech)
            removed = {key: mech.pop(key) for key in KEYS if key in mech}
            if not removed:
                continue
        clean = (
            json.dumps(mech, separators=(",", ":"))
            if variable != MARKETPLACE and separator == "="
            else json.dumps(mech)
        )
        if raw.startswith("'"):
            clean = "'" + clean.replace("'", "''") + "'"
        elif raw.startswith('"'):
            clean = json.dumps(clean)
        lines[value_index : scalar_end + 1] = [
            prefix + ("" if pair else variable) + separator + clean + (newline or "\n")
        ]
        if variable != MARKETPLACE:
            continue
        # Limit existing-value checks to this YAML environment block.
        indent = len(line) - len(line.lstrip())

        def in_environment(candidate, level=indent):
            if not candidate.strip():
                return True
            depth = len(candidate) - len(candidate.lstrip())
            if depth != level:
                return depth > level
            return bool(
                re.match(
                    r"(?:-\s*(?:name:|[A-Z_0-9]+=)|[A-Z_0-9]+[:=]|#)",
                    candidate.lstrip(),
                )
            )

        start, end = index, value_index + 1
        while start > 0 and in_environment(lines[start - 1]):
            start -= 1
        while end < len(lines) and in_environment(lines[end]):
            end += 1
        block = "".join(lines[start:end])
        additions = []
        for key, val in removed.items():
            target, _ = KEYS[key]
            changes.append(f"remove {MARKETPLACE}.{key}={scalar(val)}")
            if re.search(
                r"(?m)^\s*(?:-\s*)?(?:name:\s*)?" + target + r"(?:=|:|\s*$)", block
            ):
                changes.append(f"keep existing {target}")
                continue
            value = scalar(val)
            if pair:
                addition = (
                    pair[1]
                    + "- name: "
                    + target
                    + "\n"
                    + prefix
                    + separator
                    + json.dumps(value)
                )
            else:
                addition = (
                    prefix
                    + target
                    + separator
                    + (json.dumps(value) if separator.startswith(":") else value)
                )
            additions.append(addition + (newline or "\n"))
            changes.append(f"add {target}={value}")
        lines[value_index + 1 : value_index + 1] = additions
    return "".join(lines), changes


def migrate(quickstart, dry_run=False, warn_only=False):
    """Migrate a selected quickstart and every cached operate service."""
    packages = json.loads(
        (Path(__file__).resolve().parents[1] / "packages/packages.json").read_text()
    )
    hashes = {
        name: packages["dev"][f"custom/valory/{name}/0.1.0"]
        for name in ("kelly_criterion", "fixed_bet")
    }
    paths = [quickstart / "configs/config_predict_trader.json"]
    services = sorted((quickstart / ".operate/services").glob("sc-*"))
    if not services:
        print(f">> {quickstart}: no operate services; skipping cache")
    for service in services:
        paths.extend([service / "config.json"])
        paths.extend(sorted(service.rglob("agent_0.env")))
        paths.extend(sorted(service.rglob("build.yaml")))
    stored = []
    for path in paths:
        if not path.is_file():
            print(f">> {path}: missing; skipping")
            continue
        with path.open(encoding="utf-8", newline="") as source:
            text = source.read()
        priorities = []
        updated, changes = (migrate_json if path.suffix == ".json" else migrate_env)(
            text, priorities, hashes
        )
        for mech in priorities:
            priority = (
                mech.get("priority_mech_address"),
                mech.get("priority_mech_service_id"),
            )
            if all(value is not None for value in priority):
                stored.append((path, priority))
        if warn_only:
            continue
        if changes:
            print(
                f">> {'(dry-run) would update' if dry_run else 'updated'} {path}: "
                + "; ".join(changes)
            )
            if not dry_run:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = path.with_name(path.name + ".bak-" + stamp)
                shutil.copy2(path, backup)
                with path.open("w", encoding="utf-8", newline="") as destination:
                    destination.write(updated)
                print(f"   backup: {backup}")
        else:
            print(f">> {path}: config unchanged")
    if stored:
        reference_path, reference = stored[0]
        for path, priority in stored[1:]:
            if (str(priority[0]).lower(), str(priority[1])) != (
                str(reference[0]).lower(),
                str(reference[1]),
            ):
                print(
                    f"WARNING: priority mech differs: {reference_path} "
                    f"address={reference[0]} service_id={reference[1]}; "
                    f"{path} address={priority[0]} service_id={priority[1]}. "
                    "Check ERC8004 registration before deploying."
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("quickstart", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()
    migrate(args.quickstart, args.dry_run, args.warn_only)
