<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {nextTick, onBeforeUnmount, onMounted, provide, ref, watch} from "vue"
import SettingsPanel from "./components/SettingsPanel.vue"
import {fetchServerMode, getToken} from "./api"
import {useRoute, useRouter} from "vue-router"
import {useDashboard} from "./dashboard"
import {rememberResumePath, takeResumePath} from "./router"

const route = useRoute()
const router = useRouter()
const dashboard = useDashboard()
const settingsOpen = ref(false)
const settingsDialog = ref<HTMLElement | null>(null)
let settingsTrigger: HTMLElement | null = null

function showSettings(trigger: HTMLElement | null) {
    if (settingsOpen.value) return
    settingsTrigger = trigger
    history.pushState({...history.state, jailbeeSettings: true}, "", location.href)
    settingsOpen.value = true
    void nextTick(() => settingsDialog.value?.focus({preventScroll: true}))
}
provide("open-settings", showSettings)
function dismissSettings() {
    if (settingsOpen.value) history.back()
}
function onPopState() {
    if (settingsOpen.value && !history.state?.jailbeeSettings) {
        settingsOpen.value = false
        void nextTick(() => settingsTrigger?.focus({preventScroll: true}))
    }
}
function settingsKey(event: KeyboardEvent) {
    if (!settingsOpen.value) return
    event.stopImmediatePropagation()
    if (event.key === "Escape") {
        event.preventDefault()
        event.stopImmediatePropagation()
        dismissSettings()
    } else if (event.key === "Tab") {
        const controls = Array.from(settingsDialog.value?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]") ?? [])
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && (document.activeElement === first || document.activeElement === settingsDialog.value)) {
            event.preventDefault()
            last?.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault()
            first?.focus()
        }
    }
}
watch(() => route.fullPath, () => {
    if (settingsOpen.value) {
        settingsOpen.value = false
        settingsTrigger = null
    }
})


dashboard?.onUnauthorized(() => {
    takeResumePath()
    void router.push({name: "login"})
})
dashboard?.onSetupMode(() => {
    rememberResumePath(route)
    void router.replace({name: "setup"})
})

watch(
    () => (route.matched.length === 0 ? null : route.meta.public === true),
    (isPublic) => {
        if (isPublic === null || dashboard === null) return
        if (isPublic) dashboard.stop()
        else dashboard.start()
    },
    {immediate: true},
)

// Views with their own sticky header stick below the banner via this variable.
const banner = ref<HTMLElement | null>(null)
let bannerObserver: ResizeObserver | null = null
function publishBannerHeight() {
    const root = document.documentElement.style
    if (banner.value === null) root.removeProperty("--app-banner-height")
    else root.setProperty("--app-banner-height", `${banner.value.getBoundingClientRect().height}px`)
}
watch(banner, (el) => {
    bannerObserver?.disconnect()
    bannerObserver = null
    publishBannerHeight()
    if (el !== null && typeof ResizeObserver !== "undefined") {
        bannerObserver = new ResizeObserver(publishBannerHeight)
        bannerObserver.observe(el)
    }
}, {flush: "post"})

function reload() {
    location.reload()
}

// A signed-in page learns of setup mode from its socket (the setup server
// closes /api/ws with CLOSE_SETUP_MODE). The login page has no socket, so it
// polls while visible: an already-open login tab or PWA follows the temporary
// setup server too. The setup view polls as well, to leave once setup ends.
// checkMode runs only on those two pages (timer, `online` and visibility
// alike), so a plain server restart under a signed-in page adds no failed
// requests beyond the socket's own retries.
let checkingMode = false
let modeTimer: ReturnType<typeof setInterval> | undefined
async function checkMode() {
    if (route.name !== "login" && route.name !== "setup") return
    if (checkingMode || document.hidden) return
    checkingMode = true
    try {
        const mode = await fetchServerMode()
        if (mode === "setup" && route.name !== "setup") {
            await router.replace({name: "setup"})
        } else if (mode === "dashboard" && route.name === "setup") {
            // A stored token is still valid unless the socket says otherwise
            // (4001); sessions outlive a server restart.
            if (getToken()) await router.replace(takeResumePath())
            else {
                takeResumePath()
                await router.replace({name: "login"})
            }
        }
    } finally {
        checkingMode = false
    }
}
onMounted(() => {
    window.addEventListener("popstate", onPopState)
    window.addEventListener("keydown", settingsKey, true)
    modeTimer = setInterval(() => {
        void checkMode()
    }, 2_000)
    window.addEventListener("online", checkMode)
    document.addEventListener("visibilitychange", checkMode)
})
onBeforeUnmount(() => {
    window.removeEventListener("popstate", onPopState)
    window.removeEventListener("keydown", settingsKey, true)
    clearInterval(modeTimer)
    window.removeEventListener("online", checkMode)
    document.removeEventListener("visibilitychange", checkMode)
    dashboard?.stop()
    bannerObserver?.disconnect()
    document.documentElement.style.removeProperty("--app-banner-height")
})
</script>

<template>
    <aside v-if="dashboard?.state.outdated" ref="banner" class="outdated">
        <p role="alert">This page is out of date; reload.</p>
        <button type="button" @click="reload">Reload</button>
    </aside>
    <!-- Route-specific documents must reload and release their live follow on navigation. -->
    <div :inert="settingsOpen || undefined"><router-view :key="route.path" /></div>
    <div v-if="settingsOpen" class="settings-backdrop" @click.self="dismissSettings">
        <section ref="settingsDialog" class="settings-dialog" role="dialog" aria-label="Settings" aria-modal="true" tabindex="-1">
            <header class="settings-header"><h2>Settings</h2><button type="button" aria-label="Close settings" @click="dismissSettings">&times;</button></header>
            <SettingsPanel />
        </section>
    </div>
</template>

<style scoped>
.settings-backdrop { position: fixed; inset: 0; z-index: 100; background: transparent; }
.settings-dialog { position: absolute; right: 0; top: 0; bottom: 0; width: min(36rem, 100%); overflow-y: auto; background: var(--surface); color: var(--text); border-left: 1px solid var(--border-strong); }
.settings-header { display: flex; align-items: center; justify-content: space-between; padding: var(--space-3); border-bottom: 1px solid var(--border); }
.settings-header h2 { margin: 0; font-size: 1.25rem; }
.settings-header button { min-width: 44px; min-height: 44px; font: inherit; }
:root[data-profile="eink"] .settings-backdrop { background: transparent; }
:root[data-profile="eink"] .settings-dialog { box-shadow: none; border: 1px solid var(--border-strong); }
@media (max-width: 600px) { .settings-dialog { top: auto; width: 100%; height: 92%; border: 1px solid var(--border-strong); } }
.outdated {
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-3);
    background: var(--notice-bg);
    border-bottom: 2px solid var(--border-strong);
}
.outdated p { margin: 0; }
.outdated button { font: inherit; min-height: 2.75rem; }
</style>
