<script setup lang="ts">
// One agent session as a readable document. Every `html` field is rendered
// and sanitised by the server; it is inserted as is.
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import ToolCall from "../components/ToolCall.vue"
import {readableBottom} from "../composables/readableBottom"
import {usePager} from "../composables/usePager"
import {useDashboard} from "../dashboard"
import type {
    EntryContent,
    RenderedEntry,
    RenderedSubagent,
    RenderedToolResult,
    TranscriptView,
} from "../protocol"
import {settings} from "../settings"

const header = ref<HTMLElement | null>(null)
// The sticky header's bottom edge: below an app banner, not just its height.
const readableTop = ref(0)
const transcriptEl = ref<HTMLElement | null>(null)
const pager = usePager(
    () => readableTop.value,
    // The bottom spacer lets a short final reply start below the header; page
    // turns end where the transcript does, not inside that blank room.
    () => transcriptEl.value
        ? document.documentElement.scrollHeight - (transcriptEl.value.getBoundingClientRect().bottom + window.scrollY)
        : 0,
)
const eink = computed(() => settings.displayProfile === "eink")
/** E-ink: live entries wait here until the reader taps the notice. */
const pending = ref<RenderedEntry[]>([])

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
const thinking = computed(() => settings.showThinking)
const title = computed(() => transcript.value?.title || (agentId.value === null ? "Untitled session" : "Subagent"))

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

const atTranscriptEnd = ref(false)
const latestVisible = ref(false)
const unseen = ref<RenderedEntry | null>(null)
const autoFollow = computed(() => !eink.value || settings.einkFollow)
const jumpLabel = computed(() => {
    if (pending.value.length || unseen.value) return "New messages ↓"
    if (atTranscriptEnd.value) return null
    if (latestAnswer.value && !latestVisible.value) return "Latest answer ↓"
    return "Scroll to bottom ↓"
})

function measureVisibility() {
    const headerRect = header.value?.getBoundingClientRect()
    readableTop.value = headerRect ? Math.max(0, headerRect.bottom) : 0
    const content = transcriptEl.value?.getBoundingClientRect()
    atTranscriptEnd.value = !!content && content.bottom <= readableBottom() + 1
    if (atTranscriptEnd.value && pending.value.length === 0) unseen.value = null
    const el = latestAnswer.value === null ? null : document.getElementById(entryDomId(latestAnswer.value))
    if (el === null) {
        latestVisible.value = false
        return
    }
    const rect = el.getBoundingClientRect()
    const top = Math.max(0, headerRect?.bottom ?? 0)
    const bottom = readableBottom()
    // Any visible part counts, including a reply taller than the viewport.
    latestVisible.value = rect.bottom > top && rect.top < bottom && rect.bottom > rect.top
}

watch(shown, () => void nextTick(measureVisibility))
watch(eink, () => void nextTick(measureVisibility))

let layoutObserver: ResizeObserver | null = null
onMounted(() => {
    window.addEventListener("scroll", measureVisibility, {passive: true})
    window.addEventListener("resize", measureVisibility)
    if (typeof ResizeObserver !== "undefined") {
        layoutObserver = new ResizeObserver(measureVisibility)
        layoutObserver.observe(document.body)
        if (header.value) layoutObserver.observe(header.value)
    }
})
onBeforeUnmount(() => {
    window.removeEventListener("scroll", measureVisibility)
    window.removeEventListener("resize", measureVisibility)
    layoutObserver?.disconnect()
})

function entryDomId(id: string): string {
    return `entry-${id}`
}

// ---- live follow ----------------------------------------------------------
const dashboard = useDashboard()
let unfollow: (() => void) | null = null
let unmounted = false

/** Scroll to the transcript, not the blank space reserved for answer jumps. */
function scrollToEnd() {
    if (eink.value) pager.end()
    else if (transcriptEl.value) {
        const bottom = transcriptEl.value.getBoundingClientRect().bottom + window.scrollY
        window.scrollTo(0, Math.max(0, Math.ceil(bottom - readableBottom())))
    }
    measureVisibility()
}

