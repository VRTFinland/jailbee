<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import Button from "../components/ui/Button.vue"
import {computed, ref} from "vue"
import {useRouter} from "vue-router"
import {logout} from "../api"
import RepoList from "../components/RepoList.vue"
import PageNav from "../components/PageNav.vue"
import {usePager} from "../composables/usePager"
import {useSlowView} from "../composables/useSlowView"
import {useDashboard} from "../dashboard"
import {settings} from "../settings"

const props = withDefaults(defineProps<{refreshMs?: number}>(), {refreshMs: 60_000})

const router = useRouter()
const dashboard = useDashboard()
const readableTop = ref(0)
const pager = usePager(() => readableTop.value)
const eink = computed(() => settings.displayProfile === "eink")
// E-ink: the list is a displayed copy refreshed slowly or on demand.
const {shown: view, refresh} = useSlowView(
    () => dashboard?.state.view ?? null,
    () => eink.value,
    () => props.refreshMs,
)
const status = computed(() => dashboard?.state.status ?? null)
const connected = computed(() => dashboard?.state.connected ?? false)
const device = computed(() => dashboard?.state.device ?? "")

async function signOut() {
    dashboard?.stop()
    await logout()
    await router.push({name: "login"})
}
</script>

<template>
    <div class="page">
        <TitleBar @readable-top="readableTop = $event" title="JailBee" context="containers" />
        <div class="bar">
            <span class="device" v-if="device">{{ device }}</span>
            <Button v-if="eink" class="refresh" variant="text" @click="refresh">Refresh</Button>
            <Button variant="text" @click="signOut">Log out</Button>
        </div>

        <p v-if="!connected" class="notice">Connecting…</p>
        <p v-else-if="status" class="notice">{{ status }}</p>

        <main>
            <p v-if="view === null" class="muted">Waiting for the first update…</p>
            <RepoList v-else :view="view" />
        </main>
        <PageNav :pager="pager" />
    </div>
</template>

<style scoped>
.page {
    max-width: 48rem;
    margin: 0 auto;
    padding: var(--md-space-16);
}
.bar {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-16);
    padding-bottom: var(--md-space-8);
    border-bottom: 1px solid var(--md-outline-variant);
}
.device {
    color: var(--md-on-surface-variant);
}
.notice {
    margin: var(--md-space-16) 0;
    padding: var(--md-space-8) var(--md-space-16);
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    border-radius: var(--md-shape-medium);
}
.muted {
    color: var(--md-on-surface-variant);
}
</style>
