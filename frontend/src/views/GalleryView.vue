<script setup lang="ts">
// The design system gallery (#167, `/ui`): the visual reference every view is
// checked against. Each panel scopes one theme and one display profile, so the
// whole system is judged on one screen. Behind login; linked from no menu.
// Later tickets add each UI primitive to every panel.
import Icon from "../components/ui/Icon.vue"
import {ICON_NAMES} from "../components/ui/material-symbols/paths"
import iconLicence from "../components/ui/material-symbols/LICENSE?url&no-inline"
import iconNotice from "../components/ui/material-symbols/NOTICE.md?url&no-inline"
const THEMES = [
    {id: "light", label: "Light"},
    {id: "dark", label: "Dark"},
    {id: "eink", label: "E-ink"},
] as const
const PROFILES = [
    {id: "normal", label: "normal"},
    {id: "eink", label: "e-ink"},
] as const

const COLOUR_GROUPS: {title: string; roles: string[]}[] = [
    {
        title: "Surfaces",
        roles: [
            "surface", "surface-container-lowest", "surface-container-low", "surface-container",
            "surface-container-high", "surface-container-highest", "on-surface", "on-surface-variant",
            "outline", "outline-variant", "hairline",
        ],
    },
    {title: "Primary", roles: ["primary", "on-primary", "primary-container", "on-primary-container"]},
    {title: "Error", roles: ["error", "on-error", "error-container", "on-error-container"]},
    {
        title: "Phases",
        roles: ["booting", "autostart", "job", "failed"].flatMap((p) => [
            `phase-${p}`, `phase-${p}-container`, `on-phase-${p}-container`,
        ]),
    },
]

const TYPE_ROLES = ["title", "body", "label"].flatMap((role) =>
    ["large", "medium", "small"].map((size) => ({
        token: `${role}-${size}`,
        label: `${role[0].toUpperCase()}${role.slice(1)} ${size}`,
    })),
)

const SHAPES = ["small", "medium", "large", "extra-large", "full"]

function typeStyle(token: string): Record<string, string> {
    return {
        fontSize: `var(--md-type-${token}-size)`,
        lineHeight: `var(--md-type-${token}-line-height)`,
        fontWeight: `var(--md-type-${token}-weight)`,
    }
}
</script>

<template>
    <main class="gallery">
        <h1 class="gallery-title">Design system</h1>
        <div class="panels">
            <template v-for="theme in THEMES" :key="theme.id">
                <section
                    v-for="profile in PROFILES"
                    :key="`${theme.id}-${profile.id}`"
                    class="panel"
                    :data-theme="theme.id"
                    :data-profile="profile.id"
                    :aria-label="`${theme.label} theme, ${profile.label} profile`"
                >
                    <h2 class="panel-title">{{ theme.label }} theme, {{ profile.label }} profile</h2>

                    <div v-for="group in COLOUR_GROUPS" :key="group.title" class="group">
                        <h3 class="group-title">{{ group.title }}</h3>
                        <ul class="swatches">
                            <li v-for="role in group.roles" :key="role" class="swatch-item">
                                <span
                                    class="swatch"
                                    :data-token="`--md-${role}`"
                                    :style="{background: `var(--md-${role})`}"
                                ></span>
                                <code class="swatch-name">--md-{{ role }}</code>
                            </li>
                        </ul>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Type scale</h3>
                        <p v-for="role in TYPE_ROLES" :key="role.token" class="type-sample" :style="typeStyle(role.token)">
                            {{ role.label }} <code class="swatch-name">--md-type-{{ role.token }}</code>
                        </p>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Icons</h3>
                        <p class="swatch-name">
                            <a :href="iconLicence">Material Symbols licence</a>
                            · <a :href="iconNotice">Source and attribution</a>
                        </p>
                        <p class="swatch-item" role="group" aria-label="Icon sizes">
                            <Icon v-for="size in (['small', 'medium', 'large'] as const)" :key="size"
                                name="settings" :size="size" />
                            <span class="swatch-name">small / medium / large</span>
                        </p>
                        <ul class="swatches">
                            <li v-for="name in ICON_NAMES" :key="name" class="swatch-item">
                                <Icon :name="name" :label="name" />
                                <code class="swatch-name">{{ name }}</code>
                            </li>
                        </ul>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Shape scale</h3>
                        <ul class="shapes">
                            <li
                                v-for="shape in SHAPES"
                                :key="shape"
                                class="shape"
                                :style="{borderRadius: `var(--md-shape-${shape})`}"
                            >
                                {{ shape }}
                            </li>
                        </ul>
                    </div>
                </section>
            </template>
        </div>
    </main>
</template>

<style scoped>
.gallery {
    padding: var(--md-space-16);
}
.gallery-title {
    margin: 0 0 var(--md-space-16);
    font-size: var(--md-type-title-large-size);
    line-height: var(--md-type-title-large-line-height);
    font-weight: var(--md-type-title-large-weight);
}
.panels {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 22rem), 1fr));
    gap: var(--md-space-16);
}
.panel {
    min-width: 0;
    padding: var(--md-space-16);
    background: var(--md-surface);
    color: var(--md-on-surface);
    border: 1px solid var(--md-outline-variant);
    border-radius: var(--md-shape-large);
    font-family: var(--md-font-body);
}
.panel-title {
    margin: 0 0 var(--md-space-12);
    font-size: var(--md-type-title-medium-size);
    line-height: var(--md-type-title-medium-line-height);
    font-weight: var(--md-type-title-medium-weight);
}
.group + .group {
    margin-top: var(--md-space-16);
}
.group-title {
    margin: 0 0 var(--md-space-8);
    color: var(--md-on-surface-variant);
    font-size: var(--md-type-label-large-size);
    line-height: var(--md-type-label-large-line-height);
    font-weight: var(--md-type-label-large-weight);
}
.swatches,
.shapes {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--md-space-8);
}
.swatch-item {
    display: flex;
    align-items: center;
    gap: var(--md-space-8);
    min-width: 0;
}
.swatch {
    flex: none;
    width: var(--md-target-min);
    height: var(--md-space-24);
    border: 1px solid var(--md-outline);
    border-radius: var(--md-shape-small);
}
.swatch-name {
    font-family: var(--md-font-mono);
    font-size: var(--md-type-body-small-size);
    line-height: var(--md-type-body-small-line-height);
    color: var(--md-on-surface-variant);
    overflow-wrap: anywhere;
}
.type-sample {
    margin: 0 0 var(--md-space-4);
}
.shapes {
    grid-template-columns: repeat(auto-fill, minmax(5rem, 1fr));
}
.shape {
    display: grid;
    place-items: center;
    min-height: var(--md-target-row);
    border: 1px solid var(--md-hairline);
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
    font-size: var(--md-type-label-medium-size);
    font-weight: var(--md-type-label-medium-weight);
    transition: background-color var(--md-motion-duration) var(--md-motion-easing);
}
.shape:hover {
    background: var(--md-surface-container-highest);
}
</style>
