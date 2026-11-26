<script setup lang="ts">
import {computed} from "vue"
import type {FrontPageView} from "../protocol"
import {settings, updateSettings} from "../settings"
import Button from "./ui/Button.vue"
import ListRow from "./ui/ListRow.vue"
import StatusChip from "./ui/StatusChip.vue"
import {agentStatus, listStatuses} from "./ui/status"

const props = withDefaults(defineProps<{
    view: FrontPageView
    showHeadings?: boolean
    listedContainers?: string[]
}>(), {showHeadings: true})
const listed = computed(() => new Set(props.listedContainers ?? props.view.repos.flatMap(
    repo => repo.containers.map(row => row.name),
)))

const selection = computed(() => settings.containerFilter === "all" ? null
    : props.view.filters?.[settings.containerFilter] ?? {visible: [], context: []})
const context = computed(() => new Set(selection.value?.context ?? []))
const repos = computed(() => {
    if (!selection.value) return props.view.repos
    const visible = new Set(selection.value.visible)
    return props.view.repos.map(repo => ({...repo,
        containers: repo.containers.filter(row => visible.has(row.name)).map(row => ({...row,
            indent: selection.value?.indents?.[row.name] ?? row.indent,
        })),
    })).filter(repo => repo.containers.length > 0)
})
</script>

<template>
    <p v-if="settings.containerFilter !== 'all'" class="filter-notice muted" role="status">Showing: {{ settings.containerFilter === 'active' ? 'Active' : 'Running' }} · <Button variant="text" @click="updateSettings({containerFilter: 'all'})">show all</Button></p>
    <p v-if="repos.length === 0" class="muted">{{ settings.containerFilter === 'all' ? 'No repositories.' : 'No matching containers.' }}</p>
    <section v-for="repo in repos" :key="repo.prefix" class="repo" :data-repo="repo.prefix">
        <h2 v-if="showHeadings" class="repo-heading">
            <router-link :to="{name: 'repo', params: {prefix: repo.prefix}}">{{ repo.prefix }}</router-link>
        </h2>
        <p v-if="repo.containers.length === 0" class="muted empty">No containers.</p>
        <ul v-else class="rows">
            <ListRow v-for="c in repo.containers" :key="c.name" wrap
                :to="{name: 'container', params: {name: c.name}}"
                :context="context.has(c.name)" class="row" :class="{nested: c.depth > 0}"
                :data-container="c.name" :data-state="c.state" :data-phase="c.phase ?? undefined"
                :data-agent="c.agent ? agentStatus(c.agent)!.status : undefined">
                <template #title><span class="name"><span class="indent">{{ c.indent }}</span>{{ c.cells[0] }}</span></template>
                <template #trailing>
                    <div class="statuses">
                        <StatusChip v-for="(chip, index) in listStatuses(c)" :key="index" v-bind="chip" />
                    </div>
                </template>
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
            </ListRow>
        </ul>
    </section>
</template>

<style scoped>
.filter-notice { margin: var(--md-space-8) 0 0; font-size: var(--md-type-body-medium-size); }
.repo { margin: var(--md-space-24) 0; }
.repo-heading {
    font-size: var(--md-type-title-medium-size);
    line-height: var(--md-type-title-medium-line-height);
    font-weight: var(--md-type-title-medium-weight);
    margin: 0 0 var(--md-space-8);
    color: var(--md-on-surface-variant);
}
.repo-heading a { color: inherit; text-decoration: none; }
.repo-heading a:hover { text-decoration: underline; }
.muted { color: var(--md-on-surface-variant); }
.rows { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--md-space-8); }
.row.nested { margin-left: var(--md-space-16); }
.statuses { display: flex; flex-wrap: wrap; gap: var(--md-space-8); }
.orchestration { margin: var(--md-space-8) 0 0; overflow-wrap: anywhere; }
.orchestration a { color: inherit; }
.name, .indent { font-family: var(--md-font-mono); }
.indent { white-space: pre; color: var(--md-on-surface-variant); }
.cells { display: flex; flex-wrap: wrap; gap: var(--md-space-4) var(--md-space-16); margin: var(--md-space-8) 0 0; }
.cell { display: flex; gap: var(--md-space-4); max-width: 100%; }
dt { color: var(--md-on-surface-variant); flex-shrink: 0; }
dd { margin: 0; font-family: var(--md-font-mono); overflow-wrap: anywhere; min-width: 0; }
dd pre { margin: 0; font: inherit; white-space: pre; overflow: hidden; text-overflow: ellipsis; }
</style>
