<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, ref, watch} from "vue"
import {readItem, writeItem} from "../storage"
import Button from "./ui/Button.vue"
import Field from "./ui/Field.vue"
import IconButton from "./ui/IconButton.vue"

const props = withDefaults(defineProps<{available: boolean, reason?: string, error?: string, sending?: boolean, awaiting?: boolean, status?: string, initialExpanded?: boolean, panelOpen?: boolean}>(), {initialExpanded: undefined})
const freshResult = ref(false)
let resultTimer: ReturnType<typeof setTimeout> | undefined
watch(() => [props.error, props.status], () => {
    clearTimeout(resultTimer)
    freshResult.value = !!(props.error || props.status)
    if (freshResult.value) resultTimer = setTimeout(() => { freshResult.value = false }, 5000)
}, {immediate: true})
onBeforeUnmount(() => clearTimeout(resultTimer))
const statusText = computed(() => props.sending ? 'Sending…' :
    (freshResult.value ? props.error || props.status : '') ||
    (!props.available ? props.reason : '') || props.error || props.status)
const statusError = computed(() => !!props.error && statusText.value === props.error)
const emit = defineEmits<{send: [text: string]}>()
const text = defineModel<string>("draft", {default: ""})
const saved = readItem("jailbee.composerExpanded")
const expanded = ref(props.initialExpanded ?? (saved === null ? window.innerWidth >= 900 : saved === "1"))
const root = ref<HTMLFormElement | null>(null)
async function toggle(value: boolean) {
    expanded.value = value
    writeItem("jailbee.composerExpanded", value ? "1" : "0")
    if (value) {
        await nextTick()
        root.value?.querySelector("textarea")?.focus()
    }
}
function send() {
    if (props.available && !props.sending && !props.awaiting && text.value.trim()) emit("send", text.value)
}
async function insertNewline() {
    if (!props.available || props.sending || props.awaiting) return
    const field = root.value?.querySelector('textarea')
    if (!field) return
    const start = field.selectionStart
    text.value = text.value.slice(0, start) + "\n" + text.value.slice(field.selectionEnd)
    await nextTick()
    field.focus()
    field.setSelectionRange(start + 1, start + 1)
}
function keydown(event: KeyboardEvent) {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return
    event.preventDefault()
    send()
}
</script>

<template>
    <form ref="root" class="composer" :class="{collapsed: !expanded}" @submit.prevent="send">
        <div class="composer-input">
            <div v-if="expanded && !panelOpen" class="composer-controls">
                <Field v-model="text" label="Message agent" multiline enterkeyhint="send" placeholder="Enter to send; Shift+Enter for a new line"
                    :disabled="!available || sending || awaiting" @keydown="keydown" />
                <IconButton icon="jump" label="Insert newline" :disabled="!available || sending || awaiting" @click="insertNewline" />
                <IconButton icon="expand" label="Hide composer" @click="toggle(false)" />
                <IconButton icon="page-up" label="Send message" variant="tonal"
                    :disabled="!available || sending || awaiting || !text.trim()" @click="send" />
            </div>
            <Button v-else-if="!panelOpen" variant="tonal" aria-label="Message agent" @click="toggle(true)">Message agent</Button>
            <p class="composer-status" :class="{'is-error': statusError}" role="status" aria-live="polite">{{ statusText }}</p>
        </div>
    </form>
</template>

<style scoped>
.composer-input { width: 100%; min-width: 0; }
.composer-controls { display: flex; align-items: end; gap: var(--md-space-8); }
.composer-controls > :first-child { flex: 1; min-width: 0; }
.composer-status { margin: 0; height: var(--md-type-body-small-line-height); line-height: var(--md-type-body-small-line-height); font-size: var(--md-type-body-small-size); color: var(--md-on-surface-variant); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.composer-status.is-error { color: var(--md-error); }
</style>
