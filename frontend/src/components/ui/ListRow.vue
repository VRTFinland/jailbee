<script setup lang="ts">
// A list row (#167): a title, an optional secondary line and a trailing slot
// (a StatusChip, a Switch, an IconButton). The selected row is tonal and
// carries `aria-current`; e-ink, with no tints, outlines it instead. The row
// is 48px tall for a fine pointer and 56px for a coarse one.
defineProps<{
    title?: string
    secondary?: string
    selected?: boolean
}>()
</script>

<template>
    <li class="list-row" :class="{selected}" :aria-current="selected ? 'true' : undefined">
        <span class="list-row-title"><slot name="title">{{ title }}</slot></span>
        <span v-if="secondary || $slots.secondary" class="list-row-secondary"><slot name="secondary">{{
            secondary }}</slot></span>
        <span v-if="$slots.trailing" class="list-row-trailing"><slot name="trailing" /></span>
    </li>
</template>

<style scoped>
.list-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    column-gap: var(--md-space-12);
    min-height: var(--md-target-row);
    padding: var(--md-space-8) var(--md-space-16);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-large);
    background: var(--md-surface-container-lowest);
    color: var(--md-on-surface);
}
.list-row.selected {
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    outline: 2px solid var(--md-hairline);
    outline-offset: calc(-1 * var(--md-focus-ring-width));
}
.list-row-title {
    grid-column: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--md-type-title-small-size);
    line-height: var(--md-type-title-small-line-height);
    font-weight: var(--md-type-title-small-weight);
}
.list-row-title :slotted(a) {
    color: inherit;
}
.list-row-secondary {
    grid-column: 1;
    min-width: 0;
    color: var(--md-on-surface-variant);
    font-size: var(--md-type-body-small-size);
    line-height: var(--md-type-body-small-line-height);
}
.list-row.selected .list-row-secondary {
    color: inherit;
}
.list-row-trailing {
    grid-column: 2;
    grid-row: 1 / span 2;
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-8);
}
</style>
