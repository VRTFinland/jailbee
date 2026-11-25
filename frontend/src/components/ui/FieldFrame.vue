<script setup lang="ts">
import {computed, useId} from "vue"

defineOptions({inheritAttrs: false})
const props = defineProps<{id?: string; label: string; help?: string; error?: string}>()
const generatedId = useId()
const controlId = computed(() => props.id ?? generatedId)
const controlAttrs = computed(() => ({
    id: controlId.value,
    "aria-labelledby": `${controlId.value}-label`,
    "aria-describedby": [props.help && `${controlId.value}-help`, props.error && `${controlId.value}-error`]
        .filter(Boolean).join(" ") || undefined,
    "aria-invalid": props.error ? true : undefined,
}))
</script>

<template>
    <div class="ui-field" :class="$attrs.class">
        <label :id="`${controlId}-label`" :for="controlId" class="ui-field-label">{{ label }}</label>
        <slot :control-attrs="controlAttrs" />
        <p v-if="help" :id="`${controlId}-help`" class="ui-field-help">{{ help }}</p>
        <p v-if="error" :id="`${controlId}-error`" class="ui-field-error" role="alert">{{ error }}</p>
    </div>
</template>

<style src="./fields.css"></style>
