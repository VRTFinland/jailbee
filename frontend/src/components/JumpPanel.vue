<script setup lang="ts">
import {computed, nextTick, onMounted, ref} from "vue"
import type {RenderedEntry} from "../protocol"

const props = defineProps<{entries: RenderedEntry[]}>()
const emit = defineEmits<{close: []; select: [target: string]}>()
const closeButton = ref<HTMLButtonElement | null>(null)
const prompts = computed(() => props.entries.filter(e => e.kind === "user" && !e.meta).map(e => {
    const node = document.createElement("div")
    node.innerHTML = e.kind === "user" ? e.html : ""
    return {id: e.id, text: (node.textContent ?? "").replace(/\s+/g, " ").trim() || "(empty prompt)"}
}))
onMounted(() => void nextTick(() => closeButton.value?.focus()))
</script>

<template>
    <div class="jump-backdrop" @click.self="emit('close')">
        <section class="jump-panel" role="dialog" aria-modal="true" aria-label="Jump to" @keydown.esc.stop="emit('close')">
            <header><strong>Jump to</strong><button ref="closeButton" type="button" @click="emit('close')">Close</button></header>
            <button type="button" @click="emit('select', 'start')">Start</button>
            <button type="button" @click="emit('select', 'latest')">Latest answer</button>
            <button type="button" @click="emit('select', 'end')">End</button>
            <button v-for="prompt in prompts" :key="prompt.id" type="button" class="excerpt" @click="emit('select', `entry:${prompt.id}`)">{{ prompt.text }}</button>
        </section>
    </div>
</template>

<style scoped>
.jump-backdrop { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; background: rgb(0 0 0 / 20%); }
.jump-panel { box-sizing: border-box; width: min(32rem, calc(100vw - 2rem)); max-height: calc(100dvh - 2rem); overflow-y: auto; padding: var(--space-2); color: var(--text); background: var(--surface); border: 1px solid var(--border-strong); }
header { display: flex; align-items: center; justify-content: space-between; }
.jump-panel > button { display: block; width: 100%; text-align: left; border-bottom: 1px solid var(--border); }
button { min-height: 44px; padding: var(--space-2); color: inherit; background: transparent; border: 0; font: inherit; cursor: pointer; }
.excerpt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
:root[data-profile="eink"] .jump-backdrop { background: transparent; }
</style>
