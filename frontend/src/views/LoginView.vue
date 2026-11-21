<script setup lang="ts">
// PIN login. Ported from codereader's LoginView (PIN part); later slices add
// TOTP and security keys as further methods.
import {computed, ref} from "vue"
import {useRouter} from "vue-router"
import {loginWithPin, rememberedDeviceName} from "../api"

const router = useRouter()
const pin = ref("")
const device = ref(rememberedDeviceName())
const error = ref("")
const loading = ref(false)

const ready = computed(() => /^\d{6}$/.test(pin.value) && device.value.trim().length > 0)

async function submit() {
    if (!ready.value || loading.value) return
    error.value = ""
    loading.value = true
    const result = await loginWithPin(pin.value, device.value.trim())
    loading.value = false
    if (result.ok) {
        await router.push({name: "front"})
    } else {
        error.value = result.error
        pin.value = ""
    }
}
</script>

<template>
    <main class="login">
        <h1>JailBee</h1>
        <p>Enter the PIN printed by <code>jailbee remote web serve</code>.</p>
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
            <label>
                <span>PIN</span>
                <input
                    v-model="pin"
                    name="pin"
                    class="pin"
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
.pin {
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
