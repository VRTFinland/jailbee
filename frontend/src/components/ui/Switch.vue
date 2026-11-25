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
    emit("update:modelValue", !props.modelValue)
}
</script>

<template>
    <button type="button" role="switch" class="switch" :aria-checked="modelValue ? 'true' : 'false'"
        :aria-label="label" :disabled="disabled" @click="toggle">
        <span class="switch-track"><span class="switch-handle ui-state-layer"></span></span>
    </button>
</template>

<style src="./state-layer.css"></style>

<style scoped>
/* The button is the 44px hit target; the track inside it is the Material 3
   switch, the handle growing from 16 to 24 when on. */
.switch {
    display: inline-grid;
    place-items: center;
    flex: none;
    min-height: var(--md-target-min);
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
    box-shadow: none;
}
.switch:focus-visible {
    outline: none;
}
.switch:focus-visible .switch-track {
    outline: var(--md-focus-ring-width) solid var(--md-primary);
    outline-offset: var(--md-focus-ring-offset);
}
.switch-track {
    position: relative;
    display: inline-flex;
    align-items: center;
    width: var(--md-switch-track-width);
    height: var(--md-switch-track-height);
    border: 2px solid var(--md-outline);
    border-radius: var(--md-shape-full);
    background: var(--md-surface-container-highest);
    transition: background-color var(--md-motion-duration) var(--md-motion-easing);
}
.switch[aria-checked="true"] .switch-track {
    border-color: var(--md-primary);
    background: var(--md-primary);
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
    /* The track's width less its two 2px borders, the handle and the inset. */
    left: calc(var(--md-switch-track-width) - var(--md-space-24) - var(--md-space-4) - var(--md-switch-handle-inset));
    width: var(--md-space-24);
    height: var(--md-space-24);
    background: var(--md-on-primary);
}
/* The state layer is a halo around the handle, as in Material 3. */
.switch-handle.ui-state-layer::before {
    inset: calc(var(--md-space-12) - var(--md-space-24));
    background: var(--md-on-surface);
}
.switch[aria-checked="true"] .switch-handle.ui-state-layer::before {
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
[data-theme="eink"] .switch:disabled .switch-track {
    border-style: dashed;
}
</style>
