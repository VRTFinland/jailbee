<script setup lang="ts">
import {ICON_PATHS, type IconName} from "./material-symbols/paths"

withDefaults(defineProps<{
    name: IconName
    size?: "small" | "medium" | "large"
    label?: string
}>(), {size: "medium"})
</script>

<template>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" class="icon"
        :style="{width: `var(--md-icon-${size})`, height: `var(--md-icon-${size})`}"
        :role="label ? 'img' : undefined" :aria-label="label || undefined"
        :aria-hidden="label ? undefined : true" focusable="false" fill="currentColor">
        <path :d="ICON_PATHS[name]" />
    </svg>
</template>

<style scoped>
.icon {
    display: inline-block;
    flex: none;
    vertical-align: middle;
}
</style>
