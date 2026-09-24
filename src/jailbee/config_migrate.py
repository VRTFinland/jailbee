"""Plan and apply migrations from deprecated config spellings and storage.

Planning is pure and operates on in-memory YAML. Applying validates each
resulting file independently before writing any file; migrations are
idempotent, so a crash between file writes is safe to retry.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from jailbee.config.common import _HOST_LEVEL_KEYS, _parse_yaml_text, normalize_credentials_key
from jailbee.config.local_layer import local_config_dir, local_config_path, validate_local_raw
from jailbee.config_writer import DELETE, YamlChange, credential_key_migration, patch_yaml

if TYPE_CHECKING:
    from pathlib import Path

    from sqlmodel import Session

MIGRATION_IDS: tuple[str, ...] = (
    "chrome-block",
    "claude-credentials-key",
    "credentials-repos",
    "github-api-tokens",
    "egress-db-rows",
)

_MASK = "********"


@dataclass(frozen=True)
class MigrationInputs:
    """File texts by path, plus the legacy database rows."""

    global_path: Path
    texts: dict[Path, str]
    egress_rows: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    """One migration's changes to one file."""

    migration_id: str
    summary: str
    path: Path
    changes: tuple[YamlChange, ...]


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    conflicts: tuple[str, ...]
    new_texts: dict[Path, str]
    rows_to_delete: tuple[tuple[str, str], ...]

    @property
    def pending(self) -> bool:
        return bool(self.steps or self.rows_to_delete)


class _State:
    def __init__(self, inputs: MigrationInputs) -> None:
        self.inputs = inputs
        self.texts = dict(inputs.texts)
        self.steps: list[Step] = []
        self.conflicts: list[str] = []
        self.rows: list[tuple[str, str]] = []

    def raw(self, path: Path) -> dict[str, object]:
        return _parse_yaml_text(self.texts.get(path, ""), str(path))

    def change(
        self, migration_id: str, summary: str, path: Path, changes: list[YamlChange]
    ) -> None:
        if changes:
            self.texts[path] = patch_yaml(self.texts.get(path, ""), changes)
            self.steps.append(Step(migration_id, summary, path, tuple(changes)))


def _chrome_block(state: _State) -> None:
    from jailbee.config.loader import resolve_browsers_raw

    paths = [
        state.inputs.global_path,
        *sorted(p for p in state.texts if p != state.inputs.global_path),
    ]
    for path in paths:
        raw = state.raw(path)
        if isinstance(raw.get("chrome"), dict):
            browsers = resolve_browsers_raw(raw)["browsers"]
            state.change(
                "chrome-block",
                "`chrome:` → `browsers.chrome`",
                path,
                [YamlChange(("browsers",), browsers), YamlChange(("chrome",), DELETE)],
            )


def _claude_credentials_key(state: _State) -> None:
    path = state.inputs.global_path
    state.change(
        "claude-credentials-key",
        "`claude_credentials:` → `credentials:`",
        path,
        credential_key_migration(state.raw(path), []),
    )


def _per_repo_map(
    state: _State,
    *,
    migration_id: str,
    map_path: tuple[str, str],
    local_key: tuple[str, str],
) -> None:
    gpath = state.inputs.global_path
    block = state.raw(gpath).get(map_path[0])
    entries = block.get(map_path[1]) if isinstance(block, dict) else None
    if not isinstance(entries, dict) or not entries:
        return
    dotted_map = ".".join(map_path)
    dotted_local = ".".join(local_key)
    deletes: list[YamlChange] = []
    for prefix, value in sorted(entries.items()):
        if not isinstance(prefix, str):
            continue
        lpath = local_config_path(prefix)
        lblock = state.raw(lpath).get(local_key[0])
        present = isinstance(lblock, dict) and local_key[1] in lblock
        existing = lblock.get(local_key[1]) if isinstance(lblock, dict) else None
        if present and existing != value:
            state.conflicts.append(
                f"{dotted_map}.{prefix} in {gpath} differs from {dotted_local} in {lpath}; "
                "left in place — remove one of them by hand."
            )
            continue
        if not present:
            state.change(
                migration_id,
                f"{dotted_map}.{prefix} → {dotted_local}",
                lpath,
                [YamlChange(local_key, value)],
            )
        deletes.append(YamlChange((*map_path, prefix), DELETE))
    if len(deletes) == len(entries):
        deletes = [YamlChange(map_path, DELETE)]
    state.change(migration_id, f"drop migrated {dotted_map} entries", gpath, deletes)