async function onLiveAppend(entries: RenderedEntry[]) {
    if (transcript.value === null || entries.length === 0) return
    // Read before the DOM grows: document padding and the bottom guard are not
    // transcript content, so browser scrollHeight cannot determine follow mode.
    measureVisibility()
    const follow = autoFollow.value && atTranscriptEnd.value
    if (!follow && unseen.value === null) unseen.value = entries[0]
    if (eink.value && !follow) {
        pending.value.push(...entries)
        return
    }
    transcript.value.entries.push(...entries)
    await nextTick()
    if (follow) scrollToEnd()
    else measureVisibility()
}

async function applyPending() {
    if (transcript.value === null || pending.value.length === 0) return
    transcript.value.entries.push(...pending.value)
    pending.value = []
    await nextTick()
    pager.measure()
}

watch(autoFollow, async () => {
    const follow = autoFollow.value && atTranscriptEnd.value
    await applyPending()
    if (follow) scrollToEnd()
    else measureVisibility()
})

function startFollowing(view: TranscriptView) {
    if (unmounted) return // left before the transcript arrived
    unfollow = dashboard?.follow(
        {
            prefix: prefix.value,
            agent: agent.value,
            session_id: sessionId.value,
            subagent_id: agentId.value,
        },
        view.offset,
        settings.agentContext,
        {onAppend: (entries) => void onLiveAppend(entries)},
    ) ?? null
}

onBeforeUnmount(() => {
    unmounted = true
    unfollow?.()
    unfollow = null
})

async function jump() {
    const firstUnseen = unseen.value
    const target = firstUnseen
        ? (firstUnseen.kind === "assistant" ? firstUnseen.id : null)
        : (jumpLabel.value === "Latest answer ↓" ? latestAnswer.value : null)
    await applyPending()
    unseen.value = null
    const el = target === null ? null : document.getElementById(entryDomId(target))
    if (el) {
        if (eink.value) pager.goToElement(el)
        else el.scrollIntoView({block: "start"})
        measureVisibility()
    } else scrollToEnd()
}

/** The REST path of this transcript, without the leading `/api/repos/`. */
const transcriptPath = computed(() =>
    [
        prefix.value,
        "sessions",
        agent.value,
        sessionId.value,
        ...(agentId.value === null ? [] : ["subagents", agentId.value]),
    ]
        .map(encodeURIComponent)
        .join("/"),
)

// ---- agent context (meta records) -------------------------------------------
/** Withheld meta content fetched so far, by entry id; filled on first expand. */
const loadedContent = ref<Record<string, string>>({})
const contentFailed = ref<Record<string, boolean>>({})

async function expandMeta(id: string, event: Event) {
    const open = (event.target as HTMLDetailsElement).open
    if (!open || id in loadedContent.value) return
    try {
        const body = await apiFetch<EntryContent>(
            `/api/repos/${transcriptPath.value}/entries/${encodeURIComponent(id)}`,
        )
        loadedContent.value[id] = body.html
        await nextTick()
        pager.measure()
        contentFailed.value[id] = false
    } catch {
        contentFailed.value[id] = true
    }
}

async function load() {
    try {
        const mode = settings.agentContext
        const view = await apiFetch<TranscriptView>(
            `/api/repos/${transcriptPath.value}?meta=${mode}`,
        )
        if (mode !== settings.agentContext) return // the setting changed meanwhile: a newer load runs
        unfollow?.()
        unfollow = null
        loadedContent.value = {}
        contentFailed.value = {}
        pending.value = []
        unseen.value = null
        transcript.value = view
        startFollowing(view)
        await nextTick()
        pager.measure()
    } catch (e) {
        if (e instanceof Unauthorized) {
            await router.push({name: "login"})
        } else if (e instanceof Error && e.message.startsWith("API error 404")) {
            error.value = agentId.value === null ? "Session not found." : "Subagent not found."
        } else {
            error.value = "The session could not be loaded."
        }
    }
}

onMounted(load)
// Changing Agent context re-reads the transcript and resubscribes with the new mode.
watch(
    () => settings.agentContext,
    () => {
        // Stop the old-mode appends at once; `load` resubscribes with the new mode.
        unfollow?.()
        unfollow = null
        void load()
    },
)
</script>

