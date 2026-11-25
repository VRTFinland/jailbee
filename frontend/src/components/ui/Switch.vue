<script setup lang="ts">
// An on/off switch (#167), `v-model` bound. A button with the switch role:
// the label names it, `aria-checked` carries the state. The caller shows the
// visible wording beside it (a ListRow title, a settings line).
const props = withDefaults(defineProps<{
    modelValue: boolean
    label: string
    disabled?: boolean
}>(), {disabled: false})
const emit = defineEmits<{"update:modelValue": [value: boolean]}>()

function toggle(): void {
    if (!props.disabled) emit("update:modelValue", !props.modelValue)
}
</script>

<template>
    <button type="button" role="switch" class="switch focus-ring" :aria-checked="modelValue ? 'true' : 'false'"
        :aria-label="label" :disabled="disabled" @click="toggle">
        <span class="switch-handle state-layer"></span>
    </button>
</template>

<style src="./state-layer.css"></style>

<style scoped>
/* Material 3 switch: a 52x32 track; the handle grows from 16 to 24 when on. */
.switch {
    position: relative;
    display: inline-flex;
    align-items: center;
    flex: none;
    width: 3.25rem;
    height: 2rem;
    padding: 0;
    border: 2px solid var(--md-outline);
    border-radius: var(--md-shape-full);
    background: var(--md-surface-container-highest);
    color: var(--md-on-surface);
    cursor: pointer;
    box-shadow: none;
    transition: background-color var(--md-motion-duration) var(--md-motion-easing);
}
.switch[aria-checked="true"] {
    border-color: var(--md-primary);
    background: var(--md-primary);
    color: var(--md-primary);
}
.switch-handle {
    position: absolute;
    left: var(--md-space-4);
    width: var(--md-space-16);
    height: var(--md-space-16);
    border-radius: var(--md-shape-full);
    background: var(--md-outline);
    transition: left var(--md-motion-duration) var(--md-motion-easing),
        width var(--md-motion-duration) var(--md-motion-easing),
        height var(--md-motion-duration) var(--md-motion-easing);
}
.switch[aria-checked="true"] .switch-handle {
    left: calc(100% - var(--md-space-24) - var(--md-space-4) / 2);
    width: var(--md-space-24);
    height: var(--md-space-24);
    background: var(--md-on-primary);
}
/* The state layer is a halo around the handle, as in Material 3. */
.switch-handle.state-layer::before {
    inset: calc(-1 * var(--md-space-12));
    background: var(--md-on-surface);
}
.switch[aria-checked="true"] .switch-handle.state-layer::before {
    background: var(--md-primary);
}
.switch:hover:not(:disabled) .switch-handle::before {
    opacity: var(--md-state-hover-opacity);
}
.switch:active:not(:disabled) .switch-handle::before {
    opacity: var(--md-state-pressed-opacity);
}
.switch:disabled {
    cursor: default;
    opacity: var(--md-disabled-content-opacity);
}
/* E-ink has no greys: a disabled switch keeps black ink and a dashed track. */
[data-theme="eink"] .switch:disabled {
    border-style: dashed;
}
</style>
