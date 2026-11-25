"""The browser's status vocabulary mirrors the values the dashboard core sends.

`frontend/src/components/ui/status-vocabulary.json` is what the web UI's
status mapping (`status.ts`) and its Vitest coverage test are built from; it
must list every container state, row phase and agent state the core can put
on the wire, so a new value fails here instead of reaching a browser unmapped.
"""

from __future__ import annotations

import json
from pathlib import Path

from jailbee.agent_status import URGENCY
from jailbee.dashboard.format import ROW_PHASE_STYLES
from jailbee.lifecycle import STATE_SORT_RANK

VOCABULARY = (
    Path(__file__).parents[1] / "frontend" / "src" / "components" / "ui" / "status-vocabulary.json"
)


def test_committed_status_vocabulary_matches_the_dashboard_core() -> None:
    expected = {
        "states": list(STATE_SORT_RANK),
        "phases": list(ROW_PHASE_STYLES),
        "agents": list(URGENCY),
    }
    assert json.loads(VOCABULARY.read_text()) == expected, (
        f"update {VOCABULARY.name} to:\n{json.dumps(expected, indent=4)}"
    )
