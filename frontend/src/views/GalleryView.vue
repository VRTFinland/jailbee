<script setup lang="ts">
// The design system gallery (#167, `/ui`): the visual reference every view is
// checked against. Each panel scopes one theme and one display profile, so the
// whole system is judged on one screen. Behind login; linked from no menu.
// Portalled overlays preserve the sample panel's theme and display profile.
import {reactive} from "vue"
import AnswerPanel from "../components/ui/AnswerPanel.vue"
import Disclosure from "../components/ui/Disclosure.vue"
import TranscriptEntry from "../components/ui/TranscriptEntry.vue"
import Button from "../components/ui/Button.vue"
import Dialog from "../components/ui/Dialog.vue"
import Field from "../components/ui/Field.vue"
import Menu from "../components/ui/Menu.vue"
import MenuRow from "../components/ui/MenuRow.vue"
import Select from "../components/ui/Select.vue"
import Icon from "../components/ui/Icon.vue"
import IconButton from "../components/ui/IconButton.vue"
import ListRow from "../components/ui/ListRow.vue"
import StatusChip, {STATUS_NAMES} from "../components/ui/StatusChip.vue"
import Switch from "../components/ui/Switch.vue"
import {rowStatuses} from "../components/ui/status"
import {ICON_NAMES} from "../components/ui/material-symbols/paths"
import iconLicence from "../components/ui/material-symbols/LICENSE?url&no-inline"
import iconNotice from "../components/ui/material-symbols/NOTICE.md?url&no-inline"
const sampleQuestions = {questions: [{number: 1, title: "Choose an approach", body: "Which approach should we take?", options: [{letter: "a", text: "Keep the current approach"}, {letter: "b", text: "Try a new approach"}], recommendation: "a", start: 0, end: 1}]}
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

const BUTTON_VARIANTS = ["primary", "tonal", "text", "danger"] as const

/** The one live sample switch of each panel, keyed by panel. */
const followLive = reactive<Record<string, boolean>>(Object.fromEntries(
    THEMES.flatMap((t) => PROFILES.map((p) => [`${t.id}-${p.id}`, false])),
))

const selectedTheme = reactive<Record<string, string>>(Object.fromEntries(
    THEMES.flatMap((t) => PROFILES.map((p) => [`${t.id}-${p.id}`, "light"])),
))

/** The linked sample rows' chips, through the same mapping the views use. */
const ORCHESTRATOR_CHIPS = rowStatuses({state: "Running", phase: null, agent: "busy"})
const CHILD_CHIPS = rowStatuses({state: "Running", phase: "job", agent: "waiting"})

