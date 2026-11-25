<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue"
import {useRoute} from "vue-router"
import {FONT_SIZE_RANGE, settings, THEMES, THEME_LABELS, updateSettings} from "../settings"

const props = defineProps<{context?: "containers" | "session" | null; session?: {agent: string; models: string[]; id: string} | null}>()
const route = useRoute()
const fonts = [{field: "bodyFontSize", label: "Body", name: "body"}, {field: "codeFontSize", label: "Code", name: "code"}] as const
const filters = [{mode: "all", label: "All"}, {mode: "running", label: "Running"}, {mode: "active", label: "Active"}] as const
const filtered = computed(() => props.context === "containers" && settings.containerFilter !== "all")
const follow = computed(() => settings.displayProfile === "eink" ? settings.einkFollow : settings.followNewMessages)
function setFollow(event: Event) {
    const enabled = (event.target as HTMLInputElement).checked
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
    if (!panel.value) return
    const rows = Array.from(panel.value.querySelectorAll<HTMLElement>(".menu-row"))
    rows.forEach(row => { row.hidden = false })
    const controls = panel.value.querySelector<HTMLElement>(".menu-pages")
    if (controls) controls.hidden = false
    const styles = getComputedStyle(panel.value)
    const inset = parseFloat(styles.paddingTop) + parseFloat(styles.paddingBottom)
        + parseFloat(styles.borderTopWidth) + parseFloat(styles.borderBottomWidth)
    const guard = document.querySelector(".bottom-guard")?.getBoundingClientRect().height ?? 32
    const available = Math.max(1, window.innerHeight - panel.value.getBoundingClientRect().top
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
    panel.value?.focus({preventScroll: true})
}
watch(() => [props.context, settings.bodyFontSize, settings.codeFontSize, settings.displayProfile], () => {
    page.value = 0
    void nextTick(paginate)
})
const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
const panel = ref<HTMLElement | null>(null)

async function close(restoreFocus = true) {
    open.value = false
    if (restoreFocus) {
        await nextTick()
        trigger.value?.focus({preventScroll: true})
    }
}
watch(() => route.path, () => { if (open.value) void close(false) })
async function toggle() {
    if (open.value) return close()
    open.value = true
    page.value = 0
    await nextTick()
    paginate()
    focusable()[0]?.focus({preventScroll: true})
}
function focusable(): HTMLElement[] {
    return Array.from(panel.value?.querySelectorAll<HTMLElement>("button:not(:disabled), a, input, select") ?? [])
        .filter(el => !el.closest("[hidden]"))
}
function onEscape(event: KeyboardEvent) {
    if (open.value && event.key === "Escape") {
        event.preventDefault()
        void close()
    }
}
function onKey(event: KeyboardEvent) {
    if (!open.value) return
    if (["PageDown", "PageUp"].includes(event.key) || (settings.displayProfile === "eink" && ["n", "m"].includes(event.key) && !(event.target instanceof HTMLSelectElement))) {
        event.preventDefault()
        event.stopImmediatePropagation()
        turn(["PageDown", "m"].includes(event.key) ? 1 : -1)
    }
    if (event.key === "Tab") {
        const controls = focusable()
        const first = controls[0]
        const last = controls[controls.length - 1]
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
            event.preventDefault()
            last?.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault()
            first?.focus()
        }
    }
}
onMounted(() => {
    window.addEventListener("keydown", onKey, true)
    window.addEventListener("keydown", onEscape)
    window.addEventListener("resize", paginate)
})
onBeforeUnmount(() => {
    window.removeEventListener("keydown", onKey, true)
    window.removeEventListener("keydown", onEscape)
    window.removeEventListener("resize", paginate)
})
</script>

