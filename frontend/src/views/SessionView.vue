<script setup lang="ts">
// One agent session as a readable document. Every `html` field is rendered
// and sanitised by the server; it is inserted as is.
import {computed, nextTick, onMounted, ref} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import ToolCall from "../components/ToolCall.vue"
import type {
    RenderedEntry,
    RenderedSubagent,
    RenderedToolResult,
    TranscriptView,
} from "../protocol"
import {setShowThinking, showThinking} from "../settings"

const route = useRoute()
const router = useRouter()
const prefix = computed(() => String(route.params.prefix))
const agent = computed(() => String(route.params.agent))
const sessionId = computed(() => String(route.params.sessionId))
/** Set on the subagent route: this view then shows that subagent's transcript. */
const agentId = computed(() =>
    route.params.agentId === undefined ? null : String(route.params.agentId),
)
const sessionRef = computed(() => ({
    prefix: prefix.value,
    agent: agent.value,
    sessionId: sessionId.value,
}))

const transcript = ref<TranscriptView | null>(null)
const error = ref<string | null>(null)
const thinking = ref(showThinking())

function toggleThinking(event: Event) {
    thinking.value = (event.target as HTMLInputElement).checked
    setShowThinking(thinking.value)
}

/** Tool results and subagent refs, by the call they belong to. */
const byCall = computed(() => {
    const results = new Map<string, RenderedToolResult>()
    const subagents = new Map<string, RenderedSubagent[]>()
    const calls = new Set<string>()
    for (const e of transcript.value?.entries ?? []) {
        if (e.kind === "tool_call") calls.add(e.call_id)
        else if (e.kind === "tool_result") results.set(e.call_id, e)
        else if (e.kind === "subagent") {
            subagents.set(e.call_id, [...(subagents.get(e.call_id) ?? []), e])
        }
    }
    return {results, subagents, calls}
})

/** The entries shown in the flow: results and subagents fold into their call. */
const shown = computed<RenderedEntry[]>(() =>
    (transcript.value?.entries ?? []).filter((e) => {
        if (e.kind === "thinking") return thinking.value
        if (e.kind === "tool_result" || e.kind === "subagent") {
            return !byCall.value.calls.has(e.call_id)
        }
        return true
    }),
)

const latestAnswer = computed(() => {
    const entries = transcript.value?.entries ?? []
    for (let i = entries.length - 1; i >= 0; i--) {
        if (entries[i].kind === "assistant") return entries[i].id
    }
    return null
})

function entryDomId(id: string): string {
    return `entry-${id}`
}

function jumpToLatest() {
    if (latestAnswer.value === null) return
    document.getElementById(entryDomId(latestAnswer.value))?.scrollIntoView({block: "start"})
}

onMounted(async () => {
    const path = [
        prefix.value,
        "sessions",
        agent.value,
        sessionId.value,
        ...(agentId.value === null ? [] : ["subagents", agentId.value]),
    ]
        .map(encodeURIComponent)
        .join("/")
    try {
        transcript.value = await apiFetch<TranscriptView>(`/api/repos/${path}`)
        await nextTick()
    } catch (e) {
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = agentId.value === null ? "Session not found." : "Subagent not found."
        } else {
            error.value = "The session could not be loaded."
        }
    }
})
</script>

