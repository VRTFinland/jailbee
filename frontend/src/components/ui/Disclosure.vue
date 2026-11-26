<script setup lang="ts">
import Icon from "./Icon.vue"

defineProps<{label: string; error?: boolean}>()
const open = defineModel<boolean>("open", {default: false})
</script>

<template>
    <button type="button" class="disclosure ui-state-layer ui-focus-ring" :data-error="error || undefined"
        :aria-expanded="open" @click="open = !open">
        <Icon :name="open ? 'expand' : 'forward'" size="small" />
        <span>{{ label }}</span>
    </button>
</template>

<style src="./state-layer.css"></style>
<style scoped>
.disclosure {
    display: flex;
    align-items: center;
    gap: var(--md-space-8);
    width: 100%;
    min-height: var(--md-target-min);
    padding: var(--md-space-8) var(--md-space-16);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-medium);
    background: var(--md-surface-container);
    color: var(--md-on-surface-variant);
    font-family: var(--md-font-mono);
    font-size: var(--md-type-body-medium-size);
    line-height: var(--md-type-body-medium-line-height);
    text-align: left;
    cursor: pointer;
}
.disclosure[data-error] {
    border-color: var(--md-error);
    color: var(--md-error);
}
.disclosure span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
</style>
