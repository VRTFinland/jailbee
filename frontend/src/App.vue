<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {onBeforeUnmount, onMounted, watch} from "vue"
import {fetchServerMode} from "./api"
import {useRoute, useRouter} from "vue-router"
import {useDashboard} from "./dashboard"

const route = useRoute()
const router = useRouter()
const dashboard = useDashboard()

dashboard?.onUnauthorized(() => {
    void router.push({name: "login"})
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

// Login has no socket to notice a server replacement. Poll while visible so
// an already-open tab (including a PWA) follows the temporary setup server too.
let checkingMode = false
let modeTimer: ReturnType<typeof setInterval> | undefined
async function checkMode() {
    if (checkingMode || document.hidden) return
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
    modeTimer = setInterval(() => void checkMode(), 2_000)
    window.addEventListener("online", checkMode)
    document.addEventListener("visibilitychange", checkMode)
})
onBeforeUnmount(() => {
    clearInterval(modeTimer)
    window.removeEventListener("online", checkMode)
    document.removeEventListener("visibilitychange", checkMode)
    dashboard?.stop()
})
</script>

<template>
    <!-- Route-specific documents must reload and release their live follow on navigation. -->
    <router-view :key="route.path" />
</template>
