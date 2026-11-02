"""Validate a repository configuration without host-specific layers."""

from dataclasses import dataclass
from pathlib import Path

from jailbee.config.common import _parse_yaml_text
from jailbee.config.errors import ConfigError
from jailbee.config.loader import load_config_from_layers
from jailbee.config.models_agents import AutostartStage


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
        cfg = load_config_from_layers(
            {}, _parse_yaml_text(text, label), path, origin=label, emit_hint=False, local_raw={}, repo_layer=True
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
    return RepoLayerReport(label, None, tuple(unchecked), tuple(cfg.deprecation_notices()))
