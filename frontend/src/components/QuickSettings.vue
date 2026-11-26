<script setup lang="ts">
import {computed, inject, nextTick, onBeforeUnmount, onMounted, ref, useId, watch} from "vue"
import {useRoute, type RouteLocationRaw} from "vue-router"
import {FONT_SIZE_RANGE, settings, THEMES, THEME_LABELS, updateSettings} from "../settings"
import IconButton from "./ui/IconButton.vue"
import Button from "./ui/Button.vue"
import Dialog from "./ui/Dialog.vue"
import FieldFrame from "./ui/FieldFrame.vue"
import Switch from "./ui/Switch.vue"

const props = defineProps<{context?: "containers" | "session" | null; session?: {agent: string; models: string[]; id: string} | null; parent?: {label: string; to: RouteLocationRaw}}>()
const route = useRoute()
const openSettings = inject<(trigger: HTMLElement | null) => void>("open-settings", () => {})
const openJump = inject<(origin: HTMLElement | null) => void>("open-jump", () => {})
function showJump() {
    void close(false)
    openJump(trigger.value)
}
function showSettings() {
    void close(false)
    openSettings(trigger.value)
}
const fonts = [{field: "bodyFontSize", label: "Body", name: "body"}, {field: "codeFontSize", label: "Code", name: "code"}] as const
const filters = [{mode: "all", label: "All"}, {mode: "running", label: "Running"}, {mode: "active", label: "Active"}] as const
const filtered = computed(() => props.context === "containers" && settings.containerFilter !== "all")
const follow = computed(() => settings.displayProfile === "eink" ? settings.einkFollow : settings.followNewMessages)
function setFollow(enabled: boolean) {
    updateSettings(settings.displayProfile === "eink" ? {einkFollow: enabled} : {followNewMessages: enabled})
}
function cycleTheme() {
    updateSettings({theme: THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length]})
}
const page = ref(0)
const pageCount = ref(1)
let pages: HTMLElement[][] = []

// Measure indivisible rows, not a fixed item count: font size and phone height
// both change how many touch targets fit. No popover scroll is needed.
function paginate() {
    const element = panel()
    if (!element) return
    const rows = Array.from(element.querySelectorAll<HTMLElement>(".menu-row"))
    rows.forEach(row => { row.hidden = false })
    const controls = element.querySelector<HTMLElement>(".menu-pages")
    if (controls) controls.hidden = false
    const styles = getComputedStyle(element)
    const inset = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom)
        + parseFloat(styles.borderTopWidth) + parseFloat(styles.borderBottomWidth)
    const guard = document.querySelector(".bottom-guard")?.getBoundingClientRect().height ?? 32
    const available = Math.max(1, window.innerHeight - element.getBoundingClientRect().top
        - guard - inset - (controls?.getBoundingClientRect().height ?? 0))
    pages = [[]]
    let height = 0
    for (const row of rows) {
        const rowHeight = row.getBoundingClientRect().height
        if (height + rowHeight > available && pages[pages.length - 1].length) {
            pages.push([])
            height = 0
        }
        pages[pages.length - 1].push(row)
        height += rowHeight
    }
    pageCount.value = pages.length
    page.value = Math.min(page.value, pages.length - 1)
    if (controls) controls.hidden = pageCount.value <= 1
    showPage()
}
function showPage() {
    pages.forEach((rows, index) => rows.forEach(row => { row.hidden = index !== page.value }))
}
function turn(delta: number) {
    page.value = Math.max(0, Math.min(pageCount.value - 1, page.value + delta))
    showPage()
    panel()?.focus({preventScroll: true})
}
watch(() => [props.context, props.session, props.parent, settings.bodyFontSize, settings.codeFontSize, settings.displayProfile], () => {
    page.value = 0
    void nextTick(paginate)
})
const open = ref(false)
const triggerButton = ref<InstanceType<typeof IconButton> | null>(null)
const trigger = computed<HTMLButtonElement | null>(() => triggerButton.value?.$el ?? null)
const panelId = `quick-settings-${useId()}`
function panel() { return document.getElementById(panelId) }
let restoreOnClose = true
function returnFocus(event: Event) {
    event.preventDefault()
    if (restoreOnClose) trigger.value?.focus({preventScroll: true})
}
watch(open, async value => {
    if (!value) return
    restoreOnClose = true
    page.value = 0
    await nextTick()
    paginate()
})

async function close(restoreFocus = true) {
    restoreOnClose = restoreFocus
    open.value = false
    if (restoreFocus) {
        await nextTick()
        trigger.value?.focus({preventScroll: true})
    }
}
watch(() => route.path, () => { if (open.value) void close(false) })
function onKey(event: KeyboardEvent) {
    if (!open.value) return
    if (["PageDown", "PageUp"].includes(event.key) || (settings.displayProfile === "eink" && ["n", "m"].includes(event.key) && !(event.target instanceof HTMLSelectElement))) {
        event.preventDefault()
        event.stopImmediatePropagation()
        turn(["PageDown", "m"].includes(event.key) ? 1 : -1)
    }

}
onMounted(() => {
    window.addEventListener("keydown", onKey, true)
    window.addEventListener("resize", paginate)
})
onBeforeUnmount(() => {
    window.removeEventListener("keydown", onKey, true)
    window.removeEventListener("resize", paginate)
})
</script>

