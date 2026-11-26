<script setup lang="ts">
// A pill button (#167): primary for the one main action, tonal for the
// others, text for low emphasis (Cancel), danger for a destructive action.
import Icon from "./Icon.vue"
import type {IconName} from "./material-symbols/paths"

withDefaults(defineProps<{
    variant?: "primary" | "tonal" | "text" | "danger"
    type?: "button" | "submit"
    /** Compact reader chrome; the hit area still extends to 44px above it. */
    compact?: boolean
    /** One-line excerpt for prompt navigation; the full text stays its name. */
    truncate?: boolean
    /** Narrow page counters retain a full hit target without wide pill insets. */
    narrow?: boolean
    disabled?: boolean
    /** A leading icon, decorative: the content names the button. */
    icon?: IconName
}>(), {variant: "tonal", type: "button", compact: false, disabled: false, icon: undefined})
defineEmits<{click: [event: MouseEvent]}>()
</script>

<template>
    <button :type="type" class="button ui-state-layer ui-focus-ring" :data-variant="variant" :data-compact="compact || undefined" :data-truncate="truncate || undefined" :data-narrow="narrow || undefined" :disabled="disabled"
        @click="$emit('click', $event)">
        <Icon v-if="icon" :name="icon" size="small" />
        <span class="button-label"><slot /></span>
    </button>
</template>

<style src="./state-layer.css"></style>

<style scoped>
.button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-8);
    min-height: var(--md-target-min);
    padding: 0 var(--md-space-24);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-full);
    font-family: var(--md-font-body);
    font-size: var(--md-type-label-large-size);
    line-height: var(--md-type-label-large-line-height);
    font-weight: var(--md-type-label-large-weight);
    cursor: pointer;
    isolation: isolate;
    overflow: hidden;
    box-shadow: none;
}
.button[data-variant="primary"] {
    background: var(--md-primary);
    color: var(--md-on-primary);
}
.button[data-variant="tonal"] {
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
}
.button[data-variant="text"] {
    padding: 0 var(--md-space-12);
    background: transparent;
    border-color: transparent;
    color: var(--md-primary);
}
.button[data-variant="danger"] {
    background: var(--md-error);
    color: var(--md-on-error);
}
.button:disabled {
    cursor: default;
    color: color-mix(in srgb, var(--md-on-surface) calc(var(--md-disabled-content-opacity) * 100%), transparent);
    background: color-mix(in srgb, var(--md-on-surface) calc(var(--md-disabled-container-opacity) * 100%), transparent);
}
.button[data-variant="text"]:disabled {
    background: transparent;
}
/* E-ink has no greys: a disabled button keeps black ink and a dashed edge. */
[data-theme="eink"] .button:disabled {
    border-style: dashed;
    border-color: var(--md-hairline);
    background: var(--md-surface);
    color: var(--md-on-surface);
}
/* The full target extends upwards, never into the reader's bottom guard. */
.button[data-compact] {
    min-width: var(--md-target-min);
    min-height: 0;
    height: var(--md-space-24);
    padding: 0 var(--md-space-4);
    overflow: visible;
}
.button[data-compact]::after {
    content: "";
    position: absolute;
    bottom: 0;
    left: 0;
    width: 100%;
    height: var(--md-target-min);
}
.button[data-narrow] {
    min-width: var(--md-target-min);
    padding-inline: var(--md-space-4);
}
.button[data-truncate] {
    min-width: 0;
    max-width: 100%;
    justify-content: flex-start;
}
.button[data-truncate] .button-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.button-label {
    min-width: 0;
}
</style>
