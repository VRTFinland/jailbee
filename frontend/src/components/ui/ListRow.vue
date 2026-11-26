<script setup lang="ts">
// A list row (#167): a title, an optional secondary line, block content in
// the default slot (orchestration lines, `<dl>` cells) and a trailing slot (a
// StatusChip, a Switch, an IconButton). The selected row is tonal and carries
// `aria-current`; its outline is the hairline token, drawn only in e-ink,
// where the tonal fill is white. The row is 48px tall for a fine pointer and
// 56px for a coarse one.
//
// With `to`, the title is a router link whose hit area stretches over the
// whole row, with the state layer and focus ring on the row; `aria-current`
// moves to the link. Trailing controls and links in the body stay outside
// that link (no interactive content nested in an anchor) and above its area.
import type {RouteLocationRaw} from "vue-router"

defineProps<{
    title?: string
    secondary?: string
    selected?: boolean
    to?: RouteLocationRaw
    /** Wrap long titles and trailing chips before the full-width body. */
    wrap?: boolean
    /** Supporting ancestor retained for orientation, not a primary result. */
    context?: boolean
}>()
</script>

<template>
    <li class="list-row" :class="{selected, wrap, 'filter-context': context, linked: to !== undefined, 'ui-state-layer': to !== undefined}"
        :aria-current="selected && to === undefined ? 'true' : undefined">
        <span class="list-row-title">
            <router-link v-if="to !== undefined" :to="to" class="list-row-link"
                :aria-current="selected ? 'true' : undefined"><slot name="title">{{ title }}</slot></router-link>
            <slot v-else name="title">{{ title }}</slot>
        </span>
        <span v-if="secondary || $slots.secondary" class="list-row-secondary"><slot name="secondary">{{
            secondary }}</slot></span>
        <div v-if="$slots.default" class="list-row-body"><slot /></div>
        <span v-if="$slots.trailing" class="list-row-trailing"><slot name="trailing" /></span>
    </li>
</template>

<style src="./state-layer.css"></style>

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
.list-row.filter-context {
    opacity: var(--md-context-opacity);
}
[data-theme="eink"] .list-row.filter-context {
    border-style: dashed;
}
.list-row.wrap {
    display: flex;
    flex-wrap: wrap;
    row-gap: var(--md-space-8);
}
.list-row.wrap .list-row-title {
    flex: 1 1 auto;
    white-space: normal;
    overflow-wrap: anywhere;
}
.list-row.wrap .list-row-trailing { order: 1; max-width: 100%; }
.list-row.wrap .list-row-secondary { order: 2; flex-basis: 100%; }
.list-row.wrap .list-row-body { order: 3; flex-basis: 100%; }
.list-row.selected {
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    outline: 2px solid var(--md-hairline);
    outline-offset: calc(-1 * var(--md-space-4) / 2);
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
.list-row-title :slotted(a),
.list-row-link {
    color: inherit;
}
.list-row-link {
    text-decoration: none;
}
/* The link's hit area is the whole row. */
.list-row-link::after {
    content: "";
    position: absolute;
    inset: 0;
    border-radius: inherit;
}
.list-row-link:focus-visible {
    outline: none;
}
.list-row.linked:has(.list-row-link:focus-visible) {
    outline: var(--md-focus-ring-width) solid var(--md-primary);
    outline-offset: var(--md-focus-ring-offset);
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
.list-row-body {
    grid-column: 1 / -1;
    min-width: 0;
    font-size: var(--md-type-body-small-size);
    line-height: var(--md-type-body-small-line-height);
}
/* Controls in the body and trailing slots are their own targets: positioned
   after the link in source order, they paint above its area, and hovering or
   pressing one does not light the row. Text and chips beside them stay part
   of the row's link. */
.list-row-body :deep(:is(a, button, input, select, textarea, summary, label, [tabindex])),
.list-row-trailing :deep(:is(a, button, input, select, textarea, summary, label, [tabindex])) {
    position: relative;
}
.list-row.linked:has(:is(.list-row-body, .list-row-trailing) :is(a, button, input, select, textarea, summary, label, [tabindex]):hover)::before {
    opacity: 0;
}
.list-row-trailing {
    grid-column: 2;
    grid-row: 1 / span 2;
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-8);
}
</style>
