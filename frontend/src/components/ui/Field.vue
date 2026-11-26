<script setup lang="ts">
import FieldFrame from "./FieldFrame.vue"

defineOptions({inheritAttrs: false})
withDefaults(defineProps<{
    id?: string
    label: string
    help?: string
    error?: string
    type?: "text" | "password" | "email" | "search" | "url"
    disabled?: boolean
    multiline?: boolean
}>(), {type: "text", help: undefined, error: undefined, disabled: false})
const value = defineModel<string>({default: ""})
</script>

<template>
    <FieldFrame :id="id" :label="label" :help="help" :error="error" :class="$attrs.class" v-slot="{controlAttrs}">
        <textarea v-if="multiline" v-model="value" v-bind="controlAttrs($attrs)" :disabled="disabled"
            class="ui-input ui-focus-ring" />
        <input v-else v-model="value" v-bind="controlAttrs($attrs)" :type="type" :disabled="disabled"
            class="ui-input ui-focus-ring" />
    </FieldFrame>
</template>

<style src="./state-layer.css"></style>
