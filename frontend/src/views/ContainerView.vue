<script setup lang="ts">
import {computed, nextTick, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import {useDashboard} from "../dashboard"
import {usePager} from "../composables/usePager"
import type {ContainerSessionHistory, ContainerSessionList} from "../protocol"

const pager = usePager()
const route = useRoute()
const router = useRouter()
const name = computed(() => String(route.params.name))
const dashboard = useDashboard()
const rows = computed(() => dashboard?.state.view?.repos.flatMap(repo => repo.containers) ?? [])
const info = computed(() => rows.value.find(row => row.name === name.value))
const children = computed(() => rows.value.filter(row => row.child?.orchestrator === name.value))
const live = ref<ContainerSessionList | null>(null)
const history = ref<ContainerSessionHistory | null>(null)
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

watch([name, historyPage, historyQuery], async ([selected, page, query], _previous, onCleanup) => {
    let current = true
    onCleanup(() => { current = false })
    history.value = null
    historyFailed.value = false
    try {
        const params = new URLSearchParams({limit: String(historyPageSize), offset: String(page * historyPageSize)})
        if (query) params.set("q", query)
        const result = await apiFetch<ContainerSessionHistory>(
            `/api/containers/${encodeURIComponent(selected)}/sessions/history?${params}`,
        )
        if (!current) return
        history.value = result
        historyTotal.value = result.total
        await nextTick()
        pager.page.value = 0
        window.scrollTo(0, 0)
        pager.measure()
    } catch (e) {
        if (!current) return
        if (e instanceof Unauthorized) await router.push({name: "login"})
        else historyFailed.value = true
    }
}, {immediate: true})
</script>

<template>
    <div class="page">
        <header class="bar">
            <router-link class="back" :to="{name: 'front'}">&larr; Repositories</router-link>
            <h1>{{ name }}</h1>
        </header>
        <p v-if="info?.child" class="parent">
            Child of <router-link v-if="info.child.orchestrator" :to="{name: 'container', params: {name: info.child.orchestrator}}">{{ info.child.orchestrator_short_name }}</router-link><span v-else>{{ info.child.orchestrator_short_name }}</span>
            · {{ info.child.state }}<template v-if="info.child.runner"> · {{ info.child.runner }}</template>
            <template v-if="!rows.some(row => row.name === info?.child?.orchestrator)"> · orchestrator not listed</template>
        </p>
        <section v-if="info?.orchestrator || children.length" class="children" aria-label="Children">
            <h2>Children ({{ children.length }})</h2>
            <p v-if="!children.length" class="muted">No children.</p>
            <ul v-else>
                <li v-for="child in children" :key="child.name">
                    <router-link :to="{name: 'container', params: {name: child.name}}">{{ child.cells[0] || child.name }}</router-link>
                    · {{ child.child?.state }}<template v-if="child.child?.runner"> · {{ child.child.runner }}</template>
                </li>
            </ul>
        </section>
        <h2>Sessions</h2>
        <p v-if="error" class="notice">{{ error }}</p>
        <p v-else-if="live === null" class="muted">Loading…</p>
        <p v-else-if="sessions.length === 0 && !historyPending && !historyFailed && !historyQuery" class="muted">No sessions.</p>
        <label v-if="live !== null" class="history-search">
            Search earlier sessions
            <input v-model="search" type="search" placeholder="Title or session ID" />
        </label>
        <p v-if="live !== null && history?.total === 0 && historyQuery" class="muted" role="status">No matches.</p>
        <ul v-if="live !== null && sessions.length" class="sessions">
            <li
                v-for="s in sessions"
                :key="`${s.agent}/${s.session_id}`"
                class="session"
                :data-live="s.live"
            >
                <router-link
                    :to="{
                        name: 'session',
                        params: {prefix: live.repo, agent: s.agent, sessionId: s.session_id},
                    }"
                >
                    <span class="title">{{ s.title || "Untitled session" }}</span>
                    <span class="meta">
                        <span v-if="s.live" class="live">live</span>
                        <span class="agent">{{ s.agent }}</span>
                        <time v-if="s.last_activity" :datetime="s.last_activity">{{
                            when(s.last_activity)
                        }}</time>
                        <span v-if="s.models.length" class="models">{{ s.models.join(", ") }}</span>
                    </span>
                </router-link>
            </li>
        </ul>
        <p v-if="live !== null && historyPending" class="muted">Loading earlier sessions…</p>
        <p v-else-if="live !== null && historyFailed" class="muted">Earlier sessions could not be loaded.</p>
        <nav v-if="live !== null" class="history-nav" aria-label="Earlier sessions">
            <button aria-label="Previous earlier sessions" :disabled="historyPending || historyPage === 0" @click="historyPage--">Previous</button>
            <span aria-live="polite">Page {{ historyPage + 1 }} of {{ historyPages }}</span>
            <button aria-label="Next earlier sessions" :disabled="historyPending || historyFailed || historyPage + 1 >= historyPages" @click="historyPage++">Next</button>
        </nav>
        <PageNav :pager="pager" />
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    padding: var(--space-3);
}
.bar {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    overflow-wrap: anywhere;
    gap: var(--space-3);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border);
}
.bar h1 {
    font-size: 1.25rem;
    margin: 0;
    font-family: var(--font-mono);
}
.back {
    color: var(--muted);
    text-decoration: none;
}
.parent,
.children {
    overflow-wrap: anywhere;
}
.children li {
    margin-block: var(--space-2);
}
.parent a,
.children a {
    color: inherit;
}
.notice {
    margin: var(--space-3) 0;
    padding: var(--space-2) var(--space-3);
    background: var(--notice-bg);
    border-radius: var(--radius);
}
.muted {
    color: var(--muted);
}
.history-search {
    display: grid;
    gap: var(--space-1);
    margin-block: var(--space-3);
}
.history-search input {
    min-width: 0;
    width: 100%;
    box-sizing: border-box;
    min-height: 2.75rem;
    padding: var(--space-2);
    font: inherit;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
}
.history-nav {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin-block: var(--space-3);
}
.history-nav button {
    min-height: 2.75rem;
}
.sessions {
    list-style: none;
    margin: var(--space-3) 0 0;
    padding: 0;
}
.session a {
    display: block;
    min-height: 3.5rem; /* a large touch target */
    padding: var(--space-3);
    margin-bottom: var(--space-2);
    border: 1px solid var(--border);
    border-left-width: 4px;
    border-radius: var(--radius);
    background: var(--surface);
    color: inherit;
    text-decoration: none;
}
.session[data-live="true"] a {
    border-left-color: var(--accent);
}
.title {
    display: block;
    word-break: break-word;
}
.meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    margin-top: var(--space-1);
    font-size: 0.85rem;
    color: var(--muted);
}
.live {
    font-weight: 700;
    color: var(--accent);
}
.models {
    font-family: var(--font-mono);
}
</style>
