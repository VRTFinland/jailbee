<script setup lang="ts">
// PIN and authenticator-code (TOTP) login. Ported from codereader's LoginView;
// the server says which methods are on (`GET /api/auth/methods`).
import {computed, onMounted, ref, watch} from "vue"
import {useRoute, useRouter} from "vue-router"
import Button from "../components/ui/Button.vue"
import Field from "../components/ui/Field.vue"
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
            <Button
                v-for="m in codeMethods"
                :key="m"
                type="button"
                role="tab"
                :data-method="m"
                :aria-selected="active === m"
                class="tab"
                :variant="active === m ? 'tonal' : 'text'"
                @click="choose(m)"
            >
                {{ LABELS[m] }}
            </Button>
        </div>
        <p v-if="codeMethods.length === 0">Log in with your security key.</p>
        <p v-else-if="active === 'pin'">Enter the PIN printed by <code>jailbee remote web serve</code>.</p>
        <p v-else>Enter the 6-digit code from your authenticator app.</p>
        <form class="form" @submit.prevent="submit">
            <Field
                v-if="codeMethods.length > 0 && active === 'pin'"
                v-model="pin"
                label="PIN"
                name="pin"
                type="text"
                inputmode="numeric"
                autocomplete="one-time-code"
                pattern="[0-9]*"
                maxlength="6"
                placeholder="000000"
                autofocus
            />
            <Field
                v-else-if="codeMethods.length > 0"
                v-model="code"
                label="Code"
                name="code"
                type="text"
                inputmode="numeric"
                autocomplete="one-time-code"
                pattern="[0-9]*"
                maxlength="6"
                placeholder="000000"
                autofocus
            />
            <Button v-if="codeMethods.length > 0" type="submit" variant="primary" :disabled="!ready || loading">
                {{ loading ? "…" : "Log in" }}
            </Button>
            <Button
                v-if="securityKey"
                type="button"
                data-test="security-key"
                :variant="codeMethods.length > 0 ? 'tonal' : 'primary'"
                :disabled="loading"
                @click="useSecurityKey"
            >
                Use security key
            </Button>
        </form>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
    </main>
</template>

<style scoped>
.login {
    max-width: 22rem;
    margin: 0 auto;
    padding: max(calc(var(--md-space-32) + var(--md-space-32)), calc(var(--quick-trigger-top) + var(--quick-trigger-size) + var(--md-space-12))) var(--md-space-16) calc(var(--md-space-32) + var(--md-space-32));
}
.login h1 {
    margin-bottom: var(--md-space-8);
    font-size: var(--md-type-title-large-size);
    line-height: var(--md-type-title-large-line-height);
    font-weight: var(--md-type-title-large-weight);
}
.form {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-16);
    margin-top: var(--md-space-24);
}
.tabs {
    display: flex;
    margin-top: var(--md-space-16);
}
.tab {
    flex: 1;
}
.error {
    margin-top: var(--md-space-16);
    color: var(--md-error);
}
</style>
