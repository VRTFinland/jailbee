<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {nextTick, onBeforeUnmount, onMounted, provide, ref, watch} from "vue"
import SettingsPanel from "./components/SettingsPanel.vue"
import Button from "./components/ui/Button.vue"
import Dialog from "./components/ui/Dialog.vue"
import {fetchServerMode, getToken} from "./api"
import {useRoute, useRouter} from "vue-router"
import {useDashboard} from "./dashboard"
import {rememberResumePath, takeResumePath} from "./router"

const route = useRoute()
const router = useRouter()
const dashboard = useDashboard()
const settingsOpen = ref(false)
let settingsTrigger: HTMLElement | null = null
let scrollRestoration: ScrollRestoration | null = null
let restorationFrame: number | undefined
function keepReaderScroll() {
    if (restorationFrame !== undefined) cancelAnimationFrame(restorationFrame)
    restorationFrame = undefined
    scrollRestoration ??= history.scrollRestoration
    history.scrollRestoration = "manual"
}
function releaseReaderScroll() {
    restorationFrame = requestAnimationFrame(() => {
        if (scrollRestoration !== null) history.scrollRestoration = scrollRestoration
        scrollRestoration = null
        restorationFrame = undefined
    })
}

function showSettings(trigger: HTMLElement | null) {
    if (settingsOpen.value) return
    settingsTrigger = trigger
    keepReaderScroll()
    if (history.state?.jailbeeSettings !== route.fullPath) {
        history.pushState({...history.state, jailbeeSettings: route.fullPath}, "", location.href)
    }
    settingsOpen.value = true
}
provide("open-settings", showSettings)
function dismissSettings() {
    if (settingsOpen.value) history.back()
}
function onPopState() {
    const open = history.state?.jailbeeSettings === route.fullPath
    if (open === settingsOpen.value) return
    settingsOpen.value = open
    if (open) keepReaderScroll()
    else releaseReaderScroll()
    void nextTick(() => {
        if (!open) {
            const trigger = settingsTrigger?.isConnected ? settingsTrigger
                : document.querySelector<HTMLElement>('[aria-label="Quick settings"][aria-haspopup]')
            trigger?.focus({preventScroll: true})
        }
    })
}
function settingsFocus(event: Event) {
    event.preventDefault()
    const trigger = settingsTrigger?.isConnected ? settingsTrigger
        : document.querySelector<HTMLElement>('[aria-label="Quick settings"][aria-haspopup]')
    trigger?.focus({preventScroll: true})
}
watch(() => route.fullPath, () => {
    if (settingsOpen.value) {
        settingsOpen.value = false
        releaseReaderScroll()
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
    modeTimer = setInterval(() => {
        void checkMode()
    }, 2_000)
    window.addEventListener("online", checkMode)
    document.addEventListener("visibilitychange", checkMode)
})
onBeforeUnmount(() => {
    if (restorationFrame !== undefined) cancelAnimationFrame(restorationFrame)
    if (scrollRestoration !== null) history.scrollRestoration = scrollRestoration
    window.removeEventListener("popstate", onPopState)
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
        <Button variant="tonal" @click="reload">Reload</Button>
    </aside>
    <!-- Route-specific documents must reload and release their live follow on navigation. -->
    <div :inert="settingsOpen || undefined"><router-view :key="route.path" /></div>
    <Dialog :open="settingsOpen" title="Settings" placement="drawer" close-label="Close settings" preserve-scroll
        aria-label="Settings" @update:open="value => { if (!value) dismissSettings() }"
        @close-auto-focus="settingsFocus">
        <SettingsPanel in-drawer />
    </Dialog>
</template>

<style scoped>
.outdated {
    position: sticky;
    top: 0;
    z-index: var(--md-layer-sticky);
    display: flex;
    align-items: center;
    gap: var(--md-space-16);
    padding: var(--md-space-16);
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    border-bottom: 2px solid var(--md-outline);
}
.outdated p { margin: 0; }
</style>
