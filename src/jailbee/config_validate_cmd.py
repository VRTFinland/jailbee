"""Shared repository-layer validation and output."""

from pathlib import Path

from jailbee.config.repo_layer import RepoLayerReport, validate_repo_layer
from jailbee.tui import error_plain, info, success, warn_plain


def report_repo_layer(report: RepoLayerReport) -> int:
    if report.error is not None:
        error_plain(report.error)
        return 1
    success(f"Schema OK (repo layer only): {report.label}")
    info("Host layers (global.yaml, host-local repo file) are not checked here.")
    for line in (*report.unchecked, *report.notices):
        warn_plain(line)
    return 2 if report.notices else 0


def validate_repo_config(path: Path | None) -> int:
    if path is None:
        from jailbee.git import repo_toplevel

        root = repo_toplevel(Path.cwd())
        path = root / '.jailbee' / 'config.yaml' if root else None
        if path is None or not path.is_file():
            error_plain('no .jailbee/config.yaml in this repository')
            return 1
    return report_repo_layer(validate_repo_layer(path))
