<script setup lang="ts">
import Button from "../components/ui/Button.vue"
import AgentComposer from "../components/AgentComposer.vue"
import TranscriptEntry from "../components/ui/TranscriptEntry.vue"
import TitleBar from "../components/TitleBar.vue"
// One agent session as a readable document. Every `html` field is rendered
// and sanitised by the server; it is inserted as is.
import {computed, nextTick, onBeforeUnmount, onMounted, provide, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {apiFetch, Unauthorized} from "../api"
import PageNav from "../components/PageNav.vue"
import JumpPanel from "../components/JumpPanel.vue"
import ToolCall from "../components/ToolCall.vue"
import {readableBottom} from "../composables/readableBottom"
import {usePager} from "../composables/usePager"
import {useDashboard} from "../dashboard"
import type {
    EntryContent,
    MessageOutcome,
    RenderedEntry,
    RenderedSubagent,
    RenderedToolResult,
    SessionSummary,
    TranscriptView,
} from "../protocol"
import {settings} from "../settings"
import {readPosition, writePosition} from "../readingPosition"
import {readItem, writeItem} from "../storage"

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
const jumpPanelOpen = ref(false)
const jumpOrigin = ref<HTMLElement | null>(null)
function openJump(origin: HTMLElement | null = document.activeElement as HTMLElement | null) {
    if (jumpPanelOpen.value) return
    jumpOrigin.value = origin
    jumpPanelOpen.value = true
}
provide("open-jump", openJump)
function jumpKey(event: KeyboardEvent) {
    const target = event.target
    if (event.key !== "j" || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey ||
        document.querySelector('[role="dialog"], dialog[open]') ||
        (target instanceof HTMLElement && (target.isContentEditable || !!target.closest('input, textarea, select, [contenteditable="true"]')))) return
    event.preventDefault()
    openJump()
}

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
const sending = ref(false)
const sendError = ref("")
const sendStatus = ref("")
let awaitingText: string | null = null
let matched = false
function matchesMessage(entries: RenderedEntry[], text: string): boolean {
    const normalize = (value: string) => value.replace(/\r\n/g, "\n")
    return entries.some(entry => entry.kind === "user" && !entry.meta &&
        entry.text !== undefined && normalize(entry.text) === normalize(text))
}
async function sendMessage(text: string) {
    if (sending.value || !transcript.value?.chat?.available || agentId.value !== null) return
    sending.value = true
    sendError.value = ""
    sendStatus.value = ""
    awaitingText = text
    matched = false
    try {
        const result = await apiFetch<MessageOutcome>(`/api/repos/${transcriptPath.value}/messages`, {
            method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text}),
        })
        if (result.code !== "delivered") {
            awaitingText = null
            sendError.value = result.message
        } else if (matched) {
            awaitingText = null
            sendStatus.value = "Delivered"
        } else sendStatus.value = "Waiting for transcript"
    } catch {
        awaitingText = null
        sendError.value = "The message could not be sent."
    } finally { sending.value = false }
}
const thinking = computed(() => settings.showThinking)
const parentTitle = ref("Parent session")
const sessionContainer = ref<string | null>(null)
const crumbs = computed(() => [
    {label: "JailBee", to: {name: "front"}},
    {label: prefix.value, to: {name: "repo", params: {prefix: prefix.value}}},
    ...(sessionContainer.value ? [{label: sessionContainer.value, to: {name: "container", params: {name: sessionContainer.value}}}] : []),
    ...(agentId.value === null ? [] : [{label: parentTitle.value, to: {name: "session", params: sessionRef.value}}]),
])
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

function visibleEntries(entries: RenderedEntry[], calls: Set<string>): RenderedEntry[] {
    return entries.filter((e) => {
        if (e.kind === "thinking") return thinking.value
        if (e.kind === "tool_result" || e.kind === "subagent") return !calls.has(e.call_id)
        return true
    })
}

/** The entries shown in the flow: results and subagents fold into their call. */
const shown = computed(() => visibleEntries(transcript.value?.entries ?? [], byCall.value.calls))
const pendingVisible = computed(() => {
    const calls = new Set([...transcript.value?.entries ?? [], ...pending.value]
        .filter((e) => e.kind === "tool_call").map((e) => e.call_id))
    return visibleEntries(pending.value, calls)
})

const latestAnswer = computed(() => {
    const entries = transcript.value?.entries ?? []
    for (let i = entries.length - 1; i >= 0; i--) {
        if (entries[i].kind === "assistant") return entries[i].id
    }
    return null
})

