// Per-device display preferences, kept in browser storage. The settings view
// (ticket 09) edits these; until then the session view has its own toggle.
import {readItem, writeItem} from "./storage"

const SHOW_THINKING_KEY = "jailbee.showThinking"

/** Whether thinking blocks are shown; hidden unless this device chose otherwise. */
export function showThinking(): boolean {
    return readItem(SHOW_THINKING_KEY) === "1"
}

export function setShowThinking(value: boolean): void {
    writeItem(SHOW_THINKING_KEY, value ? "1" : "0")
}
