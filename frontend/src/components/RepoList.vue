<script setup lang="ts">
import {computed} from "vue"
import type {ContainerFilter, FrontPageView} from "../protocol"
import {settings, updateSettings} from "../settings"

const props = withDefaults(defineProps<{
    view: FrontPageView
    showHeadings?: boolean
    listedContainers?: string[]
}>(), {showHeadings: true})
const listed = computed(() => new Set(props.listedContainers ?? props.view.repos.flatMap(
    repo => repo.containers.map(row => row.name),
)))

const filterButtons: {mode: ContainerFilter, label: string, path: string}[] = [
    {mode: "all", label: "All", path: "M3 4h14M3 10h14M3 16h14"},
    {mode: "running", label: "Running", path: "m6 3 10 7-10 7Z"},
    {mode: "active", label: "Active", path: "M1 10h4l3-7 4 14 3-7h4"},
]
const selection = computed(() => settings.containerFilter === "all" ? null
    : props.view.filters?.[settings.containerFilter] ?? {visible: [], context: []})
const context = computed(() => new Set(selection.value?.context ?? []))
const repos = computed(() => {
    if (!selection.value) return props.view.repos
    const visible = new Set(selection.value.visible)
    return props.view.repos.map(repo => ({...repo,
        containers: repo.containers.filter(row => visible.has(row.name)),
    })).filter(repo => repo.containers.length > 0)
})
const hidden = computed(() => props.view.repos.reduce((n, r) => n + r.containers.length, 0)
    - repos.value.reduce((n, r) => n + r.containers.length, 0))

const AGENT_LABELS: Record<string, string> = {
    waiting: "waiting for you",
    busy: "working",
    idle: "idle",
    shell: "shell",
}

const ERROR_PREFIX = "error:"

/** `error:<kind>` states share one style; the kind is only worded. */
function agentFamily(state: string): string {
    return state.startsWith(ERROR_PREFIX) ? "error" : state
}

function agentLabel(state: string): string {
    if (state.startsWith(ERROR_PREFIX)) {
        return `API error: ${state.slice(ERROR_PREFIX.length)}`
    }
    return AGENT_LABELS[state] ?? state
}
</script>

<template>
    <div class="filter-bar">
        <div class="filters" role="group" aria-label="Container filter">
            <button v-for="filter in filterButtons" :key="filter.mode" type="button"
                :aria-label="filter.label" :title="filter.label"
                :aria-pressed="settings.containerFilter === filter.mode"
                @click="updateSettings({containerFilter: filter.mode})">
                <svg viewBox="0 0 20 20" aria-hidden="true"><path :d="filter.path" /></svg>
            </button>
        </div>
        <span v-if="settings.containerFilter !== 'all'" class="muted" role="status">{{ hidden }} hidden by filter</span>
    </div>
    <p v-if="repos.length === 0" class="muted">{{ settings.containerFilter === 'all' ? 'No repositories.' : 'No matching containers.' }}</p>
    <section v-for="repo in repos" :key="repo.prefix" class="repo" :data-repo="repo.prefix">
        <h2 v-if="showHeadings" class="repo-heading">
            <router-link :to="{name: 'repo', params: {prefix: repo.prefix}}">{{
                repo.prefix
            }}</router-link>
        </h2>
        <p v-if="repo.containers.length === 0" class="muted empty">No containers.</p>
        <ul v-else class="rows">
            <li
                v-for="c in repo.containers"
                :key="c.name"
                class="row"
                :class="{nested: c.depth > 0, 'filter-context': context.has(c.name)}"
                :data-container="c.name"
                :data-agent="c.agent ? agentFamily(c.agent) : undefined"
                :data-state="c.state"
                :data-phase="c.phase ?? undefined"
            >
                <div class="main">
                    <router-link class="name" :to="{name: 'container', params: {name: c.name}}"
                        ><span class="indent">{{ c.indent }}</span
                        >{{ c.cells[0] }}</router-link
                    >
                    <span v-if="c.phase" class="phase">{{ c.phase }}</span>
                    <span v-else-if="c.state === 'Stopped'" class="phase">Stopped</span>
                    <span v-if="c.agent" class="agent" :class="`agent-${agentFamily(c.agent)}`">{{
                        agentLabel(c.agent)
                    }}</span>
                </div>
                <p v-if="c.orchestrator || c.child_count" class="orchestration">
                    Orchestrator · {{ c.child_count }} {{ c.child_count === 1 ? 'child' : 'children' }}
                </p>
                <p v-if="c.child" class="orchestration">
                    Child of <router-link v-if="c.child.orchestrator" :to="{name: 'container', params: {name: c.child.orchestrator}}">{{ c.child.orchestrator_short_name }}</router-link><span v-else>{{ c.child.orchestrator_short_name }}</span>
                    · {{ c.child.state }}<template v-if="c.child.runner"> · {{ c.child.runner }}</template>
                    <template v-if="!listed.has(c.child.orchestrator ?? '')"> · orchestrator not listed</template>
                </p>
                <dl class="cells">
                    <template v-for="(col, i) in view.columns" :key="col.name">
                        <div v-if="i > 0 && c.cells[i] !== ''" class="cell">
                            <dt>{{ col.header }}</dt>
                            <dd><pre v-if="col.name === 'sync'">{{ c.cells[i] }}</pre><template v-else>{{ c.cells[i] }}</template></dd>
                        </div>
                    </template>
                </dl>
            </li>
        </ul>
    </section>
