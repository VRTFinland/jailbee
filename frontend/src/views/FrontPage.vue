<script setup lang="ts">
import {onBeforeUnmount, onMounted, ref} from "vue"
import {useRouter} from "vue-router"
import {logout} from "../api"
import type {FrontPageView, ServerMessage} from "../protocol"
import RepoList from "../components/RepoList.vue"
import {DashboardSocket} from "../socket"

const router = useRouter()
const view = ref<FrontPageView | null>(null)
const status = ref<string | null>(null)
const connected = ref(false)
const device = ref("")

const socket = new DashboardSocket({
    onMessage(message: ServerMessage) {
        if (message.type === "hello") device.value = message.device_name
        else if (message.type === "front_page") view.value = message.view
        else if (message.type === "status") status.value = message.message
    },
    onConnectionChange(value: boolean) {
        connected.value = value
    },
    onUnauthorized() {
        void router.push({name: "login"})
    },
})

onMounted(() => socket.start())
onBeforeUnmount(() => socket.stop())

async function signOut() {
    socket.stop()
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