const atTranscriptEnd = ref(false)
const following = ref(false)
let lastScrollY = window.scrollY
let programmaticScroll = false
let scrollFrame: number | undefined
const answerAhead = ref(false)
const unseen = ref<RenderedEntry | null>(null)
const autoFollow = computed(() => eink.value ? settings.einkFollow : settings.followNewMessages)
// Allow deferred scroll events to arrive before declaring a jump settled;
// bound the guard even if layout keeps moving indefinitely.
const SCROLL_STABLE_FRAMES = 3
const SCROLL_MAX_FRAMES = 120
const jumpAction = computed<"new" | "answer" | "bottom" | null>(() => {
    if (pending.value.length || unseen.value) return "new"
    if (atTranscriptEnd.value) return null
    return answerAhead.value ? "answer" : "bottom"
})
const jumpLabel = computed(() => jumpAction.value === null ? null : {
    new: pendingVisible.value.length ? `${pendingVisible.value.length} new ${pendingVisible.value.length === 1 ? "message" : "messages"} ↓` : "New messages ↓",
    answer: "Latest answer ↓",
    bottom: "Scroll to bottom ↓",
}[jumpAction.value])

function measureVisibility() {
    const content = transcriptEl.value?.getBoundingClientRect()
    atTranscriptEnd.value = !!content && content.bottom <= readableBottom() + 1
    if (atTranscriptEnd.value && pending.value.length === 0) unseen.value = null
    const el = latestAnswer.value === null ? null : document.getElementById(entryDomId(latestAnswer.value))
    if (el === null) {
        answerAhead.value = false
        return
    }
    const rect = el.getBoundingClientRect()
    // Once the answer's start is visible or passed, offer the end, not a jump
    // backwards to an answer the reader has already reached.
    answerAhead.value = rect.top >= readableBottom()
}

const positionStore = {getItem: readItem, setItem: writeItem}
const positionKey = [prefix.value, agent.value, sessionId.value, ...(agentId.value === null ? [] : [agentId.value])]
let positionTimer: ReturnType<typeof setTimeout> | undefined
let positionReady = false

function savePosition() {
    if (!positionReady || !transcriptEl.value) return
    measureVisibility()
    const entries = Array.from(transcriptEl.value.querySelectorAll<HTMLElement>("[id^='entry-']"))
    const first = entries[0]
    const entry = entries.reverse().find(el => el.getBoundingClientRect().top <= readableTop.value + 1) ?? first
    writePosition(positionStore, positionKey, {entryId: entry ? entry.id.slice(6) : null, atEnd: atTranscriptEnd.value})
}
function schedulePosition() {
    if (positionTimer !== undefined) return
    positionTimer = setTimeout(() => { positionTimer = undefined; savePosition() }, 250)
}

function onScroll() {
    measureVisibility()
    if (positionReady) schedulePosition()
    if (!programmaticScroll) {
        if (atTranscriptEnd.value) following.value = true
        else if (window.scrollY < lastScrollY) following.value = false
    }
    lastScrollY = window.scrollY
}

function finishProgrammaticScroll() {
    if (scrollFrame !== undefined) cancelAnimationFrame(scrollFrame)
    scrollFrame = undefined
    programmaticScroll = false
}

/** Scroll events are asynchronous, and smooth scrolling can span many frames. */
function scrollProgrammatically(scroll: () => void, followAtDestination = false) {
    finishProgrammaticScroll()
    programmaticScroll = true
    let previousY = window.scrollY
    let stableFrames = 0
    let frames = 0
    const settle = () => {
        stableFrames = window.scrollY === previousY ? stableFrames + 1 : 0
        previousY = window.scrollY
        if (stableFrames >= SCROLL_STABLE_FRAMES || ++frames >= SCROLL_MAX_FRAMES) {
            finishProgrammaticScroll()
            measureVisibility()
            if (followAtDestination) following.value = atTranscriptEnd.value
        } else scrollFrame = requestAnimationFrame(settle)
    }
    scroll()
    scrollFrame = requestAnimationFrame(settle)
    measureVisibility()
}

function markScrollIntent() {
    // Real input interrupts a pending jump; every later momentum frame remains
    // a user scroll too, without needing another wheel/touch/key event.
    finishProgrammaticScroll()
}
function markScrollKey(event: KeyboardEvent) {
    if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) markScrollIntent()
}