<template>
    <div class="page" :style="{'--session-readable-top': `${readableTop}px`}">
        <header ref="header" class="session-header">
            <div class="bar">
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
            <router-link class="toggle" :to="{name: 'settings'}">Settings</router-link>
            </div>
            <template v-if="transcript">
                <h1 :title="title">{{ title }}</h1>
                <p class="session-meta muted">
                    <span>{{ transcript.agent }}</span>
                    <span v-if="transcript.models.length" class="mono" :title="transcript.models.join(', ')">{{
                        transcript.models.join(", ")
                    }}</span>
                </p>
            </template>
            <p v-if="dashboard && !dashboard.state.connected" class="notice">Connecting…</p>
        </header>
        <p v-if="error" class="notice">{{ error }}</p>
        <p v-else-if="transcript === null" class="muted">Loading…</p>
        <template v-else>
            <p v-if="transcript.entries.length === 0" class="muted">This session is empty.</p>
            <article ref="transcriptEl" class="transcript">
                <template v-for="e in shown" :key="e.id">
                    <section
                        v-if="e.kind === 'user'"
                        :id="entryDomId(e.id)"
                        class="entry user"
                        :class="{meta: e.meta}"
                    >
                        <details
                            v-if="e.meta && e.expandable"
                            class="meta-record"
                            @toggle="expandMeta(e.id, $event)"
                        >
                            <summary>{{ e.label || 'Agent context' }}</summary>
                            <div
                                v-if="e.id in loadedContent"
                                class="prose"
                                v-html="loadedContent[e.id]"
                            />
                            <p v-else-if="contentFailed[e.id]" class="muted">
                                The content could not be loaded.
                            </p>
                            <p v-else class="muted">Loading…</p>
                        </details>
                        <p v-else-if="e.meta && !e.html" class="meta-label">{{ e.label || 'Agent context' }}</p>
                        <template v-else>
                            <p v-if="e.meta && e.label" class="meta-label">{{ e.label }}</p>
                            <div class="prose" v-html="e.html" />
                        </template>
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
                v-if="jumpLabel"
                type="button"
                class="jump-latest"
                :class="{'new-messages': pending.length > 0 || unseen !== null}"
                @click="jump"
            >
                {{ jumpLabel }}
            </button>
        </template>
        <PageNav :pager="pager" />
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    /* Even a short final reply can start just below the sticky header. */
    padding: 0 var(--space-3) max(5rem, calc(100dvh - var(--session-readable-top)));
}
.session-header {
    position: sticky;
    /* Stick below App's out-of-date banner rather than under it. */
    top: var(--app-banner-height, 0px);
    z-index: 12;
    padding: var(--space-2) 0;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
}
.bar {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
}
.back {
    color: var(--muted);
    text-decoration: none;
    font-family: var(--font-mono);
}
.toggle {
    display: flex;
    align-items: center;
    text-decoration: none;
    gap: var(--space-2);
    min-height: 2.5rem;
    color: var(--muted);
    font-size: 0.9rem;
}
h1 {
    font-size: 1.4rem;
    line-height: 1.3;
    margin: 0 0 var(--space-1);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.session-meta {
    display: flex;
    gap: var(--space-1) var(--space-3);
    margin: 0;
    font-size: 0.85rem;
}
.session-meta > span {
    flex-shrink: 0;
}
.session-meta .mono {
    flex-shrink: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    scroll-margin-top: var(--session-readable-top, 0px);
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
.entry.user.meta .meta-label,
.entry.user.meta summary {
    margin: 0;
    font-weight: 600;
}
.entry.user.meta summary {
    cursor: pointer;
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
    font-size: var(--font-size-code);
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
    bottom: calc(var(--guard-bottom) + var(--space-3));
    z-index: 11;
    min-height: 2.75rem;
    padding: var(--space-2) var(--space-3);
    font: inherit;
    color: var(--accent-text);
    background: var(--accent);
    border: none;
    border-radius: var(--radius);
    cursor: pointer;
}
.new-messages {
    color: var(--text);
    background: var(--notice-bg);
    border: 2px solid var(--border-strong);
}
/* In e-ink the bar owns the bottom edge: the buttons sit above it. */
:root[data-profile="eink"] .jump-latest,
:root[data-profile="eink"] .new-messages {
    bottom: calc(var(--guard-bottom) + var(--page-nav-height) + var(--space-3));
}
</style>
