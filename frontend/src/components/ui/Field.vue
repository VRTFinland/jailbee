<script setup lang="ts">
import {useAttrs} from "vue"
import FieldFrame from "./FieldFrame.vue"

defineOptions({inheritAttrs: false})
withDefaults(defineProps<{
    id?: string
    label: string
    help?: string
    error?: string
    type?: "text" | "password" | "email" | "search" | "url"
    disabled?: boolean
}>(), {type: "text", help: undefined, error: undefined, disabled: false})
const value = defineModel<string>({default: ""})
const attrs = useAttrs()
const inputAttrs = () => Object.fromEntries(Object.entries(attrs).filter(([key]) => key !== "class"))
</script>

<template>
    <FieldFrame :id="id" :label="label" :help="help" :error="error" :class="$attrs.class" v-slot="{controlAttrs}">
        <input v-model="value" v-bind="{...inputAttrs(), ...controlAttrs}" :type="type" :disabled="disabled"
            class="ui-input ui-focus-ring" />
    </FieldFrame>
</template>

<style src="./state-layer.css"></style>
