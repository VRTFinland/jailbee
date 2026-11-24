<script setup lang="ts">
// PIN and authenticator-code (TOTP) login. Ported from codereader's LoginView;
// the server says which methods are on (`GET /api/auth/methods`).
import {computed, onMounted, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import {
    type AuthMethod,
    fetchAuthMethods,
    loginWithLink,
    loginWithPin,
    loginWithSecurityKey,
    loginWithTotp,
} from "../api"

type CodeMethod = Exclude<AuthMethod, "fido2">

const LABELS: Record<CodeMethod, string> = {pin: "PIN", totp: "Authenticator code"}

const router = useRouter()
const route = useRoute()
const methods = ref<AuthMethod[]>(["pin"])
/** The methods typed into the form; a security key is a button of its own. */
const codeMethods = computed(() => methods.value.filter((m): m is CodeMethod => m !== "fido2"))
const securityKey = computed(() => methods.value.includes("fido2"))
const active = ref<CodeMethod>("pin")
const pin = ref("")
const code = ref("")
const error = ref("")
const loading = ref(false)

watch(() => route.query.link, async (link) => {
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
}, {immediate: true})

onMounted(async () => {
    methods.value = await fetchAuthMethods()
    active.value = codeMethods.value[0] ?? "pin"
})

const secret = computed(() => (active.value === "pin" ? pin.value : code.value))
const ready = computed(() => /^\d{6}$/.test(secret.value))

function choose(method: CodeMethod) {
    active.value = method
    error.value = ""
}

async function finish(result: Awaited<ReturnType<typeof loginWithPin>>) {
    if (result.ok) {
        await router.push({name: "front"})
    } else {
        error.value = result.error
    }
}

async function useSecurityKey() {
    if (loading.value) return
    error.value = ""
    loading.value = true
    const result = await loginWithSecurityKey()
    loading.value = false
    await finish(result)
}

async function submit() {
    if (!ready.value || loading.value) return
    error.value = ""
    loading.value = true
    const result =
        active.value === "pin" ? await loginWithPin(pin.value) : await loginWithTotp(code.value)
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
        <div v-if="codeMethods.length > 1" class="tabs" role="tablist">
            <button
                v-for="m in codeMethods"
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
        <p v-if="codeMethods.length === 0">Log in with your security key.</p>
        <p v-else-if="active === 'pin'">Enter the PIN printed by <code>jailbee remote web serve</code>.</p>
        <p v-else>Enter the 6-digit code from your authenticator app.</p>
        <form class="form" @submit.prevent="submit">
            <label v-if="codeMethods.length > 0 && active === 'pin'">
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
            <label v-else-if="codeMethods.length > 0">
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
            <button v-if="codeMethods.length > 0" type="submit" class="primary" :disabled="!ready || loading">
                {{ loading ? "…" : "Log in" }}
            </button>
            <button
                v-if="securityKey"
                type="button"
                data-test="security-key"
                :class="codeMethods.length > 0 ? 'secondary' : 'primary'"
                :disabled="loading"
                @click="useSecurityKey"
            >
                Use security key
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
.secondary {
    font: inherit;
    min-height: 3rem;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
}
.primary:disabled,
.secondary:disabled {
    opacity: 0.5;
    cursor: default;
}
.error {
    margin-top: var(--space-3);
    color: var(--danger);
}
</style>
