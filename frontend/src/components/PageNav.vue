<script setup lang="ts">
// Floating e-ink controls or a thin bar, always above the bottom guard.
import {computed, nextTick, watch} from "vue"
import type {Pager} from "../composables/usePager"
import {settings} from "../settings"
import Button from "./ui/Button.vue"
import IconButton from "./ui/IconButton.vue"
import PageGuide from "./PageGuide.vue"

const props = defineProps<{pager: Pager; jumpEnabled?: boolean}>()
const emit = defineEmits<{jump: []}>()
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
            <IconButton v-if="settings.pageControls !== 'indicator'" class="prev"
                :icon="settings.pageControls === 'side' ? 'collapse' : 'back'" label="Previous page"
                :compact="settings.pageControls === 'bar'" :disabled="pager.page.value <= 0" @click="pager.prev()" />
            <Button v-if="jumpEnabled" variant="text" narrow :compact="settings.pageControls !== 'side'" class="page-indicator" aria-label="Jump to page" @click="emit('jump')">{{ pager.page.value + 1 }} / {{ pager.pageCount.value }}</Button>
            <span v-else class="page-indicator">{{ pager.page.value + 1 }} / {{ pager.pageCount.value }}</span>
            <span v-if="settings.pageControls === 'bar'" class="page-percent">· {{ percent }} %</span>
            <IconButton v-if="settings.pageControls !== 'indicator'" class="next"
                :icon="settings.pageControls === 'side' ? 'expand' : 'forward'" label="Next page"
                :compact="settings.pageControls === 'bar'" :disabled="pager.page.value >= pager.pageCount.value - 1"
                @click="pager.next()" />
        </nav>
    </template>
</template>

<style scoped>
.page-nav {
    position: fixed;
    right: var(--md-space-8);
    bottom: var(--guard-bottom);
    z-index: var(--md-layer-raised);
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--md-surface-container);
    border: 1px solid var(--md-outline-variant);
    border-radius: var(--md-shape-medium);
    font-size: var(--md-type-label-large-size);
    line-height: 1;
    white-space: nowrap;
}
.page-nav[data-mode="indicator"] {
    width: var(--page-controls-width);
    height: calc(var(--md-space-24) + var(--md-space-4));
}
.page-nav[data-mode="side"] {
    flex-direction: column;
    width: var(--page-controls-width);
    border: none;
    outline: 1px solid var(--md-outline-variant);
    outline-offset: -1px;
}
.page-nav[data-mode="side"] .page-indicator { flex: none; }
.page-nav[data-mode="side"] span.page-indicator {
    width: 100%;
    height: var(--md-target-min);
    border-block: 1px solid var(--md-outline-variant);
}
.page-nav[data-mode="bar"] {
    left: 0;
    right: 0;
    height: var(--page-nav-height);
    gap: var(--md-space-8);
    border-width: 1px 0 0;
    border-radius: 0;
}
.page-indicator { display: flex; align-items: center; justify-content: center; }
.tap-zone {
    position: fixed;
    top: 0;
    bottom: calc(var(--guard-bottom) + var(--page-nav-height));
    width: 8vw;
    z-index: auto;
    padding: 0;
    background: none;
    border: none;
}
.tap-zone.back { left: 0; }
.tap-zone.forward { right: 0; }
</style>
