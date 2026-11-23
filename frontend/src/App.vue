<script setup lang="ts">
// Holds the dashboard socket open on every signed-in page (so a deep link or
// reload of any view is live), and closed on public pages such as login.
import {onBeforeUnmount, watch} from "vue"
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

onBeforeUnmount(() => dashboard?.stop())
</script>

<template>
    <!-- Route-specific documents must reload and release their live follow on navigation. -->
    <router-view :key="route.path" />
</template>
