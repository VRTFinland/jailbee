<script setup lang="ts">
// Per-device display settings, kept in this browser, plus the name of this
// login session, which lives on the server (`GET/PUT /api/session`).
import {onMounted, ref} from "vue"
import {fetchSession, renameSession} from "../api"
import {useDashboard} from "../dashboard"
import type {MetaMode} from "../protocol"
import {
    DISPLAY_PROFILES,
    FONT_SIZE_RANGE,
    settings,
    THEMES,
    THEME_LABELS,
    updateSettings,
    type DisplayProfile,
} from "../settings"

const PROFILE_LABELS: Record<DisplayProfile, string> = {
    normal: "Normal (scrolling)",
    eink: "E-ink (page turns, no animation, slow refresh)",
}

const dashboard = useDashboard()
const deviceName = ref("")
const deviceError = ref("")
const deviceSaved = ref(false)

onMounted(async () => {
    try {
        deviceName.value = (await fetchSession()).device_name
    } catch {
        // the field stays empty; saving still works
    }
})

async function saveDeviceName() {
    deviceSaved.value = false
    const name = deviceName.value.trim()
    if (!name) {
        deviceError.value = "Give this session a name."
        return
    }
    try {
        const stored = await renameSession(name)
        deviceName.value = stored
        deviceError.value = ""
        deviceSaved.value = true
        if (dashboard) dashboard.state.device = stored
    } catch {
        deviceError.value = "The name could not be saved."
    }
}

function number(event: Event): number {
    return Number((event.target as HTMLInputElement).value)
}
</script>

<template>
    <div class="page">
        <header class="bar">
            <router-link class="back" :to="{name: 'front'}">&larr; JailBee</router-link>
            <h1>Settings</h1>
        </header>
        <p class="muted">Display choices are kept on this device only.</p>

        <fieldset>
            <legend>This session</legend>
            <form class="rename" @submit.prevent="saveDeviceName">
                <label>
                    Name shown in the header and in <code>jailbee remote web auth</code>
                    <input
                        v-model="deviceName"
                        name="device"
                        type="text"
                        autocomplete="off"
                        maxlength="100"
                    />
                </label>
                <button type="submit" class="save-device">Save name</button>
            </form>
            <p v-if="deviceError" class="error" role="alert">{{ deviceError }}</p>
            <p v-else-if="deviceSaved" class="muted" role="status">Saved.</p>
        </fieldset>

        <fieldset>
            <legend>Theme</legend>
            <label v-for="t in THEMES" :key="t" class="choice">
                <input
                    type="radio"
                    name="theme"
                    :value="t"
                    :checked="settings.theme === t"
                    @change="updateSettings({theme: t})"
                />
                {{ THEME_LABELS[t] }}
            </label>
        </fieldset>

        <fieldset>
            <legend>Display profile</legend>
            <label v-for="p in DISPLAY_PROFILES" :key="p" class="choice">
                <input
                    type="radio"
                    name="profile"
                    :value="p"
                    :checked="settings.displayProfile === p"
                    @change="updateSettings({displayProfile: p})"
                />
                {{ PROFILE_LABELS[p] }}
            </label>
            <label class="choice">
                <input
                    type="checkbox"
                    :checked="settings.einkFollow"
                    @change="updateSettings({einkFollow: ($event.target as HTMLInputElement).checked})"
                />
                Follow new messages on e-ink
            </label>
        </fieldset>

        <fieldset>
            <legend>Text size</legend>
            <label class="slider">
                Body: {{ settings.bodyFontSize }}px
                <input
                    class="body-size"
                    type="range"
                    :min="FONT_SIZE_RANGE.min"
                    :max="FONT_SIZE_RANGE.max"
                    :value="settings.bodyFontSize"
                    @input="updateSettings({bodyFontSize: number($event)})"
                />
            </label>
            <label class="slider">
                Code: {{ settings.codeFontSize }}px
                <input
                    class="code-size"
                    type="range"
                    :min="FONT_SIZE_RANGE.min"
                    :max="FONT_SIZE_RANGE.max"
                    :value="settings.codeFontSize"
                    @input="updateSettings({codeFontSize: number($event)})"
                />
            </label>
            <p class="sample">The quick brown fox <code>jumps over()</code> the lazy dog.</p>
        </fieldset>

        <fieldset>
            <legend>Sessions</legend>
            <label class="choice">
                <input type="checkbox" :checked="settings.followNewMessages"
                    @change="updateSettings({followNewMessages: ($event.target as HTMLInputElement).checked})" />
                Follow new messages (normal display)
            </label>
            <label class="choice">
                <input
                    class="show-thinking"
                    type="checkbox"
                    :checked="settings.showThinking"
                    @change="updateSettings({showThinking: ($event.target as HTMLInputElement).checked})"
                />
                Show thinking
            </label>
            <label class="choice">
                Agent context
                <select
                    class="agent-context"
                    :value="settings.agentContext"
                    @change="updateSettings({agentContext: ($event.target as HTMLSelectElement).value as MetaMode})"
                >
                    <option value="hidden">Hidden</option>
                    <option value="collapsed">Collapsed</option>
                    <option value="shown">Shown</option>
                </select>
            </label>
            <p class="muted">
                Task notifications, system reminders and command output the agent writes into the
                transcript. Hidden sends none of it; Collapsed sends a label and loads the content when
                opened. Slash commands you typed always show as a line.
            </p>
        </fieldset>
    </div>
</template>

<style scoped>
.page {
    max-width: 36rem;
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
}
.back {
    color: var(--muted);
    text-decoration: none;
    font-family: var(--font-mono);
}
.muted {
    color: var(--muted);
}
fieldset {
    min-width: 0;
    overflow-wrap: anywhere;
    margin: var(--space-3) 0;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
}
legend {
    padding: 0 var(--space-1);
    color: var(--muted);
}
.choice {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    min-height: 2.75rem;
}
.rename {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
}
.rename label {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
}
.rename input,
.save-device {
    min-width: 0;
    max-width: 100%;
    font: inherit;
    min-height: 2.75rem;
}
.error {
    color: var(--danger);
}
.slider {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: var(--space-2) 0;
}
.slider input {
    width: 100%;
    min-height: 2.25rem;
}
.sample code {
    font-size: var(--font-size-code);
}
</style>