const SHAPES = ["extra-small", "small", "medium", "large", "extra-large", "full"]

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
                        <h3 class="group-title">Buttons</h3>
                        <div class="samples">
                            <template v-for="variant in BUTTON_VARIANTS" :key="variant">
                                <Button :variant="variant">{{ variant }}</Button>
                                <Button :variant="variant" disabled>{{ variant }} disabled</Button>
                            </template>
                        </div>
                        <div class="samples">
                            <Button variant="primary" icon="add">New container</Button>
                            <Button variant="tonal" icon="settings">Settings</Button>
                        </div>
                        <div class="samples">
                            <IconButton icon="menu" label="Menu" />
                            <IconButton icon="close" label="Close" />
                            <IconButton icon="menu" label="Menu (tonal)" variant="tonal" />
                            <IconButton icon="settings" label="Settings (tonal, disabled)" variant="tonal" disabled />
                            <IconButton icon="settings" label="Settings (disabled)" disabled />
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Switches</h3>
                        <div class="samples">
                            <Switch :model-value="true" label="On" />
                            <Switch :model-value="false" label="Off" />
                            <Switch :model-value="true" label="On, disabled" disabled />
                            <Switch :model-value="false" label="Off, disabled" disabled />
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Status chips</h3>
                        <div class="samples">
                            <StatusChip v-for="status in STATUS_NAMES" :key="status" :status="status" />
                            <StatusChip status="error" label="API error: overloaded" />
                            <StatusChip status="unknown" label="Error" />
                            <StatusChip status="idle" label="idle 5m" emphasis />
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">List rows</h3>
                        <ul class="rows">
                            <ListRow title="dev-plan" secondary="waiting for you · 2 min" selected>
                                <template #trailing><StatusChip status="running" /></template>
                            </ListRow>
                            <ListRow title="fix-issue-165" secondary="autostart 2/4 · dashboard">
                                <template #trailing><StatusChip status="booting" /></template>
                            </ListRow>
                            <ListRow title="feat-reader-chrome" secondary="exit 1 · make web-e2e">
                                <template #trailing>
                                    <StatusChip status="failed" />
                                    <IconButton icon="menu" label="Actions for feat-reader-chrome" />
                                </template>
                            </ListRow>
                            <ListRow title="Follow live">
                                <template #trailing>
                                    <Switch v-model="followLive[`${theme.id}-${profile.id}`]" label="Follow live" />
                                </template>
                            </ListRow>
                            <ListRow title="dev-ork" secondary="orchestrator · 3 min"
                                link-label="dev-ork running working orchestrator 3 min"
                                :to="{name: 'gallery', query: {row: 'dev-ork'}}">
                                <p class="row-line">Orchestrator · 2 children</p>
                                <dl class="row-cells">
                                    <div><dt>Sync</dt><dd>↑2</dd></div>
                                    <div><dt>Diff</dt><dd>+12 −3</dd></div>
                                </dl>
                                <template #trailing>
                                    <StatusChip v-for="chip in ORCHESTRATOR_CHIPS" :key="chip.status" v-bind="chip" />
                                    <IconButton icon="menu" label="Actions for dev-ork" />
                                </template>
                            </ListRow>
                            <ListRow wrap context title="A long container name that wraps instead of truncating" secondary="Wrapped title and chips above metadata">
                                <template #trailing><StatusChip status="booting" /></template>
                                <p class="row-line">Orchestrator · 2 children</p>
                            </ListRow>
                            <ListRow title="dev-ork-1" secondary="child · codex" selected
                                :to="{name: 'gallery', query: {row: 'dev-ork-1'}}">
                                <p class="row-line">Child of <router-link
                                    :to="{name: 'gallery', query: {row: 'dev-ork'}}">dev-ork</router-link> · running</p>
                                <template #trailing>
                                    <StatusChip v-for="chip in CHILD_CHIPS" :key="chip.status" v-bind="chip" />
                                </template>
                            </ListRow>
                        </ul>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Fields</h3>
                        <div class="field-samples">
                            <Field label="Container name" model-value="dev-plan" help="Use a short, descriptive name." />
                            <Field label="Invalid name" error="A name is required." />
                            <Field label="Disabled field" model-value="read-only" disabled />
                            <Field label="Multiline field" model-value="First line&#10;Second line" multiline />
                            <Select label="Theme" help="Choose a colour theme." v-model="selectedTheme[`${theme.id}-${profile.id}`]" :options="[
                                {value: 'light', label: 'Light'}, {value: 'dark', label: 'Dark'},
                                {value: 'system', label: 'System', disabled: true}, {value: 'eink', label: 'E-ink'}]" />
                            <Select native label="Native preferences" model-value="hidden" :options="[{value: 'hidden', label: 'Hidden'}, {value: 'shown', label: 'Shown'}]" />
                            <Select label="Disabled select" model-value="light" :options="[{value: 'light', label: 'Light'}]" disabled />
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Overlays</h3>
                        <div class="samples">
                            <Dialog title="Destroy dev-plan?" description="The container and its uncommitted work are deleted.">
                                <template #trigger><Button>Open centred dialog</Button></template>
                                <template #actions="{close}">
                                    <Button variant="text" @click="close">Cancel</Button>
                                    <Button variant="danger" @click="close">Destroy</Button>
                                </template>
                            </Dialog>
                            <Dialog title="Container settings" description="Preferences for dev-plan." placement="drawer">
                                <template #trigger><Button>Open drawer</Button></template>
                                <Field label="Drawer name" model-value="dev-plan" />
                            </Dialog>
                            <Dialog title="Quick preferences" placement="popover" preserve-scroll hide-header close-label="Close preferences">
                                <template #trigger><Button>Open preferences</Button></template>
                                <MenuRow><Field label="Device name" model-value="Reading tablet" /></MenuRow>
                                <MenuRow action selected>Light theme</MenuRow>
                                <MenuRow as="label"><span>Follow new messages</span><Switch label="Follow new messages" :model-value="true" /></MenuRow>
                                <MenuRow action section-start>Save preferences</MenuRow>
                            </Dialog>
                            <Menu label="Container actions" :items="[
                                {value: 'shell', label: 'Shell', icon: 'forward'}, {value: 'logs', label: 'Logs', icon: 'page-down'},
                                {value: 'stop', label: 'Stop', disabled: true},
                                {value: 'destroy', label: 'Destroy', variant: 'danger', icon: 'remove'}]">
                                <template #trigger><Button>Container actions</Button></template>
                            </Menu>
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Answer panel</h3>
                        <AnswerPanel :set="sampleQuestions" :available="true" />
                        <AnswerPanel :set="sampleQuestions" :available="true" initial-height="half" />
                        <AnswerPanel :set="sampleQuestions" :available="false" reason="Messaging is unavailable." initial-height="full" />
                    </div>

                    <div class="group">
                        <h3 class="group-title">Reader</h3>
                        <TranscriptEntry kind="user">Make the title bar one row.</TranscriptEntry>
                        <TranscriptEntry>I'll update the reader and run the suite.</TranscriptEntry>
                        <TranscriptEntry kind="user" meta>Agent context</TranscriptEntry>
                        <TranscriptEntry kind="thinking">Considering the next step.</TranscriptEntry>
                        <TranscriptEntry kind="orphan">An unattached tool result.</TranscriptEntry>
                        <Button variant="text" truncate>A long prompt excerpt that stays on one line instead of expanding the jump navigation</Button>
                        <Disclosure label="Bash: make web-e2e" />
                        <Disclosure label="Bash: failed command" error />
                        <Disclosure label="Bash: expanded command" :open="true" />
                        <div class="reader-controls">
                            <Button compact variant="text">3 / 18</Button>
                            <Button narrow variant="text">33 / 180</Button>
                            <IconButton compact icon="back" label="Previous page (compact)" />
                            <IconButton compact icon="forward" label="Next page (compact)" />
                            <IconButton compact icon="back" label="Previous page (compact, disabled)" disabled />
                        </div>
                    </div>

                    <div class="group">
                        <h3 class="group-title">Pagination</h3>
                        <nav class="pagination-sample" aria-label="Earlier sessions example">
                            <IconButton icon="back" variant="tonal" label="Previous earlier sessions" disabled />
                            <span aria-live="polite">Page 1 of 3</span>
                            <IconButton icon="forward" variant="tonal" label="Next earlier sessions" />
                        </nav>
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
.group > .entry {
    margin: var(--md-space-24) 0;
}
.reader-controls {
    display: flex;
    align-items: center;
    gap: var(--md-space-8);
    margin-top: var(--md-space-24);
}
.samples {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-8);
}
.samples + .samples {
    margin-top: var(--md-space-8);
}
.field-samples {
    display: grid;
    gap: var(--md-space-16);
}
.rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--md-space-4);
}
.row-line {
    margin: var(--md-space-4) 0 0;
}
.row-cells {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-4) var(--md-space-16);
    margin: var(--md-space-4) 0 0;
}
.row-cells div {
    display: flex;
    gap: var(--md-space-4);
}
.row-cells dt {
    color: var(--md-on-surface-variant);
}
.row-cells dd {
    margin: 0;
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
.pagination-sample { display: flex; align-items: center; justify-content: center; gap: var(--md-space-16); }
.shape:hover {
    background: var(--md-surface-container-highest);
}
</style>
