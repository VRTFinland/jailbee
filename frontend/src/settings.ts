// Per-device display preferences: one reactive object, kept in browser
// storage and applied to the document root. Storage access is wrapped by
// ./storage, so with storage blocked the app runs on defaults and keeps
// choices in memory for the session.
//
// To add a setting (ticket 10 adds `displayProfile`): extend `Settings`,
// give it a key, a default and a parser in `FIELDS`.
import {reactive, watch} from "vue"
import {readItem, writeItem} from "./storage"

export type Theme = "light" | "dark" | "eink"
export const THEMES: readonly Theme[] = ["light", "dark", "eink"]
export const FONT_SIZE_RANGE = {min: 12, max: 28} as const

export interface Settings {
    theme: Theme
    bodyFontSize: number
    codeFontSize: number
    showThinking: boolean
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
    theme: {
        key: "jailbee.theme",
        parse: (raw) => THEMES.find((t) => t === raw),
        serialize: String,
        fallback: () => (prefersDark() ? "dark" : "light"),
    },
    bodyFontSize: sizeField<"bodyFontSize">("jailbee.bodyFontSize", 16),
    codeFontSize: sizeField<"codeFontSize">("jailbee.codeFontSize", 14),
    showThinking: {
        key: "jailbee.showThinking",
        parse: (raw) => (raw === "1" ? true : raw === "0" ? false : undefined),
        serialize: (v) => (v ? "1" : "0"),
        fallback: () => false,
    },
}

function load<K extends keyof Settings>(name: K): Settings[K] {
    const field = FIELDS[name] as Field<K>
    return field.parse(readItem(field.key)) ?? field.fallback()
}

function loadAll(): Settings {
    return {
        theme: load("theme"),
        bodyFontSize: load("bodyFontSize"),
        codeFontSize: load("codeFontSize"),
        showThinking: load("showThinking"),
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

/** Mirror `settings` onto `root` (theme attribute, font-size variables). */
export function startSettings(root: HTMLElement = document.documentElement): void {
    watch(
        () => ({...settings}),
        (s) => {
            root.dataset.theme = s.theme
            root.style.setProperty("--font-size-body", `${s.bodyFontSize}px`)
            root.style.setProperty("--font-size-code", `${s.codeFontSize}px`)
        },
        {immediate: true, flush: "sync"},
    )
}
