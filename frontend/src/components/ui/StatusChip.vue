<script lang="ts">
import type {IconName} from "./material-symbols/paths"

/** Container phases (Incus state and the dashboard core's row phase), then agent states. */
export const STATUS_NAMES = [
    "running", "stopped", "booting", "autostart", "job", "failed",
    "waiting", "busy", "idle", "shell", "error",
] as const
export type StatusName = (typeof STATUS_NAMES)[number]

const STATUSES: Record<StatusName, {icon: IconName; text: string}> = {
    running: {icon: "running", text: "running"},
    stopped: {icon: "stopped", text: "stopped"},
    booting: {icon: "booting", text: "booting"},
    autostart: {icon: "autostart", text: "autostart"},
    job: {icon: "job", text: "job"},
    failed: {icon: "failure", text: "failed"},
    waiting: {icon: "waiting", text: "waiting for you"},
    busy: {icon: "working", text: "working"},
    idle: {icon: "idle", text: "idle"},
    shell: {icon: "job", text: "shell"},
    error: {icon: "error", text: "error"},
}
</script>

<script setup lang="ts">
// A tonal status chip (#167): an icon and words, never colour alone, so it
// reads the same in e-ink, where every chip is black on white with a hairline.
import Icon from "./Icon.vue"

defineProps<{
    status: StatusName
    /** More specific wording, e.g. "API error: overloaded"; the icon and colour stay the status's. */
    label?: string
}>()
</script>

<template>
    <span class="status-chip" :data-status="status"
        ><Icon :name="STATUSES[status].icon" size="small" /><span class="status-chip-text">{{
            label ?? STATUSES[status].text }}</span></span>
</template>

<style scoped>
.status-chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-4);
    max-width: 100%;
    min-height: var(--md-space-24);
    padding: 0 var(--md-space-8);
    border: 1px solid var(--md-hairline);
    border-radius: var(--md-shape-small);
    background: var(--md-surface-container-high);
    color: var(--md-on-surface-variant);
    font-size: var(--md-type-label-medium-size);
    line-height: var(--md-type-label-medium-line-height);
    font-weight: var(--md-type-label-medium-weight);
    white-space: nowrap;
}
.status-chip-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
}
.status-chip[data-status="running"],
.status-chip[data-status="waiting"] {
    background: var(--md-primary-container);
    color: var(--md-on-primary-container);
}
.status-chip[data-status="stopped"] {
    background: var(--md-surface-container-highest);
}
.status-chip[data-status="booting"] {
    background: var(--md-phase-booting-container);
    color: var(--md-on-phase-booting-container);
}
.status-chip[data-status="autostart"] {
    background: var(--md-phase-autostart-container);
    color: var(--md-on-phase-autostart-container);
}
.status-chip[data-status="job"] {
    background: var(--md-phase-job-container);
    color: var(--md-on-phase-job-container);
}
.status-chip[data-status="failed"] {
    background: var(--md-phase-failed-container);
    color: var(--md-on-phase-failed-container);
}
.status-chip[data-status="error"] {
    background: var(--md-error-container);
    color: var(--md-on-error-container);
}
</style>
