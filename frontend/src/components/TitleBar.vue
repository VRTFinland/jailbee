<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue"
import QuickSettings from "./QuickSettings.vue"
import {settings} from "../settings"
import type {RouteLocationRaw} from "vue-router"

const props = defineProps<{title: string; crumbs?: {label: string; to: RouteLocationRaw}[]; context?: "containers" | "session" | null; session?: {agent: string; models: string[]; id: string} | null}>()
const emit = defineEmits<{"readable-top": [bottom: number]}>()
const bar = ref<HTMLElement | null>(null)
defineExpose({element: () => bar.value})
const breadcrumb = ref<HTMLElement | null>(null)
const measurements = ref<HTMLElement | null>(null)
const firstCrumb = ref(0)
const parentWidth = ref<number | null>(null)
const collapsed = ref(false)
const visibleCrumbs = computed(() => (props.crumbs ?? []).slice(firstCrumb.value))
let observer: ResizeObserver | null = null

function publishReadableTop() {
    const boundary = bar.value ?? document.querySelector<HTMLElement>(".outdated")
    emit("readable-top", Math.max(0, boundary?.getBoundingClientRect().bottom ?? 0))
}

function measure() {
    publishReadableTop()
    const available = breadcrumb.value?.clientWidth ?? 0
    const probe = measurements.value
    if (!available || !probe) return
    const widths = Array.from(probe.querySelectorAll<HTMLElement>(".crumb-label"), el => el.getBoundingClientRect().width)
    const titleWidth = probe.querySelector(".title-measure")!.getBoundingClientRect().width
    const separatorWidth = probe.querySelector(".separator-measure")!.getBoundingClientRect().width
    // Ten characters keep a truncated parent useful; shorter names need less room.
    const parentCharacterFloor = probe.querySelector(".minimum-parent")!.getBoundingClientRect().width
    const minimumTitle = probe.querySelector(".minimum-title")!.getBoundingClientRect().width
    let start = 0
    const pathWidth = () => widths.slice(start).reduce((sum, width) => sum + width + separatorWidth, titleWidth)
    // Always measure the full labels, so widening restores levels we removed.
    while (start < widths.length - 1 && pathWidth() > available) start++
    firstCrumb.value = start
    parentWidth.value = null
    collapsed.value = false
    if (widths.length && pathWidth() > available) {
        const titleBudget = Math.min(titleWidth, Math.max(minimumTitle, available * 2 / 3))
        const remaining = available - separatorWidth - titleBudget
        if (remaining < Math.min(widths[start]!, parentCharacterFloor)) collapsed.value = true
        else parentWidth.value = remaining
    }
}

onMounted(() => {
    measure()
    if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(measure)
        if (breadcrumb.value) observer.observe(breadcrumb.value)
        if (measurements.value) observer.observe(measurements.value)
        observer.observe(document.body)
    }
    window.addEventListener("scroll", publishReadableTop, {passive: true})
    window.addEventListener("resize", measure)
    window.addEventListener("orientationchange", measure)
})
watch(() => [props.title, props.crumbs, settings.showTitleBar], () => nextTick(() => {
    if (breadcrumb.value) observer?.observe(breadcrumb.value)
    if (measurements.value) observer?.observe(measurements.value)
    measure()
}), {deep: true})
onBeforeUnmount(() => {
    observer?.disconnect()
    window.removeEventListener("scroll", publishReadableTop)
    window.removeEventListener("resize", measure)
    window.removeEventListener("orientationchange", measure)
})
</script>

<template>
    <header v-if="settings.showTitleBar" ref="bar" class="title-bar">
        <nav ref="breadcrumb" aria-label="Breadcrumb" class="breadcrumbs">
            <template v-for="(crumb, index) in visibleCrumbs" :key="index">
                <router-link :to="crumb.to" :title="crumb.label" :aria-label="crumb.label" :class="{collapsed}" :style="parentWidth === null ? undefined : {maxWidth: `${parentWidth}px`}"><span>{{ collapsed ? '‹' : crumb.label }}</span></router-link><span v-if="!collapsed" class="separator" aria-hidden="true">›</span>
            </template>
            <h1 :title="session ? `Agent: ${session.agent}\nModels: ${session.models.join(', ')}\nSession ID: ${session.id}` : title">{{ title }}</h1>
        </nav>
        <div ref="measurements" class="measurements" aria-hidden="true" inert>
            <span v-for="(crumb, index) in crumbs" :key="index" class="crumb-label">{{ crumb.label }}</span><span class="separator-measure">›</span><span class="minimum-parent">0000000000</span><span class="title-measure">{{ title }}</span><span class="title-measure minimum-title">000</span>
        </div>
        <slot />
        <QuickSettings class="title-menu" :context="context" :session="session" />
    </header>
    <QuickSettings v-else :context="context" :session="session" :parent="crumbs?.at(-1)" />
</template>

<style scoped>
.title-bar { position: sticky; top: var(--app-banner-height, 0px); z-index: 12; display: flex; align-items: center; box-sizing: border-box; height: var(--title-bar-height); background: var(--bg); border-bottom: 1px solid var(--border); min-width: 0; }
.breadcrumbs { display: flex; align-items: center; flex: 1; min-width: 0; overflow: hidden; }
.breadcrumbs a { display: flex; flex: none; align-items: center; height: var(--title-bar-height); min-width: 0; overflow: hidden; white-space: nowrap; color: var(--muted); font-size: 13px; text-decoration: none; }
.breadcrumbs a span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.breadcrumbs a.collapsed { justify-content: center; width: 44px; font-size: 24px; }
.separator, .separator-measure { flex: none; padding: 0 6px; color: var(--muted); font-size: 13px; }
h1, .title-measure { font-size: 15px; line-height: 1.2; font-weight: 600; margin: 0; min-width: 3ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.measurements { position: absolute; visibility: hidden; pointer-events: none; display: flex; width: max-content; max-width: none; height: 0; overflow: hidden; font-size: 13px; }
.measurements > * { flex: none; }
</style>