<template>
    <div class="page">
        <header class="bar">
            <router-link
                v-if="agentId !== null"
                class="back back-to-session"
                :to="{name: 'session', params: sessionRef}"
            >
                &larr; Parent session
            </router-link>
            <router-link v-else class="back" :to="{name: 'repo', params: {prefix}}">
                &larr; {{ prefix }}
            </router-link>
            <label class="toggle">
                <input
                    type="checkbox"
                    class="show-thinking"
                    :checked="thinking"
                    @change="toggleThinking"
                />
                Show thinking
            </label>
        </header>
        <p v-if="error" class="notice">{{ error }}</p>
        <p v-else-if="transcript === null" class="muted">Loading…</p>
        <template v-else>
            <h1>{{ transcript.title || (agentId === null ? "Untitled session" : "Subagent") }}</h1>
            <p class="session-meta muted">
                <span>{{ transcript.agent }}</span>
                <span v-if="transcript.models.length" class="mono">{{
                    transcript.models.join(", ")
                }}</span>
            </p>
            <p v-if="transcript.entries.length === 0" class="muted">This session is empty.</p>
            <article class="transcript">
                <template v-for="e in shown" :key="e.id">
                    <section
                        v-if="e.kind === 'user'"
                        :id="entryDomId(e.id)"
                        class="entry user"
                        :class="{meta: e.meta}"
                    >
                        <div class="prose" v-html="e.html" />
                    </section>
                    <section
                        v-else-if="e.kind === 'assistant'"
                        :id="entryDomId(e.id)"
                        class="entry assistant"
                    >
                        <div v-if="e.model" class="model">{{ e.model }}</div>
                        <div class="prose" v-html="e.html" />
                    </section>
                    <section
                        v-else-if="e.kind === 'thinking'"
                        :id="entryDomId(e.id)"
                        class="entry thinking"
                    >
                        <div class="label">Thinking</div>
                        <div v-if="e.html" class="prose" v-html="e.html" />
                        <p v-else class="muted">(redacted)</p>
                    </section>
                    <ToolCall
                        v-else-if="e.kind === 'tool_call'"
                        :id="entryDomId(e.id)"
                        :call="e"
                        :result="byCall.results.get(e.call_id) ?? null"
                        :subagents="byCall.subagents.get(e.call_id) ?? []"
                        :session-ref="sessionRef"
                    />
                    <section
                        v-else-if="e.kind === 'tool_result'"
                        :id="entryDomId(e.id)"
                        class="entry orphan"
                        v-html="e.html"
                    />
                    <section
                        v-else-if="e.kind === 'subagent'"
                        :id="entryDomId(e.id)"
                        class="entry orphan muted"
                    >
                        <router-link
                            :to="{name: 'subagent', params: {...sessionRef, agentId: e.agent_id}}"
                        >
                            Subagent: {{ e.description || e.agent_id }}
                        </router-link>
                    </section>
                </template>
            </article>
            <button
                v-if="latestAnswer"
                type="button"
                class="jump-latest"
                @click="jumpToLatest"
            >
                Latest answer &darr;
            </button>
        </template>
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    padding: var(--space-3) var(--space-3) 5rem;
}
.bar {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border);
}
.back {
    color: var(--muted);
    text-decoration: none;
    font-family: var(--font-mono);
}
.toggle {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 2.5rem;
    color: var(--muted);
    font-size: 0.9rem;
}
h1 {
    font-size: 1.4rem;
    line-height: 1.3;
    margin: var(--space-4) 0 var(--space-1);
    word-break: break-word;
}
.session-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-3);
    margin: 0 0 var(--space-4);
    font-size: 0.85rem;
}
.mono {
    font-family: var(--font-mono);
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
.entry {
    margin: var(--space-4) 0;
    scroll-margin-top: var(--space-3);
}
.entry.user {
    padding: var(--space-2) var(--space-3);
    background: var(--surface);
    border-left: 3px solid var(--accent);
    border-radius: var(--radius);
}
.entry.user.meta {
    border-left-color: var(--border-strong);
    background: none;
    color: var(--muted);
    font-size: 0.8rem;
}
.entry.thinking {
    padding-left: var(--space-3);
    border-left: 3px solid var(--border);
    color: var(--muted);
    font-style: italic;
}
.model,
.label {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--muted);
    margin-bottom: var(--space-1);
}
.entry.orphan :deep(pre) {
    white-space: pre-wrap;
    font-size: 0.8rem;
}

/* Rendered Markdown: comfortable line length and spacing. */
.prose {
    max-width: 72ch;
    line-height: 1.65;
    overflow-wrap: anywhere;
}
.prose :deep(> :first-child) {
    margin-top: 0;
}
.prose :deep(> :last-child) {
    margin-bottom: 0;
}
.prose :deep(p),
.prose :deep(ul),
.prose :deep(ol),
.prose :deep(table),
.prose :deep(blockquote) {
    margin: 0.75em 0;
}
.prose :deep(h1),
.prose :deep(h2),
.prose :deep(h3),
.prose :deep(h4) {
    line-height: 1.3;
    margin: 1.4em 0 0.5em;
}
.prose :deep(h1) {
    font-size: 1.3rem;
}
.prose :deep(h2) {
    font-size: 1.15rem;
}
.prose :deep(h3),
.prose :deep(h4) {
    font-size: 1rem;
}
.prose :deep(a) {
    color: var(--accent);
}
.prose :deep(code) {
    font-size: 0.875em;
    padding: 0.1em 0.3em;
    background: var(--bg);
    border-radius: 3px;
}
.prose :deep(pre) {
    margin: 0.75em 0;
    padding: var(--space-2) var(--space-3);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow-x: auto;
    line-height: 1.45;
    font-family: var(--font-mono);
    font-size: 0.85rem;
}
.prose :deep(pre code) {
    padding: 0;
    background: none;
    font-size: inherit;
}
.entry.user.meta .prose :deep(pre) {
    white-space: pre-wrap;
    border: none;
    padding: 0;
    background: none;
    margin: 0;
}
.prose :deep(table) {
    display: block;
    overflow-x: auto;
    border-collapse: collapse;
}
.prose :deep(th),
.prose :deep(td) {
    padding: var(--space-1) var(--space-2);
    border: 1px solid var(--border);
    text-align: left;
}
.prose :deep(blockquote) {
    padding-left: var(--space-3);
    border-left: 3px solid var(--border);
    color: var(--muted);
}
.jump-latest {
    position: fixed;
    right: var(--space-3);
    bottom: var(--space-3);
    min-height: 2.75rem;
    padding: var(--space-2) var(--space-3);
    font: inherit;
    color: var(--accent-text);
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    cursor: pointer;
}
</style>