watch(shown, () => void nextTick(measureVisibility))
watch(eink, () => void nextTick(measureVisibility))
watch(() => settings.showTitleBar, () => void nextTick(measureVisibility))

let layoutObserver: ResizeObserver | null = null
onMounted(() => {
    window.addEventListener("scroll", onScroll, {passive: true})
    window.addEventListener("pagehide", savePosition)
    window.addEventListener("wheel", markScrollIntent, {passive: true})
    window.addEventListener("touchmove", markScrollIntent, {passive: true})
    window.addEventListener("pointerdown", markScrollIntent, {passive: true})
    window.addEventListener("click", markScrollIntent, {capture: true})
    window.addEventListener("keydown", markScrollKey)
    window.addEventListener("keydown", jumpKey)
    window.addEventListener("resize", measureVisibility)
    if (typeof ResizeObserver !== "undefined") {
        layoutObserver = new ResizeObserver(measureVisibility)
        layoutObserver.observe(document.body)
    }
})
onBeforeUnmount(() => {
    if (positionTimer !== undefined) clearTimeout(positionTimer)
    savePosition()
    window.removeEventListener("pagehide", savePosition)
    window.removeEventListener("scroll", onScroll)
    window.removeEventListener("wheel", markScrollIntent)
    window.removeEventListener("touchmove", markScrollIntent)
    window.removeEventListener("pointerdown", markScrollIntent)
    window.removeEventListener("click", markScrollIntent, {capture: true})
    finishProgrammaticScroll()
    window.removeEventListener("keydown", markScrollKey)
    window.removeEventListener("keydown", jumpKey)
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
// Finish each DOM update and its follow jump before measuring the next frame.
let appendQueue = Promise.resolve()

/** Scroll to the transcript, not the blank space reserved for answer jumps. */
function scrollToEnd() {
    scrollProgrammatically(() => {
        if (eink.value) pager.end()
        else if (transcriptEl.value) {
            const bottom = transcriptEl.value.getBoundingClientRect().bottom + window.scrollY
            window.scrollTo(0, Math.max(0, Math.ceil(bottom - readableBottom())))
        }
    })
}

async function onLiveAppend(entries: RenderedEntry[]) {
    if (transcript.value === null || entries.length === 0) return
    // Read before the DOM grows: document padding and the bottom guard are not
    // transcript content, so browser scrollHeight cannot determine follow mode.
    measureVisibility()
    const follow = autoFollow.value && following.value
    const calls = new Set([...transcript.value.entries, ...pending.value, ...entries]
        .filter((e) => e.kind === "tool_call").map((e) => e.call_id))
    const visible = visibleEntries(entries, calls)
    if (!follow && unseen.value === null) unseen.value = visible[0] ?? null
    if (eink.value && !follow && (pending.value.length > 0 || visible.length > 0)) {
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
    const activeTranscript = transcript.value
    unfollow = dashboard?.follow(
        {
            prefix: prefix.value,
            agent: agent.value,
            session_id: sessionId.value,
            subagent_id: agentId.value,
        },
        view.offset,
        settings.agentContext,
        {onAppend: (entries, chat) => {
            if (transcript.value === activeTranscript && transcript.value && chat) transcript.value.chat = chat
            if (awaitingText !== null && matchesMessage(entries, awaitingText)) {
                matched = true
                if (!sending.value) { awaitingText = null; sendStatus.value = "Delivered" }
            }
            appendQueue = appendQueue.then(async () => {
                if (!unmounted && transcript.value === activeTranscript) await onLiveAppend(entries)
            })
        }},
    ) ?? null
}

onBeforeUnmount(() => {
    unmounted = true
    unfollow?.()
    unfollow = null
})

function selectJump(target: string) {
    jumpPanelOpen.value = false
    void jump(target as "start" | "latest" | "end" | `entry:${string}`)
}

async function jump(destination: "auto" | "latest" | "end" | "start" | `entry:${string}` = "auto") {
    following.value = false
    const firstUnseen = unseen.value
    const autoTarget = firstUnseen
        ? (firstUnseen.kind === "assistant" ? firstUnseen.id : null)
        : (jumpAction.value === "answer" ? latestAnswer.value : null)
    if (destination === "auto" || destination === "latest" || destination === "end") {
        await applyPending()
        unseen.value = null
    }
    if (destination === "start") {
        scrollProgrammatically(() => eink.value ? pager.goToElement(transcriptEl.value!) : window.scrollTo(0, 0))
        return
    }
    const target = destination === "auto" ? autoTarget : destination === "latest" ? latestAnswer.value : destination.startsWith("entry:") ? destination.slice(6) : null
    const el = target === null ? null : document.getElementById(entryDomId(target))
    if (el) {
        // A short answer's start already exposes the transcript end. Arm follow
        // from the destination geometry, never an intermediate animation frame.
        const destinationY = Math.max(0, el.getBoundingClientRect().top + window.scrollY - readableTop.value)
        const transcriptBottom = transcriptEl.value!.getBoundingClientRect().bottom + window.scrollY
        following.value = transcriptBottom - destinationY <= readableBottom() + 1
        scrollProgrammatically(() => {
            if (eink.value) pager.goToElement(el)
            else el.scrollIntoView({block: "start"})
        }, true)
    } else {
        following.value = true
        scrollToEnd()
    }
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
        measureVisibility()
        if (!positionReady) {
            const remembered = readPosition(positionStore, positionKey)
            const hash = route.hash ? route.hash.slice(1) : null
            const targetId = hash ?? remembered?.entryId ?? null
            const target = targetId ? document.getElementById(hash ? targetId : entryDomId(targetId)) : null
            if (hash) {
                if (target) scrollProgrammatically(() => eink.value ? pager.goToElement(target) : target.scrollIntoView({block: "start"}))
            } else if (remembered?.atEnd && autoFollow.value) {
                scrollToEnd()
            } else if (remembered) {
                if (target) scrollProgrammatically(() => eink.value ? pager.goToElement(target) : target.scrollIntoView({block: "start"}))
                else if (remembered.entryId === null) scrollToEnd()
                else window.scrollTo(0, 0)
            }
            positionReady = true
        }
        measureVisibility()
        following.value = atTranscriptEnd.value
        lastScrollY = window.scrollY
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

onMounted(() => {
    void load()
    const path = [prefix.value, "sessions", agent.value, sessionId.value].map(encodeURIComponent).join("/")
    void apiFetch<SessionSummary>(`/api/repos/${path}/summary`).then(parent => {
        parentTitle.value = parent.title || "Untitled session"
        sessionContainer.value = parent.container ?? null
    }).catch(() => {})
})
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
    <div :style="{'--session-readable-top': `${readableTop}px`}">
        <TitleBar @readable-top="readableTop = $event" :title="title" :crumbs="crumbs" context="session" :session="transcript ? {agent: transcript.agent, models: transcript.models, id: transcript.session_id} : null" />
        <div class="page">
            <p v-if="dashboard && !dashboard.state.connected" class="notice ui-notice">Connecting…</p>
            <p v-if="error" class="notice ui-notice">{{ error }}</p>
            <p v-else-if="transcript === null" class="muted">Loading…</p>
            <template v-else>
                <p v-if="transcript.entries.length === 0" class="muted">This session is empty.</p>
                <article ref="transcriptEl" class="transcript">
                    <template v-for="e in shown" :key="e.id">
                        <TranscriptEntry
                            v-if="e.kind === 'user'"
                            :id="entryDomId(e.id)"
                            kind="user"
                            :meta="e.meta"
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
                        </TranscriptEntry>
                        <TranscriptEntry
                            v-else-if="e.kind === 'assistant'"
                            :id="entryDomId(e.id)"
                            kind="assistant"
                        >
                            <div v-if="e.model" class="model">{{ e.model }}</div>
                            <div class="prose" v-html="e.html" />
                        </TranscriptEntry>
                        <TranscriptEntry
                            v-else-if="e.kind === 'thinking'"
                            :id="entryDomId(e.id)"
                            kind="thinking"
                        >
                            <div class="label">Thinking</div>
                            <div v-if="e.html" class="prose" v-html="e.html" />
                            <p v-else class="muted">(redacted)</p>
                        </TranscriptEntry>
                        <ToolCall
                            v-else-if="e.kind === 'tool_call'"
                            :id="entryDomId(e.id)"
                            :call="e"
                            :result="byCall.results.get(e.call_id) ?? null"
                            :subagents="byCall.subagents.get(e.call_id) ?? []"
                            :session-ref="sessionRef"
                        />
                        <TranscriptEntry
                            v-else-if="e.kind === 'tool_result'"
                            :id="entryDomId(e.id)"
                            kind="orphan"
                        >
                            <div class="prose" v-html="e.html" />
                        </TranscriptEntry>
                        <TranscriptEntry
                            v-else-if="e.kind === 'subagent'"
                            :id="entryDomId(e.id)"
                            kind="orphan"
                        >
                            <router-link
                                :to="{name: 'subagent', params: {...sessionRef, agentId: e.agent_id}}"
                            >
                                Subagent: {{ e.description || e.agent_id }}
                            </router-link>
                        </TranscriptEntry>
                    </template>
                </article>
                <AgentComposer v-if="agentId === null" class="agent-composer"
                    :available="transcript.chat?.available ?? false"
                    :reason="transcript.chat?.message || 'Messaging is unavailable.'"
                    :sending="sending" :error="sendError" :status="sendStatus" @send="sendMessage" />
                <Button
                    v-if="jumpLabel"
                    :variant="pending.length > 0 || unseen !== null ? 'tonal' : 'primary'"
                    icon="page-down"
                    class="jump-latest"
                    :class="{'new-messages': pending.length > 0 || unseen !== null}"
                    @click="jump()"
                >
                    {{ jumpLabel }}
                </Button>
            </template>
            <PageNav :pager="pager" jump-enabled @jump="openJump()" />
            <JumpPanel v-if="jumpPanelOpen" :entries="shown" :origin="jumpOrigin" @close="jumpPanelOpen = false" @select="selectJump" />
        </div>
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    /* Even a short final reply can start just below the sticky header. */
    padding: 0 var(--md-space-16);
}
.page::after {
    content: "";
    display: block;
    height: max(calc(var(--md-target-min) + var(--md-space-32)), calc(100dvh - var(--session-readable-top)));
}
.agent-composer {
    position: sticky;
    bottom: calc(var(--guard-bottom) + var(--page-nav-height));
    z-index: var(--md-layer-sticky);
    background: var(--md-surface);
    padding: var(--md-space-8);
}
.muted {
    color: var(--md-on-surface-variant);
}
.transcript > .entry {
    margin: var(--md-space-24) 0;
}
.entry, .tool-call[id] {
    scroll-margin-top: var(--session-readable-top, 0px);
}
.entry.user.meta .meta-label,
.entry.user.meta summary {
    margin: 0;
    font-weight: 600;
}
.entry.user.meta summary {
    cursor: pointer;
}
.model,
.label {
    font-family: var(--md-font-mono);
    font-size: var(--md-type-label-medium-size);
    color: var(--md-on-surface-variant);
    margin-bottom: var(--md-space-4);
}
.entry.orphan :deep(pre) {
    white-space: pre-wrap;
    font-size: var(--md-type-body-small-size);
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
    margin: var(--md-space-12) 0;
}
.prose :deep(h1),
.prose :deep(h2),
.prose :deep(h3),
.prose :deep(h4) {
    line-height: 1.3;
    margin: var(--md-space-24) 0 var(--md-space-8);
}
.prose :deep(h1) {
    font-size: var(--md-type-title-large-size);
}
.prose :deep(h2) {
    font-size: var(--md-type-title-medium-size);
}
.prose :deep(h3),
.prose :deep(h4) {
    font-size: var(--md-type-title-small-size);
}
.prose :deep(a) {
    color: var(--md-primary);
}
.prose :deep(code) {
    font-size: var(--md-type-body-medium-size);
    padding: 0 var(--md-space-4);
    background: var(--md-surface-container-low);
    border-radius: var(--md-shape-small);
}
.prose :deep(pre) {
    margin: var(--md-space-12) 0;
    padding: var(--md-space-8) var(--md-space-16);
    background: var(--md-surface-container-low);
    border: 1px solid var(--md-outline-variant);
    border-radius: var(--md-shape-medium);
    overflow-x: auto;
    line-height: 1.45;
    font-family: var(--md-font-mono);
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
    padding: var(--md-space-4) var(--md-space-8);
    border: 1px solid var(--md-outline-variant);
    text-align: left;
}
.prose :deep(blockquote) {
    padding-left: var(--md-space-16);
    border-left: var(--md-space-4) solid var(--md-outline-variant);
    color: var(--md-on-surface-variant);
}
.jump-latest {
    position: fixed;
    right: var(--md-space-16);
    bottom: calc(var(--guard-bottom) + var(--md-space-16));
    z-index: var(--md-layer-raised);
}
/* In e-ink the bar owns the bottom edge: the buttons sit above it. */
[data-profile="eink"] .jump-latest,
[data-profile="eink"] .new-messages {
    bottom: calc(var(--guard-bottom) + var(--page-nav-height) + var(--md-space-16));
}
</style>
