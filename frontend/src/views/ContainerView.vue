<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import {computed, nextTick, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import Button from "../components/ui/Button.vue"
import IconButton from "../components/ui/IconButton.vue"
import Field from "../components/ui/Field.vue"
import ListRow from "../components/ui/ListRow.vue"
import StatusChip from "../components/ui/StatusChip.vue"
import Switch from "../components/ui/Switch.vue"
import {useSessionList} from "../composables/useSessionList"
import {sessionStatus} from "../components/ui/status"
import {useDashboard} from "../dashboard"
import {usePager} from "../composables/usePager"
import type {ContainerSessionHistory, ContainerSessionList, ContainerSession} from "../protocol"

const readableTop = ref(0)
const pager = usePager(() => readableTop.value)
const route = useRoute()
const router = useRouter()
const {liveOnly, openSingleLive} = useSessionList()
const name = computed(() => String(route.params.name))
const dashboard = useDashboard()
const rows = computed(() => dashboard?.state.view?.repos.flatMap(repo => repo.containers) ?? [])
const info = computed(() => rows.value.find(row => row.name === name.value))
const children = computed(() => rows.value.filter(row => row.child?.orchestrator === name.value))
const live = ref<ContainerSessionList | null>(null)
const history = ref<ContainerSessionHistory | null>(null)
const repoPrefix = computed(() => live.value?.repo ?? history.value?.repo ?? dashboard?.state.view?.repos.find(repo => repo.containers.some(row => row.name === name.value))?.prefix)
const crumbs = computed(() => [
    {label: "JailBee", to: {name: "front"}},
    ...(repoPrefix.value ? [{label: repoPrefix.value, to: {name: "repo", params: {prefix: repoPrefix.value}}}] : []),
])
const historyPage = ref(0)
const search = ref("")
const historyQuery = ref("")
const historyTotal = ref(0)
const historyPageSize = 20
const historyPages = computed(() => Math.max(1, Math.ceil(historyTotal.value / historyPageSize)))

watch(search, (value, _previous, onCleanup) => {
    const timer = window.setTimeout(() => {
        historyPage.value = 0
        historyQuery.value = value.trim()
    }, 300)
    onCleanup(() => window.clearTimeout(timer))
})
const historyFailed = ref(false)
const error = ref<string | null>(null)
/** Live sessions first: they come from the snapshot and render at once. The
 * history is a request of its own and joins the list when it arrives. */
const sessions = computed(() => {
    if (liveOnly.value) return live.value?.sessions ?? []
    // The two answers come from different snapshots: a session that went live
    // in between is in both, and is shown once, as live.
    const shown = new Set((live.value?.sessions ?? []).map(s => `${s.agent}/${s.session_id}`))
    const earlier = (history.value?.sessions ?? []).filter(s => !shown.has(`${s.agent}/${s.session_id}`))
    return [...(live.value?.sessions ?? []), ...earlier]
})
const historyPending = computed(() => history.value === null && !historyFailed.value)

const dateFormat = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"})

function when(iso: string): string {
    return dateFormat.format(new Date(iso))
}

function sessionLinkLabel(session: ContainerSession): string {
    return [session.title || "Untitled session", sessionStatus(session)?.label, session.agent,
        session.last_activity ? when(session.last_activity) : null, session.models.join(", ")]
        .filter(Boolean).join(" ")
}

watch(name, async (selected, _previous, onCleanup) => {
    let current = true
    onCleanup(() => { current = false })
    live.value = null
    historyPage.value = 0
    search.value = ""
    historyQuery.value = ""
    historyTotal.value = 0
    error.value = null
    pager.page.value = 0
    window.scrollTo(0, 0)
    pager.measure()
    const base = `/api/containers/${encodeURIComponent(selected)}/sessions`
    const liveRequest = apiFetch<ContainerSessionList>(base)
    try {
        const result = await liveRequest
        if (!current) return
        live.value = result
        await openSingleLive(result.repo, result.sessions)
        await nextTick()
        pager.measure()
    } catch (e) {
        if (!current) return
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = "Container not found."
        } else {
            error.value = "The sessions could not be loaded."
        }
        return
    }
}, {immediate: true})

watch([name, historyPage, historyQuery, liveOnly], async ([selected, page, query, onlyLive], previous, onCleanup) => {
    const resetPosition = previous?.[0] === selected
    let current = true
    onCleanup(() => { current = false })
    history.value = null
    historyFailed.value = false
    if (onlyLive) return
    try {
        const params = new URLSearchParams({limit: String(historyPageSize), offset: String(page * historyPageSize)})
        if (query) params.set("q", query)
        const result = await apiFetch<ContainerSessionHistory>(
            `/api/containers/${encodeURIComponent(selected)}/sessions/history?${params}`,
        )
        if (!current) return
        historyTotal.value = result.total
        const lastPage = historyPages.value - 1
        if (page > lastPage) {
            // History can shrink between requests; fetch the last valid page.
            historyPage.value = lastPage
            return
        }
        history.value = result
        await nextTick()
        if (resetPosition) {
            pager.page.value = 0
            window.scrollTo(0, 0)
        }
        pager.measure()
    } catch (e) {
        if (!current) return
        if (e instanceof Unauthorized) await router.push({name: "login"})
        else historyFailed.value = true
    }
}, {immediate: true})
</script>

