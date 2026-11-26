// One mapping from a container row's wire values to status chips (#167): the
// Incus state ("Running", "Frozen", "Stopped"), the dashboard core's row phase
// (booting, autostart, job, failed) and the agent state (`URGENCY`, with
// `error:<kind>`). `status-vocabulary.json` lists those values and is kept
// equal to the core's by tests/test_web_status_vocabulary.py. A value this
// mapping does not know becomes a neutral chip with its raw text.
import type {ContainerRow} from "../../protocol"
import type {StatusName} from "./StatusChip.vue"

export interface Status {
    status: StatusName | "unknown"
    label: string
    emphasis?: boolean
}

const STATES: Record<string, StatusName> = {Running: "running", Frozen: "frozen", Stopped: "stopped"}
const PHASES: Record<string, StatusName> = {booting: "booting", autostart: "autostart", job: "job", failed: "failed"}
const AGENTS: Record<string, {status: StatusName; label: string}> = {
    waiting: {status: "waiting", label: "waiting for you"},
    busy: {status: "busy", label: "working"},
    shell: {status: "shell", label: "shell"},
    idle: {status: "idle", label: "idle"},
}
const ERROR_PREFIX = "error:"

/** The container's chip: its row phase if it has one, else its Incus state. */
export function containerStatus(row: Pick<ContainerRow, "state" | "phase">): Status {
    if (row.phase !== null) {
        return Object.hasOwn(PHASES, row.phase)
            ? {status: PHASES[row.phase], label: row.phase}
            : {status: "unknown", label: row.phase}
    }
    return Object.hasOwn(STATES, row.state)
        ? {status: STATES[row.state], label: row.state.toLowerCase()}
        : {status: "unknown", label: row.state}
}

/** The agent's chip, or null without an agent. `error:<kind>` words its kind. */
export function agentStatus(agent: string | null): Status | null {
    if (agent === null) return null
    if (agent.startsWith(ERROR_PREFIX)) {
        return {status: "error", label: `API error: ${agent.slice(ERROR_PREFIX.length)}`}
    }
    return Object.hasOwn(AGENTS, agent) ? {...AGENTS[agent]} : {status: "unknown", label: agent}
}

/** List policy: phase first, otherwise stopped only; then agent and recent idle. */
export function listStatuses(row: Pick<ContainerRow, "state" | "phase" | "agent" | "agent_recent_idle">): Status[] {
    const chips: Status[] = []
    if (row.phase || row.state === "Stopped") chips.push(containerStatus(row))
    const agent = agentStatus(row.agent)
    if (agent && (row.agent !== "idle" || !row.agent_recent_idle)) chips.push(agent)
    if (row.agent_recent_idle) {
        chips.push({status: "idle", label: `${AGENTS.idle.label} ${row.agent_recent_idle}`, emphasis: true})
    }
    return chips
}

/** A session is marked only while live; reuse the running icon and tonal pair. */
export function sessionStatus(session: {live: boolean}): Status | null {
    return session.live ? {status: "running", label: "live"} : null
}

/** A row's chips: the container's, then the agent's when there is one. */
export function rowStatuses(row: Pick<ContainerRow, "state" | "phase" | "agent">): Status[] {
    const agent = agentStatus(row.agent)
    return agent ? [containerStatus(row), agent] : [containerStatus(row)]
}
