<script setup lang="ts">
// The e-ink profile's page-turn controls: edge tap zones, previous/next buttons
// and a page indicator, in a bar above the bottom guard. Renders nothing in
// the normal profile.
import type {Pager} from "../composables/usePager"
import {settings} from "../settings"

defineProps<{pager: Pager}>()
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
        <nav class="page-nav" aria-label="Pages">
            <button type="button" class="prev" :disabled="pager.page.value <= 0" @click="pager.prev()">
                &larr; Prev
            </button>
            <span class="page-indicator"
                >Page {{ pager.page.value + 1 }} / {{ pager.pageCount.value }}</span
            >
            <button
                type="button"
                class="next"
                :disabled="pager.page.value >= pager.pageCount.value - 1"
                @click="pager.next()"
            >
                Next &rarr;
            </button>
        </nav>
    </template>
</template>

<style scoped>
.page-nav {
    position: fixed;
    left: 0;
    right: 0;
    bottom: var(--guard-bottom);
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    height: var(--page-nav-height);
    padding: 0 var(--space-3);
    background: var(--bg);
    border-top: 1px solid var(--border);
}
.page-nav button {
    min-width: 6rem;
    min-height: 2.75rem;
    font: inherit;
    color: var(--text);
    background: var(--bg);
    border: 2px solid var(--border-strong);
    border-radius: var(--radius);
}
.page-nav button:disabled {
    opacity: 0.4;
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
