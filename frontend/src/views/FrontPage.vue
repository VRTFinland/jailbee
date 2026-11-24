<script setup lang="ts">
import {computed} from "vue"
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
const pager = usePager()
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
        <header class="bar">
            <h1>JailBee</h1>
            <span class="device" v-if="device">{{ device }}</span>
            <button v-if="eink" class="refresh link" type="button" @click="refresh">
                Refresh
            </button>
            <button class="link" type="button" @click="signOut">Log out</button>
        </header>

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
    padding: var(--space-3);
}
.bar {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-3);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border);
}
.bar h1 {
    font-size: 1.25rem;
    margin: 0;
    flex: 1;
}
.device {
    color: var(--muted);
}
.notice {
    margin: var(--space-3) 0;
    padding: var(--space-2) var(--space-3);
    background: var(--notice-bg);
    border-radius: var(--radius);
}
.muted {
    color: var(--muted);
}
</style>