</template>

<style scoped>
.filter-bar, .filters {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
}
.filter-bar {
    margin-top: var(--space-3);
}
.filters button {
    display: grid;
    place-items: center;
    min-width: 2.75rem;
    min-height: 2.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    background: var(--surface);
}
.filters button[aria-pressed="true"] {
    outline: 2px solid var(--accent);
    outline-offset: -3px;
    background: var(--notice-bg);
}
.filters svg {
    width: 1.1rem;
    height: 1.1rem;
    fill: none;
    stroke: currentColor;
    stroke-width: 2;
    stroke-linejoin: round;
}
.row.filter-context[data-container] {
    opacity: 0.5;
}
.repo {
    margin: var(--space-4) 0;
}
.repo-heading {
    font-size: 1rem;
    margin: 0 0 var(--space-2);
    color: var(--muted);
    letter-spacing: 0.04em;
}
.repo-heading a {
    color: inherit;
    text-decoration: none;
}
.repo-heading a:hover {
    text-decoration: underline;
}
.muted {
    color: var(--muted);
}
.rows {
    list-style: none;
    margin: 0;
    padding: 0;
}
.row {
    min-height: 3.5rem; /* a large touch target */
    padding: var(--space-3);
    border: 1px solid var(--border);
    border-left-width: 4px;
    border-radius: var(--radius);
    background: var(--surface);
    margin-bottom: var(--space-2);
}
.row.nested {
    margin-left: var(--space-3);
}
.orchestration {
    margin: var(--space-2) 0 0;
    font-size: 0.85rem;
    overflow-wrap: anywhere;
}
.orchestration a {
    color: inherit;
}
.row[data-agent="waiting"] {
    border-left-color: var(--accent);
}
.row[data-agent="error"] {
    border-left-color: var(--danger);
}
/* Row phase: the dashboard core's phases (the terminal dashboard's palette),
   mapped onto each theme's --phase-* tokens. Stopped is dim, as in the TUI.
   Same specificity as the waiting-agent edge above and later, so a phase wins. */
.row[data-phase="booting"] {
    border-left-color: var(--phase-booting);
}
.row[data-phase="autostart"] {
    border-left-color: var(--phase-autostart);
}
.row[data-phase="job"] {
    border-left-color: var(--phase-job);
}
.row[data-phase="failed"] {
    border-left-color: var(--phase-failed);
}
.row[data-state="Stopped"] {
    opacity: 0.6;
}
.phase {
    font-size: 0.85rem;
    white-space: nowrap;
    margin-left: auto;
}
.row[data-phase="booting"] .phase {
    color: var(--phase-booting);
}
.row[data-phase="autostart"] .phase {
    color: var(--phase-autostart);
}
.row[data-phase="job"] .phase {
    color: var(--phase-job);
}
.row[data-phase="failed"] .phase {
    color: var(--phase-failed);
    font-weight: 700;
}
/* E-ink has no colour: phases differ by border-left style, Stopped by an open
   top/right/bottom outline instead of a fade (greys repaint badly); phases only
   style the left edge, so the two cannot be confused. */
:root[data-theme="eink"] .row[data-phase="booting"] {
    border-left-style: dashed;
}
:root[data-theme="eink"] .row[data-phase="autostart"] {
    border-left-style: dotted;
}
:root[data-theme="eink"] .row[data-phase="job"] {
    border-left-style: double;
}
:root[data-theme="eink"] .row[data-phase="failed"] {
    border-left-width: 8px;
}
:root[data-theme="eink"] .row[data-state="Stopped"] {
    opacity: 1;
    border-top-style: none;
    border-right-style: none;
    border-bottom-style: none;
}
.main {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--space-3);
}
.name {
    font-family: var(--font-mono);
    font-size: 1.1rem;
    word-break: break-all;
}
a.name {
    color: inherit;
    text-decoration: none;
}
a.name:hover {
    text-decoration: underline;
}
.indent {
    white-space: pre;
    color: var(--muted);
}
.agent {
    font-size: 0.85rem;
    white-space: nowrap;
}
.agent-waiting {
    font-weight: 700;
    color: var(--accent);
}
.agent-error {
    font-weight: 700;
    color: var(--danger);
}
.agent-busy,
.agent-idle,
.agent-shell {
    color: var(--muted);
}
.cells {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    margin: var(--space-2) 0 0;
    font-size: 0.85rem;
}
.cell {
    display: flex;
    gap: var(--space-1);
    max-width: 100%;
}
dt {
    color: var(--muted);
    flex-shrink: 0;
}
dd {
    margin: 0;
    font-family: var(--font-mono);
    overflow-wrap: anywhere;
    min-width: 0;
}
dd pre {
    margin: 0;
    font: inherit;
    white-space: pre;
    overflow: hidden;
    text-overflow: ellipsis;
}
</style>
