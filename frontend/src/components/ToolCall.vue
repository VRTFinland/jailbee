<script setup lang="ts">
// One tool call, folded to its summary line. Expanding shows the arguments
// and the (possibly truncated) result. All HTML here is the server's
// sanitised rendering.
import {ref} from "vue"
import type {RenderedSubagent, RenderedToolCall, RenderedToolResult} from "../protocol"

const props = withDefaults(
    defineProps<{
        call: RenderedToolCall
        result: RenderedToolResult | null
        subagents?: RenderedSubagent[]
        /** The session this call belongs to; subagents link to their own view when given. */
        sessionRef?: {prefix: string; agent: string; sessionId: string}
    }>(),
    {subagents: () => [], sessionRef: undefined},
)

const open = ref(false)
</script>

<template>
    <div class="tool-call">
        <button
            type="button"
            class="tool-summary"
            :class="{error: props.result?.is_error}"
            :aria-expanded="open ? 'true' : 'false'"
            @click="open = !open"
        >
            <span class="chevron" aria-hidden="true">{{ open ? "▾" : "▸" }}</span>
            <span class="summary-text">{{ props.call.summary }}</span>
        </button>
        <ul v-if="props.subagents.length" class="subagents">
            <li v-for="s in props.subagents" :key="s.id" class="tool-subagent">
                <router-link
                    v-if="props.sessionRef"
                    class="subagent-link"
                    :to="{name: 'subagent', params: {...props.sessionRef, agentId: s.agent_id}}"
                >
                    Subagent: {{ s.description || s.agent_id }}
                </router-link>
                <template v-else>Subagent: {{ s.description || s.agent_id }}</template>
                <span v-if="s.agent_type" class="muted">({{ s.agent_type }})</span>
            </li>
        </ul>
        <div v-if="open" class="tool-body">
            <div class="label">Input</div>
            <div class="tool-input" v-html="props.call.input_html" />
            <div class="label">Result</div>
            <div v-if="props.result" class="tool-result" :class="{error: props.result.is_error}">
                <div v-html="props.result.html" />
                <p v-if="props.result.truncated" class="muted truncated">
                    Truncated; the full result has {{ props.result.full_length }} characters.
                </p>
            </div>
            <p v-else class="muted">No result yet.</p>
        </div>
    </div>
</template>

<style scoped>
.tool-call {
    margin: var(--space-2) 0;
}
.tool-summary {
    display: flex;
    gap: var(--space-2);
    align-items: baseline;
    width: 100%;
    min-height: 2.5rem; /* touch target */
    padding: var(--space-2) var(--space-3);
    font: inherit;
    font-size: 0.9rem;
    text-align: left;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    cursor: pointer;
}
.tool-summary.error {
    border-color: var(--danger);
}
.summary-text {
    font-family: var(--font-mono);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
}
.subagents {
    list-style: none;
    margin: var(--space-1) 0 0;
    padding: 0 var(--space-3);
    font-size: 0.9rem;
}
.tool-body {
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border);
    border-top: none;
    border-radius: 0 0 var(--radius) var(--radius);
}
.label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin-top: var(--space-2);
}
.tool-input :deep(pre),
.tool-result :deep(pre) {
    margin: var(--space-1) 0;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 24rem;
    overflow: auto;
}
.tool-result.error :deep(pre) {
    color: var(--danger);
}
.muted {
    color: var(--muted);
}
.truncated {
    font-size: 0.8rem;
    margin: 0;
}
</style>
