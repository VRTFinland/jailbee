<script setup lang="ts">
import {ref} from "vue"
import {DialogRoot, DialogTrigger, DialogPortal, DialogOverlay, DialogContent, DialogTitle, DialogDescription, DialogClose} from "reka-ui"
import IconButton from "./IconButton.vue"
import {useOverlayScope} from "./overlayScope"

withDefaults(defineProps<{
    title: string
    description?: string
    placement?: "dialog" | "drawer"
}>(), {placement: "dialog", description: undefined})
const open = defineModel<boolean>("open", {default: false})
const anchor = ref<HTMLElement>()
const scope = useOverlayScope(anchor, open)
</script>

<template>
    <span ref="anchor">
        <DialogRoot v-model:open="open">
            <DialogTrigger v-if="$slots.trigger" as-child><slot name="trigger" /></DialogTrigger>
            <DialogPortal>
                <div v-bind="scope" class="ui-overlay-scope">
                    <DialogOverlay class="ui-dialog-scrim" />
                    <DialogContent class="ui-dialog ui-focus-ring" :data-placement="placement"
                        aria-modal="true" v-bind="description ? {} : {'aria-describedby': undefined}">
                        <header class="ui-dialog-header">
                            <DialogTitle class="ui-dialog-title">{{ title }}</DialogTitle>
                            <DialogClose as-child><IconButton icon="close" label="Close dialog" /></DialogClose>
                        </header>
                        <DialogDescription v-if="description" class="ui-dialog-description">{{ description }}</DialogDescription>
                        <slot :close="() => open = false" />
                    </DialogContent>
                </div>
            </DialogPortal>
        </DialogRoot>
    </span>
</template>

<style src="./state-layer.css"></style>
<style src="./overlays.css"></style>
<style scoped>
.ui-dialog-scrim {
    position: fixed;
    inset: 0;
    z-index: var(--md-layer-scrim);
    background: var(--md-scrim);
}
.ui-dialog {
    position: fixed;
    z-index: var(--md-layer-dialog);
    inset: auto 0 0;
    max-height: 90dvh;
    overflow: auto;
    padding: var(--md-space-24);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-extra-large) var(--md-shape-extra-large) 0 0;
    background: var(--md-surface-container-high);
    color: var(--md-on-surface);
    font-family: var(--md-font-body);
    font-size: var(--md-type-body-large-size);
    line-height: var(--md-type-body-large-line-height);
    animation: ui-overlay-appear var(--md-motion-duration) var(--md-motion-easing);
}
.ui-dialog-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-16);
}
.ui-dialog-title {
    margin: 0;
    font-size: var(--md-type-title-large-size);
    line-height: var(--md-type-title-large-line-height);
    font-weight: var(--md-type-title-large-weight);
}
.ui-dialog-description {
    margin: var(--md-space-8) 0 var(--md-space-16);
    color: var(--md-on-surface-variant);
}
@media (min-width: 48rem) {
    .ui-dialog[data-placement="dialog"] {
        inset: var(--md-dialog-center) auto auto var(--md-dialog-center);
        translate: var(--md-dialog-center-offset);
        width: min(32rem, 90vw);
        border-radius: var(--md-shape-extra-large);
    }
    .ui-dialog[data-placement="drawer"] {
        inset: 0 0 0 auto;
        width: min(28rem, 90vw);
        max-height: 100dvh;
        border-radius: var(--md-shape-extra-large) 0 0 var(--md-shape-extra-large);
    }
}
</style>
