<script setup lang="ts">
import {computed, onBeforeUnmount, ref} from "vue"
import type {RenderedEntry} from "../protocol"
import Button from "./ui/Button.vue"
import Dialog from "./ui/Dialog.vue"

const props = defineProps<{entries: RenderedEntry[]; origin?: HTMLElement | null}>()
const emit = defineEmits<{close: []; select: [target: string]}>()
const open = ref(true)
const prompts = computed(() => props.entries.filter(e => e.kind === "user" && !e.meta).map(e => {
    const node = document.createElement("template")
    node.innerHTML = e.kind === "user" ? e.html : ""
    return {id: e.id, text: (node.content.textContent ?? "").replace(/\s+/g, " ").trim() || "(empty prompt)"}
}))
function restoreFocus(event?: Event) {
    event?.preventDefault()
    props.origin?.focus({preventScroll: true})
}
onBeforeUnmount(() => restoreFocus())
</script>

<template>
    <Dialog v-model:open="open" title="Jump to" aria-label="Jump to" close-label="Close" preserve-scroll
        @update:open="!$event && emit('close')" @close-auto-focus="restoreFocus">
        <div class="destinations">
            <Button variant="text" icon="start" @click="emit('select', 'start')">Start</Button>
            <Button variant="text" icon="jump" @click="emit('select', 'latest')">Latest answer</Button>
            <Button variant="text" icon="end" @click="emit('select', 'end')">End</Button>
            <Button v-for="prompt in prompts" :key="prompt.id" variant="text" truncate
                @click="emit('select', `entry:${prompt.id}`)">{{ prompt.text }}</Button>
        </div>
    </Dialog>
</template>

<style scoped>
.destinations { display: grid; gap: var(--md-space-4); }
</style>
