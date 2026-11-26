<script setup lang="ts">
import {computed} from "vue"
import Dialog from "./ui/Dialog.vue"
import Button from "./ui/Button.vue"
import type {RenderedEntry} from "../protocol"

const props = defineProps<{entries: RenderedEntry[]; origin?: HTMLElement | null}>()
const emit = defineEmits<{close: []; select: [target: string]}>()
const prompts = computed(() => props.entries.filter(e => e.kind === "user" && !e.meta).map(e => {
    const node = document.createElement("template")
    node.innerHTML = e.kind === "user" ? e.html : ""
    return {id: e.id, text: (node.content.textContent ?? "").replace(/\s+/g, " ").trim() || "(empty prompt)"}
}))
function returnFocus(event: Event) {
    event.preventDefault()
    props.origin?.focus({preventScroll: true})
}
</script>

<template>
    <Dialog :open="true" title="Jump to" aria-label="Jump to" close-label="Close" preserve-scroll
        @update:open="value => { if (!value) emit('close') }" @close-auto-focus="returnFocus">
        <div class="jump-targets">
            <Button variant="text" icon="start" @click="emit('select', 'start')">Start</Button>
            <Button variant="text" icon="jump" @click="emit('select', 'latest')">Latest answer</Button>
            <Button variant="text" icon="end" @click="emit('select', 'end')">End</Button>
            <Button v-for="prompt in prompts" :key="prompt.id" variant="text" @click="emit('select', `entry:${prompt.id}`)">{{ prompt.text }}</Button>
        </div>
    </Dialog>
</template>

<style scoped>
.jump-targets { display: flex; flex-direction: column; gap: var(--md-space-4); }
</style>
