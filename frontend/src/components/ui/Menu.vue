<script setup lang="ts">
import {ref} from "vue"
import {DropdownMenuRoot, DropdownMenuTrigger, DropdownMenuPortal, DropdownMenuContent, DropdownMenuItem} from "reka-ui"
import {useOverlayScope} from "./overlayScope"

export interface MenuItem {value: string; label: string; disabled?: boolean}
defineProps<{label: string; items: MenuItem[]}>()
defineEmits<{select: [value: string]}>()
const open = defineModel<boolean>("open", {default: false})
const anchor = ref<HTMLElement>()
const scope = useOverlayScope(anchor, open)
</script>

<template>
    <span ref="anchor">
        <DropdownMenuRoot v-model:open="open">
            <DropdownMenuTrigger as-child><slot name="trigger" /></DropdownMenuTrigger>
            <DropdownMenuPortal>
                <div v-bind="scope" class="ui-overlay-scope">
                    <DropdownMenuContent :aria-label="label" :aria-labelledby="undefined" class="ui-popup" :side-offset="4" align="end" loop>
                        <DropdownMenuItem v-for="item in items" :key="item.value" :disabled="item.disabled"
                            class="ui-option ui-state-layer ui-focus-ring" @select="$emit('select', item.value)">
                            {{ item.label }}
                        </DropdownMenuItem>
                    </DropdownMenuContent>
                </div>
            </DropdownMenuPortal>
        </DropdownMenuRoot>
    </span>
</template>

<style src="./state-layer.css"></style>
<style src="./overlays.css"></style>
