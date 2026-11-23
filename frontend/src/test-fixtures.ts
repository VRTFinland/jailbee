import type {FrontPageView} from "./protocol"

/** A deliberately out-of-peer-order orchestration snapshot from the web protocol. */
export function orchestrationView(): FrontPageView {
    const base = {indent: "", agent: null, state: "Running", phase: null,
        depth: 0, orchestrator: false, child_count: 0, child: null}
    return {
        seq: 1, gathered_at: "2026-11-21T12:00:00Z",
        columns: [{name: "name", header: "NAME"}],
        repos: [{prefix: "alpha", containers: [
            {...base, name: "alpha-orch", cells: ["orch"], orchestrator: true, child_count: 2},
            {...base, name: "alpha-task-a", cells: ["task-a"], depth: 1, indent: "└ ",
                child: {orchestrator: "alpha-orch", orchestrator_short_name: "orch", state: "working", runner: "codex/opus"}},
            {...base, name: "alpha-task-b", cells: ["task-b"], depth: 1, indent: "└ ",
                child: {orchestrator: "alpha-orch", orchestrator_short_name: "orch", state: "done", runner: null}},
        ]}],
    }
}
