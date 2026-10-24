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

"""Migrate off-chain settings, marketplace addresses, and Omen strategy defaults.

Sync saved quickstart inputs and cached service, agent, and Kubernetes envs.
Skip all files if configs/config_predict_trader.json is missing or declares
IS_RUNNING_ON_POLYMARKET=true. In mixed caches, skip each Polymarket service
as a group. The Omen profit threshold uses 18-decimal xDAI units.
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
V1_MARKETPLACE = "0x4554fE75c1f5576c1d7F765B2A036c199Adae329"
V2_MARKETPLACE = "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
ZERO_ADDRESS = "0x" + "0" * 40
DECODER = json.JSONDecoder()
# These controls are operator settings. Keep service runtime addresses intact.
CONTROL_ENV = (
    "PRIORITY_MECH_ADDRESS",
    "PRIORITY_MECH_SERVICE_ID",
    "OFFCHAIN_DEPOSIT_TARGET_CALLS",
    "PRIORITIZE_FRESH_MARKETS",
    "USE_OFFCHAIN",
    "IRRELEVANT_TOOLS",
    "MAX_TRADES",
    "MAX_MECH_REQUESTS_PER_CYCLE",
    "DEFAULT_ACTIVITY_GOAL",
    "ACTIVITY_TARGET",
    "STOP_TRADING_IF_STAKING_KPI_MET",
    "DISABLE_TRADING",
)
PARAM_PREFIX = "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_"
PRIORITY_ENV = {
    "PRIORITY_MECH_ADDRESS": "priority_mech_address",
    "PRIORITY_MECH_SERVICE_ID": "priority_mech_service_id",
}


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


def is_polymarket(path):
    """Detect the Polymarket flag in JSON, env, compose, or Kubernetes files."""
    text = path.read_text(encoding="utf-8")
    key = r"(?:SKILL_[A-Z0-9_]+_)?IS_RUNNING_ON_POLYMARKET"
    if path.suffix == ".json":
        for obj in objects(json.loads(text)):
            for name, value in obj.items():
                if re.fullmatch(key, name):
                    if isinstance(value, dict):
                        value = value.get("value")
                    if str(value).strip().lower() == "true":
                        return True
        return False
    return bool(
        re.search(
            r"^\s*(?:-\s*)?(?:"
            + key
            + r"[=:][ \t]*|- name:[ \t]*"
            + key
            + r"[ \t]*\r?\n[ \t]*value:[ \t]*)"
            + r"['\"]?true['\"]?[ \t]*(?:#.*)?$",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
    )


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


def update_marketplace(mech, enabled, changes, warnings):
    """Replace the known V1 address and report settings that need review."""
    address = mech.get("mech_marketplace_address")
    changed = str(address).lower() == V1_MARKETPLACE.lower()
    if changed:
        mech["mech_marketplace_address"] = V2_MARKETPLACE
        changes.append(
            f"{MARKETPLACE}.mech_marketplace_address: {address} -> {V2_MARKETPLACE}"
        )
    elif address is not None and str(address).lower() != V2_MARKETPLACE.lower():
        warnings.append(f"unknown mech_marketplace_address={address}; kept unchanged")
    if (
        str(enabled).lower() == "true"
        and mech.get("requester_staking_instance_address") == ZERO_ADDRESS
    ):
        warnings.append(
            "requester_staking_instance_address is zero while USE_MECH_MARKETPLACE "
            "is true; kept unchanged"
        )
    return changed


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


def migrate_json(text, priorities, hashes, warnings):
    """Change marketplace settings and add missing strategy defaults."""
    data = json.loads(text)
    changes = []
    for env in objects(data):
        if "mech_marketplace_config" in env:
            value = env["mech_marketplace_config"]
            mech = json.loads(value) if isinstance(value, str) else value
            if update_marketplace(
                mech, env.get("use_mech_marketplace"), changes, warnings
            ):
                if isinstance(value, str):
                    env["mech_marketplace_config"] = json.dumps(mech)
                    text = edit_json(text, data, env["mech_marketplace_config"])
                else:
                    text = edit_json(text, data, V2_MARKETPLACE)
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
        enabled = env.get("USE_MECH_MARKETPLACE")
        if isinstance(enabled, dict):
            enabled = enabled.get("value")
        changed = update_marketplace(mech, enabled, changes, warnings)
        removed = {key: mech[key] for key in KEYS if key in mech}
        if not removed and not changed:
            continue
        clean = {key: val for key, val in mech.items() if key not in removed}
        entry["value"] = json.dumps(clean) if isinstance(value, str) else clean
        text = edit_json(text, data, entry["value"])
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


def migrate_env(text, priorities, hashes, warnings, desired=None):
    """Edit env assignments, compose mappings, and Kubernetes env pairs."""
    lines = text.splitlines(keepends=True)
    changes = []
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        match = re.match(
            r"(\s*(?:-\s*)?)((?:SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_)?"
            r"MECH_MARKETPLACE_CONFIG|STRATEGIES_KWARGS|"
            r"FILE_HASH_TO_STRATEGIES)"
            r"(=|:\s*)(.*?)(\r?\n)?$",
            line,
        )
        pair = re.match(
            r"(\s*)- name: ((?:SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_)?"
            r"MECH_MARKETPLACE_CONFIG|STRATEGIES_KWARGS|"
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
        bare = variable.removeprefix(PARAM_PREFIX)
        if pair:
            prefix, separator, raw, newline = match.groups()
        else:
            prefix, _, separator, raw, newline = match.groups()
        if bare in (STRATEGIES, STRATEGY_HASHES) and raw.strip() in (
            "",
            "''",
            '""',
        ):
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
        removed = {}
        if bare == STRATEGIES:
            if not add_min_expected_profit(mech, changes):
                continue
        elif bare == STRATEGY_HASHES:
            if not update_strategy_hashes(mech, changes, hashes):
                continue
        else:
            priorities.append(dict(mech))
            enabled = re.search(
                r"(?m)^\s*(?:-\s*)?USE_MECH_MARKETPLACE[=:]\s*['\"]?(true|false)"
                r"|^\s*- name: USE_MECH_MARKETPLACE\s*\n\s*value:\s*['\"]?(true|false)",
                block,
                re.IGNORECASE,
            )
            changed = update_marketplace(
                mech,
                next(value for value in enabled.groups() if value) if enabled else None,
                changes,
                warnings,
            )
            changed = sync_priority(mech, desired or {}, changes) or changed
            removed = {key: mech.pop(key) for key in KEYS if key in mech}
            if not removed and not changed:
                continue
        clean = (
            json.dumps(mech, separators=(",", ":"))
            if separator == "="
            else json.dumps(mech)
        )
        if raw.startswith("'"):
            clean = "'" + clean.replace("'", "''") + "'"
        elif raw.startswith('"'):
            clean = json.dumps(clean)
        lines[value_index : scalar_end + 1] = [
            prefix + ("" if pair else variable) + separator + clean + (newline or "\n")
        ]
        if bare != MARKETPLACE:
            continue
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


def sync_priority(mech, desired, changes):
    """Sync only explicit priority inputs inside a marketplace dict."""
    changed = False
    for key, field in PRIORITY_ENV.items():
        if key not in desired:
            continue
        value = desired[key]["value"]
        value = int(value) if field == "priority_mech_service_id" else value
        if mech.get(field) != value:
            mech[field] = value
            changes.append(f"sync {MARKETPLACE}.{field} from quickstart")
            changed = True
    return changed


def sync_cached_json(text, desired):
    """Sync explicit quickstart controls without changing cached runtime values."""
    data = json.loads(text)
    changes = []
    for obj in list(objects(data)):
        for key in (MARKETPLACE, "mech_marketplace_config"):
            if key not in obj:
                continue
            container = obj
            field = key
            if isinstance(obj[key], dict) and "value" in obj[key]:
                container = obj[key]
                field = "value"
            value = container[field]
            if not value:
                continue
            mech = json.loads(value) if isinstance(value, str) else value
            if sync_priority(mech, desired, changes):
                container[field] = json.dumps(mech) if isinstance(value, str) else mech
                text = edit_json(text, data, container[field])
        saved = obj.get("user_provided_args")
        if isinstance(saved, dict):
            for key, source in desired.items():
                value = source["value"]
                if key not in saved:
                    saved[key] = value
                    text = edit_json(text, data, addition=(key, value))
                    changes.append(f"add saved input {key} from quickstart")
                elif saved[key] != value:
                    saved[key] = value
                    text = edit_json(text, data, value)
                    changes.append(f"sync saved input {key} from quickstart")
        env = obj.get("env_variables")
        if isinstance(env, dict):
            for key, source in desired.items():
                value = source["value"]
                if key not in env:
                    env[key] = dict(source)
                    text = edit_json(text, data, addition=(key, env[key]))
                    changes.append(f"sync {key} from quickstart")
                elif isinstance(env[key], dict):
                    if env[key].get("value") != value:
                        env[key]["value"] = value
                        text = edit_json(text, data, value)
                        changes.append(f"sync {key} from quickstart")
                elif env[key] != value:
                    env[key] = value
                    text = edit_json(text, data, value)
                    changes.append(f"sync {key} from quickstart")
        for key, source in desired.items():
            field = key.lower()
            if field not in obj:
                continue
            value = source["value"]
            if not isinstance(obj[field], str):
                value = json.loads(scalar(value))
            if obj[field] != value:
                obj[field] = value
                text = edit_json(text, data, value)
                changes.append(f"sync {field} from quickstart")
    return text, changes


def sync_cached_env(text, desired, env_file=False):
    """Sync bare controls and existing component-path keys in agent env blocks."""
    values = {key: scalar(source["value"]) for key, source in desired.items()}
    lines = text.splitlines(keepends=True)
    blocks = [(0, len(lines), None)] if env_file else []
    if not env_file:
        for index, line in enumerate(lines):
            match = re.match(r"(\s*)(- )?(?:environment|env):\s*$", line)
            if not match:
                continue
            level = len(match[1]) + len(match[2] or "")
            end = index + 1
            while end < len(lines):
                entry = lines[end]
                depth = len(entry) - len(entry.lstrip())
                if entry.strip() and (
                    depth < level
                    or (depth == level and not entry.lstrip().startswith("-"))
                ):
                    break
                end += 1
            block = "".join(lines[index + 1 : end])
            if PARAM_PREFIX in block or any(
                re.search(r"\b" + key + r"(?:=|:|\s*$)", block, re.M) for key in values
            ):
                blocks.append((index + 1, end, level))
    changes = []
    for start, end, _level in reversed(blocks):
        present = set()
        style = "env" if env_file else "mapping"
        indent = ""
        for index in range(start, end):
            line = lines[index]
            pair = re.match(r"(\s*)- name: ([A-Z_0-9]+)\s*$", line)
            match = pair or re.match(
                r"(\s*(?:-\s*)?)([A-Z_0-9]+)(=|:\s*)(.*?)(\r?\n)?$", line
            )
            if not match:
                continue
            key = match[2]
            if pair:
                style = "kubernetes"
                indent = pair[1]
            else:
                style = "env" if env_file else "list" if "-" in match[1] else "mapping"
                indent = match[1]
            bare = key.removeprefix(PARAM_PREFIX)
            if bare not in values:
                continue
            present.add(key)
            value = values[bare]
            if pair:
                target = index + 1
                scalar_match = re.match(r"(\s*value:\s*)(.*?)(\r?\n)?$", lines[target])
                if not scalar_match:
                    raise ValueError(f"Missing value for {key}")
                replacement = (
                    scalar_match[1] + json.dumps(value) + (scalar_match[3] or "\n")
                )
                old_value = scalar_match[2]
            else:
                target = index
                replacement = (
                    match[1]
                    + key
                    + match[3]
                    + (value if match[3] == "=" else json.dumps(value))
                    + (match[5] or "\n")
                )
                old_value = match[4]
            # Preserve existing formatting when the scalar already matches.
            try:
                existing = json.loads(old_value)
            except ValueError:
                existing = old_value.strip("'\"")
            if old_value != value and scalar(existing) != value:
                lines[target] = replacement
                changes.append(f"sync {key} from quickstart")
        additions = []
        for key, value in values.items():
            if key in present:
                continue
            if style == "kubernetes":
                additions.append(
                    indent
                    + "- name: "
                    + key
                    + "\n"
                    + indent
                    + "  value: "
                    + json.dumps(value)
                    + "\n"
                )
            elif style in ("env", "list"):
                additions.append(indent + key + "=" + value + "\n")
            else:
                additions.append(indent + key + ": " + json.dumps(value) + "\n")
            changes.append(f"add {key} from quickstart")
        if additions:
            # Reuse the block's line-ending style so appended controls do
            # not introduce mixed endings in CRLF files.
            preceding = "".join(lines[max(0, end - 20) : end])
            newline = "\r\n" if "\r\n" in preceding else "\n"
            if end > start and not lines[end - 1].endswith(("\n", "\r")):
                lines[end - 1] += newline
            additions = [a.replace("\n", newline) for a in additions]
        lines[end:end] = additions
    return "".join(lines), changes


def migrate(quickstart, dry_run=False, warn_only=False):
    """Migrate an Omen quickstart and skip Polymarket cached services."""
    config = quickstart / "configs/config_predict_trader.json"
    if not config.is_file() or is_polymarket(config):
        print(
            f"WARNING: {config}: missing or Polymarket quickstart; skipping all files"
        )
        return
    packages = json.loads(
        (Path(__file__).resolve().parents[1] / "packages/packages.json").read_text()
    )
    hashes = {
        name: packages["dev"][f"custom/valory/{name}/0.1.0"]
        for name in ("kelly_criterion", "fixed_bet")
    }
    paths = [config]
    paths.extend(sorted((quickstart / ".operate").glob("*-quickstart-config.json")))
    legacy = quickstart / ".operate/local_config.json"
    if legacy.is_file():
        paths.append(legacy)
    services = sorted((quickstart / ".operate/services").glob("sc-*"))
    if not services:
        print(f">> {quickstart}: no operate services; skipping cache")
    for service in services:
        service_paths = [service / "config.json"]
        service_paths.extend(sorted(service.rglob("agent_0.env")))
        service_paths.extend(sorted(service.rglob("build.yaml")))
        for path in service_paths:
            if path.is_file() and is_polymarket(path):
                print(
                    f"WARNING: {service}: Polymarket flag in {path}; "
                    "skipping all service files"
                )
                break
        else:
            paths.extend(service_paths)
    stored = []
    desired = {}
    for path in paths:
        if not path.is_file():
            print(f">> {path}: missing; skipping")
            continue
        with path.open(encoding="utf-8", newline="") as source:
            text = source.read()
        priorities = []
        warnings = []
        # Saved inputs use plain values. Only sync explicit controls there.
        if path.parent == quickstart / ".operate":
            updated, changes = text, []
        else:
            updated, changes = (
                migrate_json if path.suffix == ".json" else migrate_env
            )(text, priorities, hashes, warnings)
        if path == paths[0]:
            env = json.loads(updated).get("env_variables", {})
            desired = {
                key: entry
                for key, entry in env.items()
                if key in CONTROL_ENV
                and isinstance(entry, dict)
                and entry.get("value") not in (None, "")
            }
        if desired:
            updated, synced = (
                sync_cached_json(updated, desired)
                if path.suffix == ".json"
                else sync_cached_env(updated, desired, path.name == "agent_0.env")
            )
            changes.extend(synced)
            if path.suffix != ".json":
                updated, synced = migrate_env(updated, [], hashes, [], desired)
                changes.extend(synced)
            # Preview warnings use the old values. Real runs use the synced values.
            if not dry_run and not warn_only and path.parent != quickstart / ".operate":
                priorities = []
                (migrate_json if path.suffix == ".json" else migrate_env)(
                    updated, priorities, hashes, []
                )
        for warning in warnings:
            print(f"WARNING: {path}: {warning}")
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
