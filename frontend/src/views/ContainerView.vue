<script setup lang="ts">
import {computed, nextTick, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import {useDashboard} from "../dashboard"
import {usePager} from "../composables/usePager"
import type {ContainerSessionList} from "../protocol"

const pager = usePager()
const route = useRoute()
const router = useRouter()
const name = computed(() => String(route.params.name))
const dashboard = useDashboard()
const rows = computed(() => dashboard?.state.view?.repos.flatMap(repo => repo.containers) ?? [])
const info = computed(() => rows.value.find(row => row.name === name.value))
const children = computed(() => rows.value.filter(row => row.child?.orchestrator === name.value))
const list = ref<ContainerSessionList | null>(null)
const error = ref<string | null>(null)

const dateFormat = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"})

function when(iso: string): string {
    return dateFormat.format(new Date(iso))
}

watch(name, async (selected, _previous, onCleanup) => {
    let current = true
    onCleanup(() => { current = false })
    list.value = null
    error.value = null
    await nextTick()
    if (!current) return
    pager.page.value = 0
    window.scrollTo(0, 0)
    pager.measure()
    try {
        const result = await apiFetch<ContainerSessionList>(
            `/api/containers/${encodeURIComponent(selected)}/sessions`,
        )
        if (!current) return
        list.value = result
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
        <p v-else-if="list === null" class="muted">Loading…</p>
        <p v-else-if="list.sessions.length === 0" class="muted">No sessions.</p>
        <ul v-else class="sessions">
            <li
                v-for="s in list.sessions"
                :key="`${s.agent}/${s.session_id}`"
                class="session"
                :data-live="s.live"
            >
                <router-link
                    :to="{
                        name: 'session',
                        params: {prefix: list.repo, agent: s.agent, sessionId: s.session_id},
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