def _egress_db_rows(state: _State) -> None:
    for prefix, entries in sorted(state.inputs.egress_rows.items()):
        path = local_config_path(prefix)
        current = state.raw(path).get("egress_allow") or []
        current_list = (
            [entry for entry in current if isinstance(entry, str)]
            if isinstance(current, list)
            else []
        )
        missing = [entry for entry in entries if entry not in current_list]
        if missing:
            state.change(
                "egress-db-rows",
                f"state.sqlite repo overrides → {path.name} egress_allow",
                path,
                [YamlChange(("egress_allow",), [*current_list, *missing])],
            )
        state.rows.extend((prefix, entry) for entry in entries)


def plan_migrations(inputs: MigrationInputs) -> Plan:
    """Run every migration in registry order over in-memory text. Pure."""
    state = _State(inputs)
    _chrome_block(state)
    _claude_credentials_key(state)
    _per_repo_map(
        state,
        migration_id="credentials-repos",
        map_path=("credentials", "repos"),
        local_key=("credentials", "group"),
    )
    _per_repo_map(
        state,
        migration_id="github-api-tokens",
        map_path=("github", "api_tokens"),
        local_key=("github", "token"),
    )
    _egress_db_rows(state)
    changed = {step.path for step in state.steps}
    return Plan(
        tuple(state.steps),
        tuple(state.conflicts),
        {path: state.texts[path] for path in changed},
        tuple(state.rows),
    )


def gather_inputs(session: Session) -> MigrationInputs:
    """Read global/local files and legacy egress rows."""
    from jailbee.egress_scope import legacy_rows_by_prefix
    from jailbee.global_config import default_global_config_path

    global_path = default_global_config_path()
    texts: dict[Path, str] = {}
    if global_path.exists():
        texts[global_path] = global_path.read_text(encoding="utf-8")
    root = local_config_dir()
    if root.is_dir():
        texts.update(
            {path: path.read_text(encoding="utf-8") for path in sorted(root.glob("*.yaml"))}
        )
    return MigrationInputs(global_path, texts, legacy_rows_by_prefix(session))


def _secrets(inputs: MigrationInputs, plan: Plan) -> list[str]:
    secrets: set[str] = set()
    for text in [*inputs.texts.values(), *plan.new_texts.values()]:
        raw = yaml.safe_load(text) or {}
        github = raw.get("github") if isinstance(raw, dict) else None
        if not isinstance(github, dict):
            continue
        token = github.get("token")
        if isinstance(token, str) and token:
            secrets.add(token)
        tokens = github.get("api_tokens")
        if isinstance(tokens, dict):
            secrets.update(value for value in tokens.values() if isinstance(value, str) and value)
    return sorted(secrets, key=len, reverse=True)


def render_diff(inputs: MigrationInputs, plan: Plan) -> str:
    """Render unified diffs, masking every token found before or after."""
    output: list[str] = []
    for path, new_text in sorted(plan.new_texts.items()):
        output.extend(
            difflib.unified_diff(
                inputs.texts.get(path, "").splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{path} (now)",
                tofile=f"{path} (after migrate)",
            )
        )
    text = "".join(output)
    for secret in _secrets(inputs, plan):
        text = text.replace(secret, _MASK)
    return text


def _validate(inputs: MigrationInputs, plan: Plan) -> None:
    """Validate each resulting file against its own layer before any writes."""
    from pydantic import ValidationError

    from jailbee.config import Config, ConfigError
    from jailbee.config.loader import resolve_agents_raw, resolve_browsers_raw
    from jailbee.global_config import validate_global_raw

    for path, text in plan.new_texts.items():
        raw = _parse_yaml_text(text, str(path))
        if path == inputs.global_path:
            raw, _ = normalize_credentials_key(raw, str(path))
            validate_global_raw(raw, path, emit_hint=False)
            overlay = {key: value for key, value in raw.items() if key not in _HOST_LEVEL_KEYS}
            try:
                Config.model_validate(resolve_browsers_raw(resolve_agents_raw(overlay)))
            except (ValidationError, ConfigError) as error:
                raise ConfigError(f"Config validation failed in {path}:\n{error}") from error
        else:
            validate_local_raw(raw, str(path))


def apply_plan(inputs: MigrationInputs, plan: Plan, session: Session) -> list[Path]:
    """Validate all results, then atomically write files and delete DB rows.

    File writes are path-sorted. If interrupted midway, already-completed
    migrations have been removed from their source and the next plan safely
    resumes the remainder; database rows are deleted only after all writes.
    """
    from jailbee.config_writer import write_with_backup
    from jailbee.egress_scope import delete_legacy_rows

    _validate(inputs, plan)
    local_config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    local_config_dir().chmod(0o700)
    backups: list[Path] = []
    for path, new_text in sorted(plan.new_texts.items()):
        is_local = path != inputs.global_path
        backup = write_with_backup(
            path,
            inputs.texts.get(path, ""),
            new_text,
            mode=0o600 if is_local else None,
        )
        if backup is not None:
            backups.append(backup)
    if plan.rows_to_delete:
        delete_legacy_rows(session, plan.rows_to_delete)
    return backups
