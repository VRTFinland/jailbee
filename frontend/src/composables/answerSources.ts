import type {RenderedEntry} from "../protocol"
import type {AnswerSource} from "./useAnswerSets"

export interface AnswerMessage extends AnswerSource {entries: string[]}

/** Source ids come from the transcript adapter, never inferred from block ids or timestamps. */
export function answerMessages(entries: RenderedEntry[]): AnswerMessage[] {
    const messages = new Map<string, AnswerMessage>()
    for (const entry of entries) {
        if (!["assistant", "thinking", "tool_call"].includes(entry.kind)) continue
        const id = entry.message_id ?? entry.id
        let message = messages.get(id)
        if (!message) {
            message = {id, text: "", entries: []}
            messages.set(id, message)
        }
        message.entries.push(entry.id)
        if (entry.kind === "assistant" && entry.text) {
            message.text += (message.text ? "\n\n" : "") + entry.text
        }
    }
    return [...messages.values()]
}
