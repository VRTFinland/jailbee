<script setup lang="ts">
import {computed} from "vue"
import {useRouter} from "vue-router"
import {logout} from "../api"
import RepoList from "../components/RepoList.vue"
import {useDashboard} from "../dashboard"

const router = useRouter()
const dashboard = useDashboard()
const view = computed(() => dashboard?.state.view ?? null)
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
            <router-link class="link" :to="{name: 'settings'}">Settings</router-link>
            <button class="link" type="button" @click="signOut">Log out</button>
        </header>

        <p v-if="!connected" class="notice">Connecting…</p>
        <p v-else-if="status" class="notice">{{ status }}</p>

        <main>
            <p v-if="view === null" class="muted">Waiting for the first update…</p>
            <RepoList v-else :view="view" />
        </main>
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
