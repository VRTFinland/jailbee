<script setup lang="ts">
// Setup mode: register a FIDO2 security key or passkey. Ported from codereader's
// AuthSetupView, cut down to the key registration; everything else about logins
// is managed with `jailbee remote web auth` on the host.
import {computed, ref} from "vue"
import Button from "../components/ui/Button.vue"
import Field from "../components/ui/Field.vue"
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
            <Field
                v-model="name"
                label="Key name"
                name="key-name"
                type="text"
                autocomplete="off"
                maxlength="100"
                placeholder="e.g. YubiKey 5"
            />
            <Field
                v-model="pin"
                label="Setup PIN"
                name="pin"
                type="text"
                inputmode="numeric"
                autocomplete="one-time-code"
                pattern="[0-9]*"
                maxlength="6"
                placeholder="000000"
                autofocus
            />
            <Button type="submit" variant="primary" :disabled="!ready">
                {{ loading ? "Touch your key…" : "Register key" }}
            </Button>
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
    margin: 0 auto;
    padding: max(calc(var(--md-space-32) + var(--md-space-32)), calc(var(--quick-trigger-top) + var(--quick-trigger-size) + var(--md-space-12))) var(--md-space-16) calc(var(--md-space-32) + var(--md-space-32));
}
.setup h1 {
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
.error {
    margin-top: var(--md-space-16);
    color: var(--md-error);
}
.done {
    margin-top: var(--md-space-16);
    padding-left: var(--md-space-24);
}
</style>
