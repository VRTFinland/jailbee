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
        <!-- Hook: ticket 04 turns this heading into a link to the repo view. -->
        <h2 class="repo-heading">{{ repo.prefix }}</h2>
        <p v-if="repo.containers.length === 0" class="muted empty">No containers.</p>
        <ul v-else class="rows">
            <!-- Hook: ticket 08 turns each row's content into a link to the container view. -->
            <li
                v-for="c in repo.containers"
                :key="c.name"
                class="row"
                :data-container="c.name"
                :data-agent="c.agent ?? undefined"
            >
                <div class="main">
                    <span class="name"
                        ><span class="indent">{{ c.indent }}</span
                        >{{ c.cells[0] }}</span
                    >
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
