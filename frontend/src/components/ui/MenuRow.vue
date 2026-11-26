<script setup lang="ts">
import {RouterLink, type RouteLocationRaw} from "vue-router"
withDefaults(defineProps<{
    action?: boolean
    to?: RouteLocationRaw
    as?: "div" | "label"
    selected?: boolean
    sectionStart?: boolean
}>(), {action: false, as: "div", selected: undefined, sectionStart: false})
defineEmits<{click: [event: MouseEvent]}>()
</script>

<template>
    <component :is="to ? RouterLink : action ? 'button' : as" :to="to" class="ui-menu-row ui-option" :class="{'ui-state-layer ui-focus-ring': action || to}"
        :type="action ? 'button' : undefined" :aria-pressed="selected" :data-state="selected ? 'checked' : undefined"
        :data-section-start="sectionStart || undefined" @click="$emit('click', $event)"><slot /></component>
</template>

<style src="./state-layer.css"></style>
<style src="./overlays.css"></style>
<style scoped>
.ui-menu-row {
    width: 100%;
    margin: 0;
    justify-content: flex-start;
    background: transparent;
    color: var(--md-on-surface);
    border: 1px solid transparent;
    font: inherit;
    text-align: start;
    cursor: default;
}
button.ui-menu-row, a.ui-menu-row { cursor: pointer; text-decoration: none; }
.ui-menu-row[data-state="checked"] { background: var(--md-primary-container); color: var(--md-on-primary-container); border-color: var(--md-hairline); }
.ui-menu-row[data-section-start] { border-top-color: var(--md-outline-variant); padding-top: var(--md-space-16); }
</style>
