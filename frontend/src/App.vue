<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {onBeforeUnmount, onMounted, ref, watch} from "vue"
import {fetchServerMode} from "./api"
import {useRoute, useRouter} from "vue-router"
import {useDashboard} from "./dashboard"

const route = useRoute()
const router = useRouter()
const dashboard = useDashboard()

dashboard?.onUnauthorized(() => {
    void router.push({name: "login"})
})
dashboard?.onSetupMode(() => {
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
// setup server too. Only there, so a plain server restart under a signed-in
// page adds no failed requests beyond the socket's own retries.
let checkingMode = false
let modeTimer: ReturnType<typeof setInterval> | undefined
async function checkMode() {
    if (checkingMode || document.hidden || route.name === "setup") return
    checkingMode = true
    try {
        if (await fetchServerMode() === "setup" && route.name !== "setup") {
            await router.replace({name: "setup"})
        }
    } finally {
        checkingMode = false
    }
}
onMounted(() => {
    modeTimer = setInterval(() => {
        if (route.name === "login") void checkMode()
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
