<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import {computed, nextTick, onMounted, ref} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import RepoList from "../components/RepoList.vue"
import {useDashboard} from "../dashboard"
import {usePager} from "../composables/usePager"
import type {SessionList, SessionSummary} from "../protocol"

const pager = usePager()
const route = useRoute()
const router = useRouter()
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
const error = ref<string | null>(null)

const dateFormat = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"})

function when(iso: string): string {
    return dateFormat.format(new Date(iso))
}

onMounted(async () => {
    try {
        const list = await apiFetch<SessionList>(
            `/api/repos/${encodeURIComponent(prefix.value)}/sessions`,
        )
        sessions.value = list.sessions
        await nextTick()
        pager.measure()
    } catch (e) {
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = "Repository not found."
        } else {
            error.value = "The sessions could not be loaded."
        }
    }
})
</script>

<template>
    <div class="page">
        <TitleBar :title="prefix" :crumbs="[{label: 'JailBee', to: {name: 'front'}}]" context="containers" />
        <section v-if="containers && containers.repos.length" aria-label="Containers">
            <h2>Containers</h2>
            <RepoList :view="containers" :show-headings="false" :listed-containers="listedContainers" />
        </section>
        <h2>Sessions</h2>
        <p v-if="error" class="notice">{{ error }}</p>
        <p v-else-if="sessions === null" class="muted">Loading…</p>
        <p v-else-if="sessions.length === 0" class="muted">No sessions.</p>
        <ul v-else class="sessions">
            <li v-for="s in sessions" :key="`${s.agent}/${s.session_id}`" class="session">
                <router-link
                    :to="{
                        name: 'session',
                        params: {prefix, agent: s.agent, sessionId: s.session_id},
                    }"
                >
                    <span class="title">{{ s.title || "Untitled session" }}</span>
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
    border-radius: var(--radius);
    background: var(--surface);
    color: inherit;
    text-decoration: none;
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
.models {
    font-family: var(--font-mono);
}
.container:not(.unattributed) {
    font-family: var(--font-mono);
}
.unattributed {
    font-style: italic;
}
</style>
