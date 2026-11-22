<script setup lang="ts">
import type {FrontPageView} from "../protocol"

defineProps<{view: FrontPageView}>()

const AGENT_LABELS: Record<string, string> = {
    waiting: "waiting for you",
    busy: "working",
    idle: "idle",
    shell: "shell",
}

function agentLabel(state: string): string {
    return AGENT_LABELS[state] ?? state
}
</script>

<template>
    <p v-if="view.repos.length === 0" class="muted">No repositories.</p>
    <section v-for="repo in view.repos" :key="repo.prefix" class="repo" :data-repo="repo.prefix">
        <h2 class="repo-heading">
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
                :data-container="c.name"
                :data-agent="c.agent ?? undefined"
                :data-state="c.state"
                :data-phase="c.phase ?? undefined"
            >
                <div class="main">
                    <router-link class="name" :to="{name: 'container', params: {name: c.name}}"
                        ><span class="indent">{{ c.indent }}</span
                        >{{ c.cells[0] }}</router-link
                    >
                    <span v-if="c.phase" class="phase">{{ c.phase }}</span>
                    <span v-if="c.agent" class="agent" :class="`agent-${c.agent}`">{{
                        agentLabel(c.agent)
                    }}</span>
                </div>
                <dl class="cells">
                    <template v-for="(col, i) in view.columns" :key="col.name">
                        <div v-if="i > 0 && c.cells[i] !== ''" class="cell">
                            <dt>{{ col.header }}</dt>
                            <dd>{{ c.cells[i] }}</dd>
                        </div>
                    </template>
                </dl>
            </li>
        </ul>
    </section>
</template>

<style scoped>
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
.row[data-agent="waiting"] {
    border-left-color: var(--accent);
}
/* Row phase: the dashboard core's phases (the terminal dashboard's palette),
   mapped onto each theme's --phase-* tokens. Stopped is dim, as in the TUI. */
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
/* E-ink has no colour: phases differ by border-left style, Stopped by a
   dotted outline instead of a fade (greys repaint badly). */
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
    border-style: dotted;
}
.main {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
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
}
dt {
    color: var(--muted);
}
dd {
    margin: 0;
    font-family: var(--font-mono);
}
</style>
