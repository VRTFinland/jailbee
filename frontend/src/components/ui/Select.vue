<script setup lang="ts">
import {ref, useAttrs} from "vue"
import FieldFrame from "./FieldFrame.vue"
import {SelectRoot, SelectTrigger, SelectValue, SelectIcon, SelectPortal, SelectContent, SelectViewport, SelectItem, SelectItemText, SelectItemIndicator} from "reka-ui"
import Icon from "./Icon.vue"
import {useOverlayScope} from "./overlayScope"

export interface SelectOption {value: string; label: string; disabled?: boolean}
defineOptions({inheritAttrs: false})
withDefaults(defineProps<{
    id?: string
    help?: string
    error?: string
    label: string
    options: SelectOption[]
    placeholder?: string
    disabled?: boolean
    name?: string
    required?: boolean
}>(), {placeholder: "Choose an option", disabled: false, name: undefined, required: false})
const value = defineModel<string>({default: ""})
const open = defineModel<boolean>("open", {default: false})
const attrs = useAttrs()
const triggerAttrs = () => Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== "class"))
const anchor = ref<HTMLElement>()
const scope = useOverlayScope(anchor, open)
</script>

<template>
    <div ref="anchor">
        <FieldFrame :id="id" :label="label" :help="help" :error="error" :class="$attrs.class" v-slot="{controlAttrs}">
            <SelectRoot v-model="value" v-model:open="open" :disabled="disabled" :name="name" :required="required">
                <SelectTrigger v-bind="{...triggerAttrs(), ...controlAttrs}" class="ui-input ui-select ui-state-layer ui-focus-ring">
                    <SelectValue :placeholder="placeholder" />
                    <SelectIcon><Icon name="expand" size="small" /></SelectIcon>
                </SelectTrigger>
                <!-- Closed content keeps Reka's option-text registry, but stays out of body. -->
                <SelectPortal :disabled="!open">
                    <SelectContent v-bind="scope" position="popper" class="ui-overlay-scope ui-popup" :side-offset="4">
                        <SelectViewport>
                            <SelectItem v-for="option in options" :key="option.value" :value="option.value"
                                :disabled="option.disabled" class="ui-option ui-state-layer ui-focus-ring">
                                <SelectItemText>{{ option.label }}</SelectItemText>
                                <SelectItemIndicator><Icon name="success" size="small" /></SelectItemIndicator>
                            </SelectItem>
                        </SelectViewport>
                    </SelectContent>
                </SelectPortal>
            </SelectRoot>
        </FieldFrame>
    </div>
</template>

<style src="./state-layer.css"></style>
<style src="./overlays.css"></style>
<style scoped>
.ui-select {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-8);
    cursor: pointer;
}
</style>
