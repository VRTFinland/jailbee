<script setup lang="ts">
// PIN and authenticator-code (TOTP) login. Ported from codereader's LoginView;
// the server says which methods are on (`GET /api/auth/methods`).
import {computed, onMounted, ref} from "vue"
import {useRoute, useRouter} from "vue-router"
import {
    type AuthMethod,
    fetchAuthMethods,
    loginWithLink,
    loginWithPin,
    loginWithTotp,
    rememberedDeviceName,
} from "../api"

const LABELS: Record<AuthMethod, string> = {pin: "PIN", totp: "Authenticator code"}

const router = useRouter()
const route = useRoute()
const methods = ref<AuthMethod[]>(["pin"])
const active = ref<AuthMethod>("pin")
const pin = ref("")
const code = ref("")
const device = ref(rememberedDeviceName())
const error = ref("")
const loading = ref(false)

onMounted(async () => {
    const link = route.query.link
    if (typeof link === "string" && link) {
        // Strip the one-time token from the address before anything else.
        await router.replace({name: "login"})
        const result = await loginWithLink(link)
        if (result.ok) {
            await router.replace({name: "front"})
            return
        }
        error.value = result.error
    }
    methods.value = await fetchAuthMethods()
    active.value = methods.value[0]
})

const secret = computed(() => (active.value === "pin" ? pin.value : code.value))
const ready = computed(() => /^\d{6}$/.test(secret.value) && device.value.trim().length > 0)

function choose(method: AuthMethod) {
    active.value = method
    error.value = ""
}

async function submit() {
    if (!ready.value || loading.value) return
    error.value = ""
    loading.value = true
    const name = device.value.trim()
    const result =
        active.value === "pin" ? await loginWithPin(pin.value, name) : await loginWithTotp(code.value, name)
    loading.value = false
    if (result.ok) {
        await router.push({name: "front"})
    } else {
        error.value = result.error
        pin.value = ""
        code.value = ""
    }
}
</script>

<template>
    <main class="login">
        <h1>JailBee</h1>
        <div v-if="methods.length > 1" class="tabs" role="tablist">
            <button
                v-for="m in methods"
                :key="m"
                type="button"
                role="tab"
                :data-method="m"
                :aria-selected="active === m"
                :class="['tab', {active: active === m}]"
                @click="choose(m)"
            >
                {{ LABELS[m] }}
            </button>
        </div>
        <p v-if="active === 'pin'">Enter the PIN printed by <code>jailbee remote web serve</code>.</p>
        <p v-else>Enter the 6-digit code from your authenticator app.</p>
        <form class="form" @submit.prevent="submit">
            <label>
                <span>This device</span>
                <input
                    v-model="device"
                    name="device"
                    type="text"
                    autocomplete="off"
                    maxlength="100"
                    placeholder="e.g. Boox tablet"
                />
            </label>
            <label v-if="active === 'pin'">
                <span>PIN</span>
                <input
                    v-model="pin"
                    name="pin"
                    class="digits"
                    type="text"
                    inputmode="numeric"
                    autocomplete="one-time-code"
                    pattern="[0-9]*"
                    maxlength="6"
                    placeholder="000000"
                    autofocus
                />
            </label>
            <label v-else>
                <span>Code</span>
                <input
                    v-model="code"
                    name="code"
                    class="digits"
                    type="text"
                    inputmode="numeric"
                    autocomplete="one-time-code"
                    pattern="[0-9]*"
                    maxlength="6"
                    placeholder="000000"
                    autofocus
                />
            </label>
            <button type="submit" class="primary" :disabled="!ready || loading">
                {{ loading ? "…" : "Log in" }}
            </button>
        </form>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
    </main>
</template>

<style scoped>
.login {
    max-width: 22rem;
    margin: 4rem auto;
    padding: 0 var(--space-3);
}
.login h1 {
    margin-bottom: var(--space-2);
}
.form {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-4);
}
label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
}
input {
    font: inherit;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    min-height: 3rem;
}
.tabs {
    display: flex;
    margin-top: var(--space-3);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius);
    overflow: hidden;
}
.tab {
    flex: 1;
    font: inherit;
    min-height: 2.75rem;
    border: none;
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
}
.tab + .tab {
    border-left: 1px solid var(--border-strong);
}
.tab.active {
    background: var(--accent);
    color: var(--accent-text);
}
.digits {
    font-family: var(--font-mono);
    font-size: 1.5rem;
    letter-spacing: 0.4em;
    text-align: center;
}
.primary {
    font: inherit;
    min-height: 3rem;
    border: none;
    border-radius: var(--radius);
    background: var(--accent);
    color: var(--accent-text);
    cursor: pointer;
}
.primary:disabled {
    opacity: 0.5;
    cursor: default;
}
.error {
    margin-top: var(--space-3);
    color: var(--danger);
}
</style>
