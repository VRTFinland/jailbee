"""Validate a repository configuration without host-specific layers."""

from dataclasses import dataclass
from pathlib import Path

from jailbee.config import Config
from jailbee.config.common import _parse_yaml_text
from jailbee.config.errors import ConfigError
from jailbee.config.legacy_pr import fold_legacy_pr_keys
from jailbee.config.loader import load_config_from_layers
from jailbee.config.models_agents import AutostartStage


def default_repo_config_path(cwd: Path | None = None) -> Path:
    current = (cwd or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.is_dir() or marker.is_file():
            return directory / ".jailbee" / "config.yaml"
    return current / ".jailbee" / "config.yaml"


def load_repo_layer(path: Path) -> Config:
    path = path.resolve()
    try:
        raw = _parse_yaml_text(path.read_text(), str(path))
        return load_config_from_layers({}, raw, path, origin=str(path), emit_hint=False,
                                       local_raw={}, repo_layer=True, skip_git=True)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(str(exc)) from exc


@dataclass(frozen=True)
class RepoLayerReport:
    label: str
    error: str | None
    unchecked: tuple[str, ...]
    notices: tuple[str, ...]


def validate_repo_layer(path: Path) -> RepoLayerReport:
    path = path.resolve()
    label = str(path)
    try:
        text = path.read_text()
        raw = _parse_yaml_text(text, label)
        cfg = load_config_from_layers(
            {},
            raw,
            path,
            origin=label,
            emit_hint=False,
            local_raw={},
            repo_layer=True,
        )
    except (ConfigError, OSError, UnicodeError) as exc:
        return RepoLayerReport(label, str(exc), (), ())
    unchecked = []
    for trigger in ("on_create", "on_start"):
        for entry in getattr(cfg.autostart, trigger):
            items = [
                (entry.stage if isinstance(entry, AutostartStage) else entry.name, entry.mounts)
            ]
            if isinstance(entry, AutostartStage):
                items.extend(
                    (step.name, step.mounts) for chain in entry.all_chains() for step in chain.steps
                )
            for name, mounts in items:
                for key in mounts:
                    if key not in cfg.optional_mounts:
                        unchecked.append(
                            f"unchecked (host layer): autostart.{trigger}[{name}].mounts "
                            f"references optional_mount '{key}' — not defined in this file; "
                            "host layers may define it"
                        )
    for name, service in cfg.services.items():
        for index, step in enumerate(service.prepare, 1):
            for key in step.mounts:
                if key not in cfg.optional_mounts:
                    unchecked.append(
                        f"unchecked (host layer): services.{name}.prepare[{index}].mounts "
                        f"references optional_mount '{key}' — not defined in this file; "
                        "host layers may define it"
                    )
    notices = cfg.deprecation_notices()
    if cfg.golden.python:
        notices.append("golden.python is deprecated and ignored; remove it from the golden block.")
    if "dashboard" in cfg.model_fields_set:
        notices.append("dashboard: is deprecated and ignored; remove the repo-level block.")
    if isinstance(raw.get("chrome"), dict):
        notices.append("chrome: is deprecated; migrate it to browsers.chrome.")
    if fold_legacy_pr_keys(raw)[1]:
        notices.append("ai_pr_description and other legacy PR keys are deprecated; use pr:.")
    return RepoLayerReport(label, None, tuple(unchecked), tuple(notices))
