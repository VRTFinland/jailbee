// Per-device display preferences: one reactive object, kept in browser
// storage and applied to the document root. Storage access is wrapped by
// ./storage, so with storage blocked the app runs on defaults and keeps
// choices in memory for the session.
//
// To add a setting: extend `Settings`,
// give it a key, a default and a parser in `FIELDS`.
import {reactive, watch} from "vue"
import type {ContainerFilter, MetaMode} from "./protocol"
import {readItem, writeItem} from "./storage"

export type Theme = "auto" | "light" | "dark" | "eink"
export const THEMES: readonly Theme[] = ["auto", "light", "dark", "eink"]
export const THEME_LABELS: Record<Theme, string> = {auto: "Auto", light: "Light", dark: "Dark", eink: "E-ink"}
/** How the UI behaves: "eink" pages instead of scrolling and never animates. */
export type DisplayProfile = "normal" | "eink"
export const DISPLAY_PROFILES: readonly DisplayProfile[] = ["normal", "eink"]
/** Agent context: what transcripts show of the agent's own wrapper records. */
export const AGENT_CONTEXT_MODES: readonly MetaMode[] = ["hidden", "collapsed", "shown"]
export const FONT_SIZE_RANGE = {min: 12, max: 28} as const

export interface Settings {
    theme: Theme
    displayProfile: DisplayProfile
    bodyFontSize: number
    codeFontSize: number
    showThinking: boolean
    einkFollow: boolean
    followNewMessages: boolean
    agentContext: MetaMode
    containerFilter: ContainerFilter
}

interface Field<K extends keyof Settings> {
    key: string
    /** The stored string to a valid value, or undefined for garbage/absent. */
    parse: (raw: string | null) => Settings[K] | undefined
    serialize: (value: Settings[K]) => string
    fallback: () => Settings[K]
}

function clampSize(value: number): number {
    return Math.min(FONT_SIZE_RANGE.max, Math.max(FONT_SIZE_RANGE.min, Math.round(value)))
}

function sizeField<K extends "bodyFontSize" | "codeFontSize">(key: string, fallback: number): Field<K> {
    return {
        key,
        parse: (raw) => {
            const n = raw === null ? Number.NaN : Number(raw)
            return Number.isFinite(n) ? clampSize(n) : undefined
        },
        serialize: String,
        fallback: () => fallback,
    }
}

function prefersDark(): boolean {
    try {
        return window.matchMedia("(prefers-color-scheme: dark)").matches
    } catch {
        return false
    }
}

const FIELDS: {[K in keyof Settings]: Field<K>} = {
    containerFilter: {
        key: "jailbee.containerFilter",
        parse: (raw) => (["all", "running", "active"] as const).find(mode => mode === raw),
        serialize: String,
        fallback: () => "all",
    },
    theme: {
        key: "jailbee.theme",
        parse: (raw) => THEMES.find((t) => t === raw),
        serialize: String,
        fallback: () => "auto",
    },
    displayProfile: {
        key: "jailbee.displayProfile",
        parse: (raw) => DISPLAY_PROFILES.find((p) => p === raw),
        serialize: String,
        fallback: () => "normal",
    },
    bodyFontSize: sizeField<"bodyFontSize">("jailbee.bodyFontSize", 16),
    codeFontSize: sizeField<"codeFontSize">("jailbee.codeFontSize", 14),
    showThinking: {
        key: "jailbee.showThinking",
        parse: (raw) => (raw === "1" ? true : raw === "0" ? false : undefined),
        serialize: (v) => (v ? "1" : "0"),
        fallback: () => false,
    },
    followNewMessages: {
        key: "jailbee.followNewMessages",
        parse: (raw) => (raw === "1" ? true : raw === "0" ? false : undefined),
        serialize: (v) => (v ? "1" : "0"),
        fallback: () => true,
    },
    einkFollow: {
        key: "jailbee.einkFollow",
        parse: (raw) => (raw === "1" ? true : raw === "0" ? false : undefined),
        serialize: (v) => (v ? "1" : "0"),
        fallback: () => false,
    },
    agentContext: {
        key: "jailbee.agentContext",
        parse: (raw) => AGENT_CONTEXT_MODES.find((m) => m === raw),
        serialize: String,
        fallback: () => "hidden",
    },
}

function load<K extends keyof Settings>(name: K): Settings[K] {
    const field = FIELDS[name] as Field<K>
    return field.parse(readItem(field.key)) ?? field.fallback()
}

function loadAll(): Settings {
    return {
        theme: load("theme"),
        displayProfile: load("displayProfile"),
        bodyFontSize: load("bodyFontSize"),
        codeFontSize: load("codeFontSize"),
        showThinking: load("showThinking"),
        einkFollow: load("einkFollow"),
        followNewMessages: load("followNewMessages"),
        agentContext: load("agentContext"),
        containerFilter: load("containerFilter"),
    }
}

/** The live settings. Read from components; change through `updateSettings`. */
export const settings: Settings = reactive(loadAll())

/** Re-read storage (and the colour scheme) into `settings`. */
export function reloadSettings(): void {
    Object.assign(settings, loadAll())
}

/** Change settings and remember them on this device. */
export function updateSettings(patch: Partial<Settings>): void {
    const next: Partial<Settings> = {...patch}
    if (next.bodyFontSize !== undefined) next.bodyFontSize = clampSize(next.bodyFontSize)
    if (next.codeFontSize !== undefined) next.codeFontSize = clampSize(next.codeFontSize)
    Object.assign(settings, next)
    for (const name of Object.keys(next) as (keyof Settings)[]) {
        const field = FIELDS[name] as Field<typeof name>
        writeItem(field.key, field.serialize(settings[name] as never))
    }
}

/** Mirror `settings` onto `root` (theme and profile attributes, font-size variables). */
export function startSettings(root: HTMLElement = document.documentElement): void {
    const applyTheme = () => {
        root.dataset.theme = settings.theme === "auto" ? (prefersDark() ? "dark" : "light") : settings.theme
    }
    try {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme)
    } catch { /* Some readers do not expose media query listeners. */ }
    watch(
        () => ({...settings}),
        (s) => {
            applyTheme()
            root.dataset.profile = s.displayProfile
            root.style.setProperty("--font-size-body", `${s.bodyFontSize}px`)
            root.style.setProperty("--font-size-code", `${s.codeFontSize}px`)
        },
        {immediate: true, flush: "sync"},
    )
}