<template>
    <div>
        <TitleBar @readable-top="readableTop = $event" :title="name" :crumbs="crumbs" />
        <div class="page">
            <p v-if="info?.child" class="parent">
                Child of <router-link v-if="info.child.orchestrator" :to="{name: 'container', params: {name: info.child.orchestrator}}">{{ info.child.orchestrator_short_name }}</router-link><span v-else>{{ info.child.orchestrator_short_name }}</span>
                · {{ info.child.state }}<template v-if="info.child.runner"> · {{ info.child.runner }}</template>
                <template v-if="!rows.some(row => row.name === info?.child?.orchestrator)"> · orchestrator not listed</template>
            </p>
            <section v-if="info?.orchestrator || children.length" class="children" aria-label="Children">
                <h2>Children ({{ children.length }})</h2>
                <p v-if="!children.length" class="muted">No children.</p>
                <ul v-else>
                    <ListRow v-for="child in children" :key="child.name" wrap
                        :to="{name: 'container', params: {name: child.name}}"
                        :link-label="[child.cells[0] || child.name, child.child?.state ? `State: ${child.child.state}` : null,
                            child.child?.runner ? `Runner: ${child.child.runner}` : null].filter(Boolean).join(' ')">
                        <template #title>{{ child.cells[0] || child.name }}</template>
                        <template #secondary>
                            State: {{ child.child?.state }}<template v-if="child.child?.runner"> · Runner: {{ child.child.runner }}</template>
                        </template>
                    </ListRow>
                </ul>
            </section>
            <h2>Sessions</h2>
            <label class="session-filter">
                <Switch v-model="liveOnly" label="Live only" />
                <span>{{ liveOnly ? "Live only" : "All sessions" }}</span>
            </label>
            <p v-if="error" class="notice ui-notice">{{ error }}</p>
            <p v-else-if="live === null" class="muted">Loading…</p>
            <div v-else-if="liveOnly && sessions.length === 0">
                <p class="muted">No live sessions.</p>
                <Button variant="tonal" @click="liveOnly = false">Show all sessions</Button>
            </div>
            <p v-else-if="sessions.length === 0 && !historyPending && !historyFailed && !historyQuery" class="muted">No sessions.</p>
            <Field v-if="live !== null && !liveOnly" v-model="search" class="history-search"
                label="Search earlier sessions" type="search" placeholder="Title or session ID" />
            <p v-if="live !== null && !liveOnly && history?.total === 0 && historyQuery" class="muted" role="status">No matches.</p>
            <ul v-if="live !== null && sessions.length" class="sessions">
                <ListRow v-for="s in sessions" :key="`${s.agent}/${s.session_id}`"
                    class="session" :data-live="s.live" :link-label="sessionLinkLabel(s)" wrap
                    :to="{name: 'session', params: {prefix: live.repo, agent: s.agent, sessionId: s.session_id}}">
                    <template #title><span class="title">{{ s.title || "Untitled session" }}</span></template>
                    <template #trailing v-if="sessionStatus(s)">
                        <StatusChip v-bind="sessionStatus(s)!" />
                    </template>
                    <template #secondary>
                        <span class="meta">
                            <span class="agent">{{ s.agent }}</span>
                            <time v-if="s.last_activity" :datetime="s.last_activity">{{ when(s.last_activity) }}</time>
                            <span v-if="s.models.length" class="models">{{ s.models.join(", ") }}</span>
                        </span>
                    </template>
                </ListRow>
            </ul>
            <p v-if="live !== null && !liveOnly && historyPending" class="muted">Loading earlier sessions…</p>
            <p v-else-if="live !== null && !liveOnly && historyFailed" class="muted">Earlier sessions could not be loaded.</p>
            <nav v-if="live !== null && !liveOnly" class="history-nav" aria-label="Earlier sessions">
                <IconButton variant="tonal" icon="back" label="Previous earlier sessions" :disabled="historyPending || historyPage === 0" @click="historyPage--" />
                <span aria-live="polite">Page {{ historyPage + 1 }} of {{ historyPages }}</span>
                <IconButton variant="tonal" icon="forward" label="Next earlier sessions" :disabled="historyPending || historyFailed || historyPage + 1 >= historyPages" @click="historyPage++" />
            </nav>
            <PageNav :pager="pager" />
        </div>
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    padding: var(--md-space-16);
}
.parent,
.children {
    overflow-wrap: anywhere;
}
.parent a {
    color: inherit;
}
.muted {
    color: var(--md-on-surface-variant);
}
.session-filter {
    display: flex;
    align-items: center;
    gap: var(--md-space-8);
}
.history-search {
    margin-block: var(--md-space-16);
}
.history-nav {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-16);
    margin-block: var(--md-space-16);
}
.sessions,
.children ul {
    display: grid;
    gap: var(--md-space-8);
    list-style: none;
    margin: var(--md-space-16) 0 0;
    padding: 0;
}
.meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-4) var(--md-space-16);
    overflow-wrap: anywhere;
}
.models {
    font-family: var(--md-font-mono);
}
</style>
