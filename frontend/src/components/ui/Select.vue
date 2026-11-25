<script setup lang="ts">
import {ref, useId} from "vue"
import {SelectRoot, SelectTrigger, SelectValue, SelectIcon, SelectPortal, SelectContent, SelectViewport, SelectItem, SelectItemText, SelectItemIndicator} from "reka-ui"
import Icon from "./Icon.vue"
import {useOverlayScope} from "./overlayScope"

export interface SelectOption {value: string; label: string; disabled?: boolean}
withDefaults(defineProps<{
    label: string
    options: SelectOption[]
    placeholder?: string
    disabled?: boolean
    name?: string
    required?: boolean
}>(), {placeholder: "Choose an option", disabled: false, name: undefined, required: false})
const value = defineModel<string>({default: ""})
const open = defineModel<boolean>("open", {default: false})
const id = useId()
const anchor = ref<HTMLElement>()
const scope = useOverlayScope(anchor, open)
</script>

<template>
    <div ref="anchor" class="ui-field">
        <label :id="`${id}-label`" :for="id" class="ui-field-label">{{ label }}</label>
        <SelectRoot v-model="value" v-model:open="open" :disabled="disabled" :name="name" :required="required">
            <SelectTrigger :id="id" :aria-labelledby="`${id}-label`" class="ui-input ui-select ui-state-layer ui-focus-ring">
                <SelectValue :placeholder="placeholder" />
                <SelectIcon><Icon name="expand" size="small" /></SelectIcon>
            </SelectTrigger>
            <SelectPortal>
                <div v-bind="scope" class="ui-overlay-scope">
                    <SelectContent position="popper" class="ui-popup" :side-offset="4">
                        <SelectViewport>
                            <SelectItem v-for="option in options" :key="option.value" :value="option.value"
                                :disabled="option.disabled" class="ui-option ui-state-layer ui-focus-ring">
                                <SelectItemText>{{ option.label }}</SelectItemText>
                                <SelectItemIndicator><Icon name="success" size="small" /></SelectItemIndicator>
                            </SelectItem>
                        </SelectViewport>
                    </SelectContent>
                </div>
            </SelectPortal>
        </SelectRoot>
    </div>
</template>

<style src="./state-layer.css"></style>
<style src="./fields.css"></style>
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
