<script setup lang="ts">
// Floating e-ink controls or a thin bar, always above the bottom guard.
import {computed, nextTick, watch} from "vue"
import type {Pager} from "../composables/usePager"
import {settings} from "../settings"
import PageGuide from "./PageGuide.vue"

const props = defineProps<{pager: Pager}>()
const percent = computed(() => Math.round(
    (props.pager.page.value + 1) / props.pager.pageCount.value * 100,
))
watch(() => settings.pageControls, async () => {
    await nextTick()
    props.pager.measure()
})
</script>

<template>
    <template v-if="settings.displayProfile === 'eink'">
        <button
            type="button"
            class="tap-zone back"
            aria-label="Previous page"
            tabindex="-1"
            @click="pager.prev()"
        />
        <button
            type="button"
            class="tap-zone forward"
            aria-label="Next page"
            tabindex="-1"
            @click="pager.next()"
        />
        <PageGuide :pager="pager" />
        <nav class="page-nav" :data-mode="settings.pageControls" aria-label="Pages">
            <button v-if="settings.pageControls !== 'indicator'" type="button" class="prev"
                aria-label="Previous page" :disabled="pager.page.value <= 0" @click="pager.prev()">
                {{ settings.pageControls === 'side' ? '▲' : '‹' }}
            </button>
            <span class="page-indicator">{{ pager.page.value + 1 }} / {{ pager.pageCount.value }}</span>
            <span v-if="settings.pageControls === 'bar'" class="page-percent">· {{ percent }} %</span>
            <button v-if="settings.pageControls !== 'indicator'" type="button" class="next"
                aria-label="Next page" :disabled="pager.page.value >= pager.pageCount.value - 1"
                @click="pager.next()">
                {{ settings.pageControls === 'side' ? '▼' : '›' }}
            </button>
        </nav>
    </template>
</template>

<style scoped>
.page-nav {
    position: fixed;
    right: var(--space-2);
    bottom: var(--guard-bottom);
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg);
    border: 1px solid var(--border);
    font-size: 13px;
    line-height: 1;
    white-space: nowrap;
}
.page-nav[data-mode="indicator"] {
    width: var(--page-controls-width);
    height: 28px;
    border-radius: var(--radius);
}
.page-nav[data-mode="side"] {
    flex-direction: column;
    width: var(--page-controls-width);
    border: none;
    outline: 1px solid var(--border);
    outline-offset: -1px;
}
.page-nav[data-mode="side"] .page-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 44px;
    border-block: 1px solid var(--border);
}
.page-nav[data-mode="bar"] {
    left: 0;
    right: 0;
    height: var(--page-nav-height);
    gap: var(--space-2);
    border-width: 1px 0 0;
}
.page-nav button {
    width: 44px;
    height: 44px;
    flex-shrink: 0;
    padding: 0;
    font: inherit;
    color: var(--text);
    background: var(--bg);
    border: none;
    cursor: pointer;
}
.page-nav[data-mode="bar"] button {
    height: 100%;
}
.page-nav button:disabled {
    color: var(--muted);
    cursor: default;
}
.tap-zone {
    position: fixed;
    top: 0;
    bottom: calc(var(--guard-bottom) + var(--page-nav-height));
    width: 8vw;
    z-index: 5;
    padding: 0;
    background: none;
    border: none;
}
.tap-zone.back {
    left: 0;
}
.tap-zone.forward {
    right: 0;
}
</style>
