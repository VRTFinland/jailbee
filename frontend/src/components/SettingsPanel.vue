<script setup lang="ts">
// Per-device display settings, kept in this browser, plus the name of this
// login session, which lives on the server (`GET/PUT /api/session`).
import {onMounted, ref} from "vue"
import {fetchSession, renameSession} from "../api"
import {useDashboard} from "../dashboard"
import type {MetaMode} from "../protocol"
import Button from "./ui/Button.vue"
import Field from "./ui/Field.vue"
import FieldFrame from "./ui/FieldFrame.vue"
import Switch from "./ui/Switch.vue"
import {
    DISPLAY_PROFILES,
    FONT_SIZE_RANGE,
    PAGE_CONTROLS,
    PAGE_CONTROL_LABELS,
    type PageControls,
    PAGE_STEPS,
    type PageStep,
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
    <div class="settings-panel">
        <p class="muted">Display choices are kept on this device only.</p>

        <fieldset>
            <legend>This session</legend>
            <form class="rename" @submit.prevent="saveDeviceName">
                <Field v-model="deviceName" label="Name shown in the header and in jailbee remote web auth"
                    name="device" autocomplete="off" :maxlength="100" :error="deviceError" />
                <Button type="submit" variant="primary">Save name</Button>
            </form>
            <p v-if="deviceSaved" class="muted" role="status">Saved.</p>
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
            <div class="switch-row">
                <span>Show title bar</span>
                <Switch label="Show title bar" :model-value="settings.showTitleBar"
                    @update:model-value="updateSettings({showTitleBar: $event})" />
            </div>
            <div class="switch-row">
                <span>Follow new messages on e-ink</span>
                <Switch label="Follow new messages on e-ink" :model-value="settings.einkFollow"
                    @update:model-value="updateSettings({einkFollow: $event})" />
            </div>
        </fieldset>

        <fieldset v-if="settings.displayProfile === 'eink'">
            <legend>Page turns</legend>
            <FieldFrame label="Page controls" class="setting-field" v-slot="{controlAttrs}">
                <select v-bind="controlAttrs({'aria-label': 'Page controls'})" class="ui-input ui-focus-ring page-controls"
                    :value="settings.pageControls"
                    @change="updateSettings({pageControls: ($event.target as HTMLSelectElement).value as PageControls})">
                    <option v-for="mode in PAGE_CONTROLS" :key="mode" :value="mode">{{ PAGE_CONTROL_LABELS[mode] }}</option>
                </select>
            </FieldFrame>
            <FieldFrame label="Page step" class="setting-field" v-slot="{controlAttrs}">
                <select v-bind="controlAttrs({})" class="ui-input ui-focus-ring page-step" :value="settings.pageStep"
                    @change="updateSettings({pageStep: number($event) as PageStep})">
                    <option v-for="step in PAGE_STEPS" :key="step" :value="step">{{ step }} %</option>
                </select>
            </FieldFrame>
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
            <div class="switch-row">
                <span>Follow new messages (normal display)</span>
                <Switch label="Follow new messages (normal display)" :model-value="settings.followNewMessages"
                    @update:model-value="updateSettings({followNewMessages: $event})" />
            </div>
            <div class="switch-row">
                <span>Show thinking</span>
                <Switch label="Show thinking" :model-value="settings.showThinking"
                    @update:model-value="updateSettings({showThinking: $event})" />
            </div>
            <FieldFrame label="Agent context" class="setting-field" v-slot="{controlAttrs}">
                <select v-bind="controlAttrs({})" class="ui-input ui-focus-ring agent-context"
                    :value="settings.agentContext"
                    @change="updateSettings({agentContext: ($event.target as HTMLSelectElement).value as MetaMode})">
                    <option value="hidden">Hidden</option>
                    <option value="collapsed">Collapsed</option>
                    <option value="shown">Shown</option>
                </select>
            </FieldFrame>
            <p class="muted">
                Task notifications, system reminders and command output the agent writes into the
                transcript. Hidden sends none of it; Collapsed sends a label and loads the content when
                opened. Slash commands you typed always show as a line.
            </p>
        </fieldset>
    </div>
</template>

<style src="./ui/fields.css"></style>
<style src="./ui/state-layer.css"></style>
<style scoped>
.settings-panel {
    max-width: 36rem;
    margin: 0 auto;
    padding: var(--md-space-16);
}
.muted {
    color: var(--md-on-surface-variant);
}
fieldset {
    min-width: 0;
    overflow-wrap: anywhere;
    margin: var(--md-space-16) 0;
    padding: var(--md-space-8) var(--md-space-16);
    border: 1px solid var(--md-outline-variant);
    border-radius: var(--md-shape-medium);
    background: var(--md-surface-container-low);
    color: var(--md-on-surface);
}
legend {
    padding: 0 var(--md-space-4);
    color: var(--md-on-surface-variant);
}
.choice,
.switch-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-8);
    min-height: var(--md-target-min);
}
.choice {
    justify-content: flex-start;
}
.rename {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-8);
}
.setting-field {
    margin: var(--md-space-8) 0;
}
.slider {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-4);
    margin: var(--md-space-8) 0;
}
.slider input {
    width: 100%;
    min-height: var(--md-target-min);
}
.sample code {
    font-size: var(--md-type-body-medium-size);
    font-family: var(--md-font-mono);
}
</style>
