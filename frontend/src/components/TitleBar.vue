<script setup lang="ts">
import {nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue"
import QuickSettings from "./QuickSettings.vue"
import Icon from "./ui/Icon.vue"
import {settings} from "../settings"
import type {RouteLocationRaw} from "vue-router"

const props = defineProps<{title: string; crumbs?: {label: string; to: RouteLocationRaw}[]; context?: "containers" | "session" | null; session?: {agent: string; models: string[]; id: string} | null}>()
const emit = defineEmits<{"readable-top": [bottom: number]}>()
const bar = ref<HTMLElement | null>(null)
let observer: ResizeObserver | null = null
defineExpose({element: () => bar.value})
function publishReadableTop() {
    const boundary = bar.value ?? document.querySelector<HTMLElement>(".outdated")
    emit("readable-top", Math.max(0, boundary?.getBoundingClientRect().bottom ?? 0))
}

onMounted(() => {
    publishReadableTop()
    if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(publishReadableTop)
        if (bar.value) observer.observe(bar.value)
    }
    window.addEventListener("scroll", publishReadableTop, {passive: true})
    window.addEventListener("resize", publishReadableTop)
    window.addEventListener("orientationchange", publishReadableTop)
})
watch(() => [props.title, props.crumbs, settings.showTitleBar], () => nextTick(() => {
    if (bar.value) observer?.observe(bar.value)
    publishReadableTop()
}), {deep: true})
onBeforeUnmount(() => {
    observer?.disconnect()
    window.removeEventListener("scroll", publishReadableTop)
    window.removeEventListener("resize", publishReadableTop)
    window.removeEventListener("orientationchange", publishReadableTop)
})
</script>

<template>
    <header v-if="settings.showTitleBar" ref="bar" class="title-bar">
        <nav aria-label="Breadcrumb" class="breadcrumbs">
            <template v-for="(crumb, index) in crumbs" :key="index">
                <router-link :to="crumb.to" :title="crumb.label" :aria-label="crumb.label" ><span>{{ crumb.label }}</span></router-link><span class="separator" aria-hidden="true"><Icon name="forward" size="small" /></span>
            </template>
            <h1 :title="session ? `${title}\nAgent: ${session.agent}\nModels: ${session.models.join(', ')}\nSession ID: ${session.id}` : title" :aria-label="title" aria-current="page">{{ title }}</h1>
        </nav>
        <slot />
        <QuickSettings class="title-menu" :context="context" :session="session" />
    </header>
    <QuickSettings v-else :context="context" :session="session" :parent="crumbs?.at(-1)" />
</template>

<style scoped>
.title-bar { position: sticky; top: var(--app-banner-height, 0); z-index: var(--md-layer-sticky); display: flex; align-items: center; box-sizing: border-box; height: var(--title-bar-height); padding-inline: var(--md-space-12) var(--md-space-4); background: var(--md-surface-container); border-bottom: 1px solid var(--md-outline-variant); min-width: 0; }
.breadcrumbs { display: flex; align-items: center; flex: 1; min-width: 0; overflow: hidden; }
.breadcrumbs a { display: flex; flex: 0 1 auto; max-width: 16ch; align-items: center; height: var(--title-bar-height); min-width: 3ch; overflow: hidden; white-space: nowrap; color: var(--md-on-surface-variant); font-size: var(--md-type-body-medium-size); text-decoration: none; }
.breadcrumbs a:focus-visible { outline: var(--md-focus-ring-width) solid var(--md-primary); outline-offset: calc(-1 * var(--md-focus-ring-offset)); }
.breadcrumbs a span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.breadcrumbs a:first-child { flex-shrink: 0; max-width: none; }
.separator { display: inline-flex; align-items: center; flex: none; padding: 0 var(--md-space-4); color: var(--md-on-surface-variant); }
h1 { flex: 0 1 auto; max-width: 24ch; font-size: var(--md-type-body-medium-size); line-height: var(--md-type-body-medium-line-height); font-weight: var(--md-type-emphasis-weight); margin: 0; min-width: 3ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
