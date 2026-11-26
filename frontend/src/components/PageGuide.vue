<script setup lang="ts">
import type {Pager} from "../composables/usePager"

defineProps<{pager: Pager}>()
</script>

<template>
    <div v-if="pager.guideTop.value !== null" class="page-guide" aria-hidden="true"
        :style="{top: `${pager.guideTop.value}px`}" />
</template>

<style scoped>
.page-guide {
    position: fixed;
    left: 0;
    right: 0;
    height: 1px;
    background: var(--md-outline-variant);
    pointer-events: none;
    z-index: var(--md-layer-raised);
}
</style>
