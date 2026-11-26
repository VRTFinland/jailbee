<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import {computed, nextTick, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import RepoList from "../components/RepoList.vue"
import ListRow from "../components/ui/ListRow.vue"
import Button from "../components/ui/Button.vue"
import Switch from "../components/ui/Switch.vue"
import StatusChip from "../components/ui/StatusChip.vue"
import {sessionStatus} from "../components/ui/status"
import {useSessionList} from "../composables/useSessionList"
import {useDashboard} from "../dashboard"
import {usePager} from "../composables/usePager"
import type {SessionList, SessionSummary} from "../protocol"

const readableTop = ref(0)
const pager = usePager(() => readableTop.value)
const route = useRoute()
const router = useRouter()
const {liveOnly, openSingleLive} = useSessionList()
const prefix = computed(() => String(route.params.prefix))
const dashboard = useDashboard()
const containers = computed(() => {
    const view = dashboard?.state.view
    return view ? {...view, repos: view.repos.filter(repo => repo.prefix === prefix.value)} : null
})
const listedContainers = computed(() => dashboard?.state.view?.repos.flatMap(
    repo => repo.containers.map(row => row.name),
) ?? [])
const sessions = ref<SessionSummary[] | null>(null)
const filteredSessions = computed(() => sessions.value?.filter(s => !liveOnly.value || s.live) ?? [])
const error = ref<string | null>(null)

const dateFormat = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"})

function when(iso: string): string {
    return dateFormat.format(new Date(iso))
}

function sessionLinkLabel(session: SessionSummary): string {
    return [session.title || "Untitled session", session.agent,
        session.container ?? "unattributed", session.last_activity ? when(session.last_activity) : null,
        session.models.join(", ")]
        .filter(Boolean).join(" ")
}

watch(prefix, async (selected, _previous, onCleanup) => {
    let current = true
    onCleanup(() => { current = false })
    sessions.value = null
    error.value = null
    try {
        const list = await apiFetch<SessionList>(
            `/api/repos/${encodeURIComponent(selected)}/sessions`,
        )
        if (!current) return
        sessions.value = list.sessions
        await openSingleLive(selected, list.sessions.filter(s => s.live))
        await nextTick()
        pager.measure()
    } catch (e) {
        if (!current) return
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = "Repository not found."
        } else {
            error.value = "The sessions could not be loaded."
        }
    }
}, {immediate: true})
</script>

<template>
    <div class="page">
        <TitleBar @readable-top="readableTop = $event" :title="prefix" :crumbs="[{label: 'JailBee', to: {name: 'front'}}]" context="containers" />
        <section v-if="containers && containers.repos.length" aria-label="Containers">
            <h2>Containers</h2>
            <RepoList :view="containers" :show-headings="false" :listed-containers="listedContainers" />
        </section>
        <h2>Sessions</h2>
        <label class="session-filter">
            <Switch v-model="liveOnly" label="Live only" />
            <span>{{ liveOnly ? "Live only" : "All sessions" }}</span>
        </label>
        <p v-if="error" class="notice ui-notice">{{ error }}</p>
        <p v-else-if="sessions === null" class="muted">Loading…</p>
        <div v-else-if="liveOnly && filteredSessions.length === 0">
            <p class="muted">No live sessions.</p>
            <Button variant="tonal" @click="liveOnly = false">Show all sessions</Button>
        </div>
        <p v-else-if="sessions.length === 0" class="muted">No sessions.</p>
        <ul v-else class="sessions">
            <ListRow v-for="s in filteredSessions" :key="`${s.agent}/${s.session_id}`" class="session" wrap
                :link-label="sessionLinkLabel(s)" :title="s.title || 'Untitled session'"
                :to="{name: 'session', params: {prefix, agent: s.agent, sessionId: s.session_id}}">
                <template #trailing v-if="sessionStatus(s)"><StatusChip v-bind="sessionStatus(s)!" /></template>
                <template #secondary>
                    <span class="meta">
                        <span class="agent">{{ s.agent }}</span>
                        <span class="container" :class="{unattributed: !s.container}">{{
                            s.container ?? "unattributed"
                        }}</span>
                        <time v-if="s.last_activity" :datetime="s.last_activity">{{
                            when(s.last_activity)
                        }}</time>
                        <span v-if="s.models.length" class="models">{{ s.models.join(", ") }}</span>
                    </span>
                </template>
            </ListRow>
        </ul>
        <PageNav :pager="pager" />
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    padding: var(--md-space-16);
}
h2 {
    font-size: var(--md-type-title-medium-size);
    line-height: var(--md-type-title-medium-line-height);
    font-weight: var(--md-type-title-medium-weight);
}
.muted {
    color: var(--md-on-surface-variant);
}
.session-filter {
    display: flex;
    align-items: center;
    gap: var(--md-space-8);
}
.sessions {
    list-style: none;
    margin: var(--md-space-16) 0 0;
    padding: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--md-space-8);
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
.container:not(.unattributed) {
    font-family: var(--md-font-mono);
}
.unattributed {
    font-style: italic;
}
</style>
