<script setup lang="ts">
// Per-device display settings. Nothing here goes to the server.
import {
    DISPLAY_PROFILES,
    FONT_SIZE_RANGE,
    settings,
    THEMES,
    updateSettings,
    type DisplayProfile,
    type Theme,
} from "../settings"

const THEME_LABELS: Record<Theme, string> = {light: "Light", dark: "Dark", eink: "E-ink"}

const PROFILE_LABELS: Record<DisplayProfile, string> = {
    normal: "Normal (scrolling)",
    eink: "E-ink (page turns, no animation, slow refresh)",
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
        <p class="muted">These choices are kept on this device only.</p>

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
                <input
                    class="show-thinking"
                    type="checkbox"
                    :checked="settings.showThinking"
                    @change="updateSettings({showThinking: ($event.target as HTMLInputElement).checked})"
                />
                Show thinking
            </label>
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
    align-items: center;
    gap: var(--space-2);
    min-height: 2.75rem;
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
