<script setup lang="ts">
// Setup mode: register a FIDO2 security key or passkey. Ported from codereader's
// AuthSetupView, cut down to the key registration; everything else about logins
// is managed with `jailbee remote web auth` on the host.
import {computed, ref} from "vue"
import {registerSecurityKey} from "../setup"

const pin = ref("")
const name = ref("")
const error = ref("")
const registered = ref<string[]>([])
const loading = ref(false)
const here = window.location.hostname

const ready = computed(() => /^\d{6}$/.test(pin.value) && !loading.value)

async function submit() {
    if (!ready.value) return
    error.value = ""
    loading.value = true
    const result = await registerSecurityKey(pin.value, name.value.trim())
    loading.value = false
    pin.value = ""
    if (result.ok) {
        registered.value.push(result.name || "key")
        name.value = ""
    } else {
        error.value = result.error
    }
}
</script>

<template>
    <main class="setup">
        <h1>Register a security key</h1>
        <p>
            Enter the setup PIN printed by <code>jailbee remote web auth fido2 setup</code>, then touch your key.
            The key will log in only at <code>{{ here }}</code>: open this page at the address you log in at.
        </p>
        <form class="form" @submit.prevent="submit">
            <label>
                <span>Key name</span>
                <input
                    v-model="name"
                    name="key-name"
                    type="text"
                    autocomplete="off"
                    maxlength="100"
                    placeholder="e.g. YubiKey 5"
                />
            </label>
            <label>
                <span>Setup PIN</span>
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
            <button type="submit" class="primary" :disabled="!ready">
                {{ loading ? "Touch your key…" : "Register key" }}
            </button>
        </form>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <ul v-if="registered.length" class="done">
            <li v-for="(k, i) in registered" :key="i">Registered {{ k }}.</li>
        </ul>
        <p v-if="registered.length">
            Register another key with the new PIN, or stop setup mode (Ctrl-C) and start the dashboard again.
        </p>
    </main>
</template>

<style scoped>
.setup {
    max-width: 26rem;
    margin: 4rem auto;
    padding: 0 var(--space-3);
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
.done {
    margin-top: var(--space-3);
    padding-left: var(--space-4);
}
</style>