<template>
    <div class="quick-settings">
        <Dialog v-model:open="open" title="Quick settings" placement="popover" preserve-scroll hide-header
            :id="panelId" class="quick-panel" aria-label="Quick settings" @close-auto-focus="returnFocus" @positioned="paginate">
        <template #trigger><IconButton ref="triggerButton" icon="menu" label="Quick settings" class="quick-trigger"
            :variant="settings.showTitleBar ? 'text' : 'tonal'" aria-haspopup="dialog" :aria-expanded="open" :aria-describedby="filtered ? 'filter-description' : undefined" /></template>

            <router-link v-if="!settings.showTitleBar && parent" class="menu-row" :to="parent.to" @click="close(false)">Back to {{ parent.label }}</router-link>
            <div v-if="session" class="menu-row session-info" aria-label="Session">
                <strong>Session</strong><span>Agent: {{ session.agent }}</span><span>Models: {{ session.models.join(", ") || "—" }}</span><span>Session ID: {{ session.id }}</span>
            </div>
            <div v-for="font in fonts" :key="font.field" class="menu-row font-row">
                <span>{{ font.label }}: {{ settings[font.field] }}px</span>
                <IconButton :label="`Decrease ${font.name} font size`"
                    :disabled="settings[font.field] <= FONT_SIZE_RANGE.min"
                    @click="updateSettings({[font.field]: settings[font.field] - 1})" icon="remove" />
                <IconButton :label="`Increase ${font.name} font size`"
                    :disabled="settings[font.field] >= FONT_SIZE_RANGE.max"
                    @click="updateSettings({[font.field]: settings[font.field] + 1})" icon="add" />
            </div>
            <Button type="button" class="menu-row theme-cycle" :aria-label="`Theme: ${THEME_LABELS[settings.theme]}`" @click="cycleTheme">Theme: {{ THEME_LABELS[settings.theme] }}</Button>
            <template v-if="context">
                <div class="menu-row view-heading">View</div>
                <div v-if="context === 'containers'" role="group" aria-label="Container filter">
                    <Button v-for="filter in filters" :key="filter.mode" type="button" class="menu-row" :variant="settings.containerFilter === filter.mode ? 'tonal' : 'text'"
                        :aria-label="filter.label" :title="filter.label" :aria-pressed="settings.containerFilter === filter.mode"
                        @click="updateSettings({containerFilter: filter.mode})">{{ filter.label }}</Button>
                </div>
                <template v-else>
                    <Button type="button" class="menu-row" data-action="jump" @click="showJump">Jump to…</Button>
                    <div class="menu-row toggle-row"><span>Follow new messages</span><Switch label="Follow new messages" :model-value="follow" @update:model-value="setFollow" /></div>
                    <div class="menu-row toggle-row"><span>Show thinking</span><Switch label="Show thinking" :model-value="settings.showThinking" @update:model-value="updateSettings({showThinking: $event})" /></div>
                    <FieldFrame class="menu-row context-row" label="Agent context" v-slot="{controlAttrs}">
                        <select v-bind="controlAttrs({})" class="ui-input ui-focus-ring" aria-label="Agent context" :value="settings.agentContext" @change="updateSettings({agentContext: ($event.target as HTMLSelectElement).value as 'hidden' | 'collapsed' | 'shown'})">
                            <option value="hidden">Hidden</option><option value="collapsed">Collapsed</option><option value="shown">Shown</option>
                        </select>
                    </FieldFrame>
                </template>
            </template>
            <Button type="button" class="menu-row settings-action" @click="showSettings">Settings</Button>
            <nav class="menu-pages" hidden aria-label="Settings pages">
                <IconButton label="Previous settings page" :disabled="page === 0" @click="turn(-1)" icon="page-up" />
                <span role="status">{{ page + 1 }} / {{ pageCount }}</span>
                <IconButton label="Next settings page" :disabled="page === pageCount - 1" @click="turn(1)" icon="page-down" />
            </nav>
        </Dialog>
        <span v-if="filtered" id="filter-description" class="filter-marker"><span class="visually-hidden">Container filter enabled</span></span>

    </div>
</template>

<style scoped>
.quick-settings { position: relative; flex: none; z-index: var(--md-layer-sticky); margin-left: auto; }
.quick-settings:not(.title-menu) { position: fixed; top: calc(var(--app-banner-height, 0) + var(--quick-trigger-top)); right: var(--md-space-12); }
.session-info { flex-direction: column; align-items: flex-start; overflow-wrap: anywhere; user-select: text; }
.menu-row { display: flex; align-items: center; gap: var(--md-space-8); width: 100%; margin: 0; }
.menu-row[hidden], .menu-pages[hidden] { display: none; }
.font-row { flex-wrap: wrap; }
.font-row > span { flex: 1 1 5rem; }
.font-row button { flex-shrink: 0; }
.toggle-row { justify-content: space-between; }
.context-row { flex-wrap: wrap; }
.view-heading { padding-block: var(--md-space-8); }
.menu-pages { display: flex; align-items: center; justify-content: space-between; padding-top: var(--md-space-8); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.filter-marker { pointer-events: none; position: absolute; top: var(--md-space-4); right: var(--md-space-4); width: var(--md-space-8); height: var(--md-space-8); border-radius: var(--md-shape-full); background: var(--md-primary); border: 1px solid var(--md-on-surface); }
</style>
