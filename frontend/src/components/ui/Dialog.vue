<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, ref, watch} from "vue"
import {DialogRoot, DialogTrigger, DialogPortal, DialogOverlay, DialogContent, DialogTitle, DialogDescription, DialogClose} from "reka-ui"
import IconButton from "./IconButton.vue"
import {useOverlayScope} from "./overlayScope"

defineOptions({inheritAttrs: false})
const props = withDefaults(defineProps<{
    title: string
    description?: string
    placement?: "dialog" | "drawer" | "popover"
    closeLabel?: string
    /** Live readers keep following the document while preferences are open. */
    preserveScroll?: boolean
    /** Paginated forms account for their own header and dismissal control. */
    hideHeader?: boolean
}>(), {placement: "dialog", description: undefined, closeLabel: "Close dialog", preserveScroll: false, hideHeader: false})
const emit = defineEmits<{positioned: []}>()
const open = defineModel<boolean>("open", {default: false})
const anchor = ref<HTMLElement>()
const scope = useOverlayScope(anchor, open)
const position = ref<Record<string, string>>()
const anchored = computed(() => props.placement === "popover")
function place() {
    if (!anchored.value || !anchor.value) return
    const trigger = anchor.value.querySelector("button")?.getBoundingClientRect()
    if (!trigger) return
    position.value = {
        top: `calc(${trigger.bottom}px + var(--md-space-4))`,
        right: `clamp(var(--md-space-4), ${window.innerWidth - trigger.right}px, calc(100vw - min(22rem, 100vw - var(--md-space-32)) - var(--md-space-4)))`,
    }
    void nextTick(() => emit("positioned"))
}
watch(open, async value => {
    if (value) {
        await nextTick()
        place()
        window.addEventListener("resize", place)
    } else window.removeEventListener("resize", place)
})
onBeforeUnmount(() => window.removeEventListener("resize", place))
</script>

<template>
    <span ref="anchor">
        <DialogRoot v-model:open="open">
            <DialogTrigger v-if="$slots.trigger" as-child><slot name="trigger" /></DialogTrigger>
            <DialogPortal v-if="open">
                <div v-bind="scope" class="ui-overlay-scope" :data-dialog-placement="placement">
                    <div v-if="preserveScroll" class="ui-dialog-scrim" @pointerdown.left.self.prevent />
                    <DialogOverlay v-else class="ui-dialog-scrim" />
                    <DialogContent class="ui-dialog ui-focus-ring" :class="{'ui-popup': anchored}" :data-placement="placement" :style="position"
                        aria-modal="true" v-bind="{...$attrs, ...(!description && {'aria-describedby': undefined})}">
                        <header class="ui-dialog-header" :class="{'ui-dialog-hidden-header': hideHeader}">
                            <DialogTitle class="ui-dialog-title">{{ title }}</DialogTitle>
                            <DialogClose v-if="!hideHeader" as-child><IconButton icon="close" :label="closeLabel" /></DialogClose>
                        </header>
                        <DialogDescription v-if="description" class="ui-dialog-description">{{ description }}</DialogDescription>
                        <slot :close="() => open = false" />
                        <footer v-if="$slots.actions" class="ui-dialog-actions">
                            <slot name="actions" :close="() => open = false" />
                        </footer>
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
    pointer-events: auto;
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
.ui-dialog[data-placement="popover"] {
    inset: auto;
    width: min(22rem, calc(100vw - var(--md-space-32)));
    max-height: none;
    padding: var(--md-space-8);
    border-radius: var(--md-shape-extra-small);
    background: var(--md-surface-container);
    font-size: var(--md-type-body-medium-size);
    line-height: var(--md-type-body-medium-line-height);
}
.ui-dialog-hidden-header {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
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
.ui-dialog-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--md-space-8);
    margin-top: var(--md-space-16);
}
.ui-dialog[data-placement="drawer"] {
    border-radius: var(--md-shape-large) var(--md-shape-large) 0 0;
    animation: ui-sheet-up var(--md-motion-sheet-duration) var(--md-motion-easing);
}
[data-dialog-placement="drawer"] .ui-dialog-scrim {
    animation: ui-overlay-appear var(--md-motion-sheet-duration) var(--md-motion-easing);
}
@keyframes ui-sheet-up { from { transform: translateY(100%); } to { transform: translateY(0); } }
@keyframes ui-sheet-in { from { transform: translateX(100%); } to { transform: translateX(0); } }
@media (min-width: 48rem) {
    .ui-dialog[data-placement="dialog"] {
        inset: 0;
        margin: auto;
        height: fit-content;
        width: min(32rem, 90vw);
        border-radius: var(--md-shape-extra-large);
    }
    .ui-dialog[data-placement="drawer"] {
        inset: 0 0 0 auto;
        width: min(28rem, 90vw);
        max-height: 100dvh;
        border-radius: var(--md-shape-large) 0 0 var(--md-shape-large);
        animation-name: ui-sheet-in;
    }
}
</style>
