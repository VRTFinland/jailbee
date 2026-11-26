<script setup lang="ts">
withDefaults(defineProps<{kind?: "user" | "assistant" | "thinking" | "orphan"; meta?: boolean}>(), {kind: "assistant", meta: false})
</script>

<template>
    <section class="entry" :class="[kind, {meta}]"><slot /></section>
</template>

<style scoped>
.entry { margin: var(--md-space-24) 0; }
.user {
    padding: var(--md-space-8) var(--md-space-16);
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-medium);
}
.meta {
    background: transparent;
    color: var(--md-on-surface-variant);
    border-color: var(--md-outline-variant);
    font-size: var(--md-type-body-small-size);
}
.orphan { color: var(--md-on-surface-variant); }
.thinking {
    padding-left: var(--md-space-16);
    border-left: 2px solid var(--md-outline-variant);
    color: var(--md-on-surface-variant);
    font-style: italic;
}
</style>
