<script setup lang="ts">
// A round icon-only button (#167) with the 44px hit area. The label is
// required: it names the button for assistive technology and as a tooltip.
import Icon from "./Icon.vue"
import type {IconName} from "./material-symbols/paths"

withDefaults(defineProps<{
    icon: IconName
    label: string
    disabled?: boolean
}>(), {disabled: false})
defineEmits<{click: [event: MouseEvent]}>()
</script>

<template>
    <button type="button" class="icon-button ui-state-layer ui-focus-ring" :aria-label="label" :title="label"
        :disabled="disabled" @click="$emit('click', $event)">
        <Icon :name="icon" />
    </button>
</template>

<style src="./state-layer.css"></style>

<style scoped>
.icon-button {
    display: inline-grid;
    place-items: center;
    flex: none;
    width: var(--md-target-min);
    height: var(--md-target-min);
    padding: 0;
    border: 0;
    border-radius: var(--md-shape-full);
    background: transparent;
    color: var(--md-on-surface-variant);
    cursor: pointer;
    box-shadow: none;
}
.icon-button:disabled {
    cursor: default;
    color: color-mix(in srgb, var(--md-on-surface) calc(var(--md-disabled-content-opacity) * 100%), transparent);
}
/* E-ink has no greys: a disabled icon button keeps black ink and a dashed ring. */
[data-theme="eink"] .icon-button:disabled {
    color: var(--md-on-surface);
    outline: 1px dashed var(--md-hairline);
    outline-offset: calc(-1 * var(--md-space-4));
}
</style>
