<script setup lang="ts">
import {computed, nextTick, onMounted, ref} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import {usePager} from "../composables/usePager"
import type {ContainerSessionList} from "../protocol"

const pager = usePager()
const route = useRoute()
const router = useRouter()
const name = computed(() => String(route.params.name))
const list = ref<ContainerSessionList | null>(null)
const error = ref<string | null>(null)

const dateFormat = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"})

function when(iso: string): string {
    return dateFormat.format(new Date(iso))
}

onMounted(async () => {
    try {
        list.value = await apiFetch<ContainerSessionList>(
            `/api/containers/${encodeURIComponent(name.value)}/sessions`,
        )
        await nextTick()
        pager.measure()
    } catch (e) {
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = "Container not found."
        } else {
            error.value = "The sessions could not be loaded."
        }
    }
})
</script>

<template>
    <div class="page">
        <header class="bar">
            <router-link class="back" :to="{name: 'front'}">&larr; Repositories</router-link>
            <h1>{{ name }}</h1>
        </header>
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
