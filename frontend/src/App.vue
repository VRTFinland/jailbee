<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue"
import QuickSettings from "./components/QuickSettings.vue"
import {fetchServerMode, getToken} from "./api"
import {useRoute, useRouter} from "vue-router"
import {useDashboard} from "./dashboard"
import {rememberResumePath, takeResumePath} from "./router"

const route = useRoute()
const router = useRouter()
const dashboard = useDashboard()
const settingsContext = computed(() => {
    if (route.name === "front" || route.name === "repo") return "containers"
    if (route.name === "session" || route.name === "subagent") return "session"
    return null
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
    modeTimer = setInterval(() => {
        void checkMode()
    }, 2_000)
    window.addEventListener("online", checkMode)
    document.addEventListener("visibilitychange", checkMode)
})
onBeforeUnmount(() => {
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
    <QuickSettings :key="route.path" :context="settingsContext" />
    <!-- Route-specific documents must reload and release their live follow on navigation. -->
    <router-view :key="route.path" />
</template>

<style scoped>
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