<template>
    <div class="quick-settings">
        <button ref="trigger" type="button" class="quick-trigger" aria-label="Quick settings"
            aria-haspopup="dialog" :aria-expanded="open" :aria-describedby="filtered ? 'filter-description' : undefined" @click="toggle">
            <span aria-hidden="true">&#8942;</span>
            <span v-if="filtered" id="filter-description" class="filter-marker"><span class="visually-hidden">Container filter enabled</span></span>
        </button>
        <div v-if="open" class="quick-backdrop" @click="close()" />
        <section v-if="open" ref="panel" class="quick-panel" role="dialog" aria-label="Quick settings" aria-modal="true" tabindex="-1">
            <div v-if="session" class="menu-row session-info" aria-label="Session">
                <strong>Session</strong><span>Agent: {{ session.agent }}</span><span>Models: {{ session.models.join(", ") || "—" }}</span><span>Session ID: {{ session.id }}</span>
            </div>
            <div v-for="font in fonts" :key="font.field" class="menu-row font-row">
                <span>{{ font.label }}: {{ settings[font.field] }}px</span>
                <button type="button" :aria-label="`Decrease ${font.name} font size`"
                    :disabled="settings[font.field] <= FONT_SIZE_RANGE.min"
                    @click="updateSettings({[font.field]: settings[font.field] - 1})">&minus;</button>
                <button type="button" :aria-label="`Increase ${font.name} font size`"
                    :disabled="settings[font.field] >= FONT_SIZE_RANGE.max"
                    @click="updateSettings({[font.field]: settings[font.field] + 1})">+</button>
            </div>
            <button type="button" class="menu-row theme-cycle" :aria-label="`Theme: ${THEME_LABELS[settings.theme]}`" @click="cycleTheme">Theme: {{ THEME_LABELS[settings.theme] }}</button>
            <template v-if="context">
                <div class="menu-row view-heading">View</div>
                <div v-if="context === 'containers'" role="group" aria-label="Container filter">
                    <button v-for="filter in filters" :key="filter.mode" type="button" class="menu-row"
                        :aria-label="filter.label" :title="filter.label" :aria-pressed="settings.containerFilter === filter.mode"
                        @click="updateSettings({containerFilter: filter.mode})">{{ filter.label }}</button>
                </div>
                <template v-else>
                    <label class="menu-row"><input type="checkbox" aria-label="Follow new messages" :checked="follow" @change="setFollow" /> Follow new messages</label>
                    <label class="menu-row"><input type="checkbox" aria-label="Show thinking" :checked="settings.showThinking" @change="updateSettings({showThinking: ($event.target as HTMLInputElement).checked})" /> Show thinking</label>
                    <label class="menu-row context-row">Agent context
                        <select aria-label="Agent context" :value="settings.agentContext" @change="updateSettings({agentContext: ($event.target as HTMLSelectElement).value as 'hidden' | 'collapsed' | 'shown'})">
                            <option value="hidden">Hidden</option><option value="collapsed">Collapsed</option><option value="shown">Shown</option>
                        </select>
                    </label>
                </template>
            </template>
            <router-link class="menu-row" :to="{name: 'settings'}" @click="close(false)">Settings</router-link>
            <nav class="menu-pages" hidden aria-label="Settings pages">
                <button type="button" aria-label="Previous settings page" :disabled="page === 0" @click="turn(-1)">&uarr;</button>
                <span role="status">{{ page + 1 }} / {{ pageCount }}</span>
                <button type="button" aria-label="Next settings page" :disabled="page === pageCount - 1" @click="turn(1)">&darr;</button>
            </nav>
        </section>
    </div>
</template>

<style scoped>
.quick-settings { position: relative; flex: none; z-index: 40; margin-left: auto; }
.session-info { flex-direction: column; align-items: flex-start; overflow-wrap: anywhere; user-select: text; }
.quick-trigger span { font-size: 24px; line-height: 1; }
.quick-settings:not(.title-menu) { position: fixed; top: calc(var(--app-banner-height, 0px) + var(--quick-trigger-top)); right: 0.75rem; }
.quick-trigger { position: relative; display: grid; place-items: center; width: var(--quick-trigger-size); height: var(--quick-trigger-size); color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.quick-trigger svg { width: 1.25rem; fill: none; stroke: currentColor; stroke-width: 2; }
.quick-backdrop { position: fixed; inset: 0; background: rgb(0 0 0 / 20%); z-index: -1; }
.quick-panel { position: absolute; top: calc(var(--quick-trigger-size) + 0.25rem); right: 0; width: min(22rem, calc(100vw - 2rem)); padding: 0.5rem; border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--surface); color: var(--text); box-shadow: 0 4px 16px rgb(0 0 0 / 20%); }
.menu-row { display: flex; align-items: center; gap: 0.5rem; box-sizing: border-box; width: 100%; min-height: 2.75rem; padding: 0.25rem 0.5rem; margin: 0; color: inherit; text-decoration: none; font: inherit; border: 0; background: transparent; text-align: left; }
.menu-row[hidden], .menu-pages[hidden] { display: none; }
.menu-row[aria-pressed="true"] { background: var(--notice-bg); outline: 2px solid var(--accent); outline-offset: -2px; }
.font-row { flex-wrap: wrap; }
.font-row > span { flex: 1 1 5rem; }
.font-row button { flex-shrink: 0; }
.quick-panel button, .quick-panel select { font: inherit; color: inherit; min-height: 2.75rem; }
.font-row button, .menu-pages button { min-width: 2.75rem; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
.context-row { justify-content: space-between; flex-wrap: wrap; }
.view-heading { color: var(--muted); border-top: 1px solid var(--border); }
.menu-pages { display: flex; align-items: center; justify-content: space-between; padding-top: 0.5rem; }
.visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.filter-marker { position: absolute; top: 0.2rem; right: 0.2rem; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: var(--accent); border: 1px solid var(--text); }
:root[data-profile="eink"] .quick-panel, :root[data-theme="eink"] .quick-panel { box-shadow: none; }
:root[data-profile="eink"] .quick-backdrop { background: transparent; }
/* Keep a full touch-target row plus paging controls reachable in landscape,
   even when the document's reading font is set to its maximum. */
@media (max-height: 480px) {
    .quick-panel { font-size: min(1rem, 16px); padding: 8px; }
    .menu-row { min-height: 44px; padding: 4px 8px; gap: 8px; }
    .quick-panel button, .quick-panel select { min-height: 44px; }
    .font-row button, .menu-pages button { min-width: 44px; }
    .font-row > span { flex-basis: 80px; }
    .menu-pages { padding-top: 8px; }
}
</style>
