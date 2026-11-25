<script setup lang="ts">
import {computed, useId} from "vue"

defineOptions({inheritAttrs: false})
const props = withDefaults(defineProps<{
    label: string
    help?: string
    error?: string
    type?: "text" | "password" | "email" | "search" | "url"
    disabled?: boolean
}>(), {type: "text", help: undefined, error: undefined, disabled: false})
const value = defineModel<string>({default: ""})
const id = useId()
const describedBy = computed(() => [props.help && `${id}-help`, props.error && `${id}-error`].filter(Boolean).join(" ") || undefined)
</script>

<template>
    <div class="ui-field">
        <label :for="id" class="ui-field-label">{{ label }}</label>
        <input :id="id" v-model="value" v-bind="$attrs" :type="type" :disabled="disabled"
            :aria-describedby="describedBy" :aria-invalid="error ? true : undefined"
            class="ui-input ui-focus-ring" />
        <p v-if="help" :id="`${id}-help`" class="ui-field-help">{{ help }}</p>
        <p v-if="error" :id="`${id}-error`" class="ui-field-error">{{ error }}</p>
    </div>
</template>

<style src="./state-layer.css"></style>
<style src="./fields.css"></style>
