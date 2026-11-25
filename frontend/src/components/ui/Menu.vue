<script setup lang="ts">
import {ref} from "vue"
import {DropdownMenuRoot, DropdownMenuTrigger, DropdownMenuPortal, DropdownMenuContent, DropdownMenuItem} from "reka-ui"
import {useOverlayScope} from "./overlayScope"
import Icon from "./Icon.vue"
import type {IconName} from "./material-symbols/paths"

export interface MenuItem {value: string; label: string; disabled?: boolean; variant?: "danger"; icon?: IconName}
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
                <div v-if="open" v-bind="scope" class="ui-overlay-scope">
                    <DropdownMenuContent :aria-label="label" :aria-labelledby="undefined" class="ui-popup" :side-offset="4" align="end" loop>
                        <DropdownMenuItem v-for="item in items" :key="item.value" :disabled="item.disabled"
                            class="ui-option ui-state-layer ui-focus-ring" :data-variant="item.variant" @select="$emit('select', item.value)">
                            <Icon v-if="item.icon" :name="item.icon" size="small" />
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
