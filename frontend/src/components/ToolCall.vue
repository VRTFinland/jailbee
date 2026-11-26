<script setup lang="ts">
// One tool call, folded to its summary line. Expanding shows the arguments
// and the (possibly truncated) result. All HTML here is the server's
// sanitised rendering.
import {ref} from "vue"
import Disclosure from "./ui/Disclosure.vue"
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
        <Disclosure v-model:open="open" class="tool-summary" :label="props.call.summary" :error="props.result?.is_error" />
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
    margin: var(--md-space-8) 0;
}
.subagents {
    list-style: none;
    margin: var(--md-space-4) 0 0;
    padding: 0 var(--md-space-16);
    font-size: var(--md-type-body-medium-size);
}
.tool-body {
    padding: var(--md-space-8) var(--md-space-16);
    border: 1px solid var(--md-outline-variant);
    border-top: none;
    background: var(--md-surface-container-low);
    border-radius: 0 0 var(--md-shape-medium) var(--md-shape-medium);
}
.label {
    font-size: var(--md-type-label-medium-size);
    text-transform: uppercase;
    letter-spacing: normal;
    color: var(--md-on-surface-variant);
    margin-top: var(--md-space-8);
}
.tool-input :deep(pre),
.tool-result :deep(pre) {
    margin: var(--md-space-4) 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-type-body-small-size);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 24rem;
    overflow: auto;
}
.tool-result.error :deep(pre) {
    color: var(--md-error);
}
.muted {
    color: var(--md-on-surface-variant);
}
.truncated {
    font-size: var(--md-type-body-small-size);
    margin: 0;
}
</style>
