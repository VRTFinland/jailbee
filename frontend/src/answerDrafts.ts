export interface DraftSession {
  prefix: string
  agent: string
  session: string
}

export interface DraftKey extends DraftSession {
  messageId: string
}

export interface AnswerDraft {
  sourceText: string
  answers: { value: string; note: string }[]
}

export interface ListedAnswerDraft extends AnswerDraft {
  messageId: string
}

const namespace = 'jailbee:answer-draft:'

function keyFor(key: DraftKey): string {
  return namespace + JSON.stringify([key.prefix, key.agent, key.session, key.messageId])
}

function parseDraft(raw: string): AnswerDraft | null {
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object') return null
    const draft = value as Record<string, unknown>
    if (typeof draft.sourceText !== 'string' || !Array.isArray(draft.answers)) return null
    if (!draft.answers.every((answer: unknown) => {
      if (!answer || typeof answer !== 'object') return false
      const item = answer as Record<string, unknown>
      return typeof item.value === 'string' && typeof item.note === 'string'
    })) return null
    return { sourceText: draft.sourceText, answers: draft.answers.map((item: { value: string; note: string }) => ({ value: item.value, note: item.note })) }
  } catch {
    return null
  }
}

export function createAnswerDraftStore(storage?: Storage | null) {
  const memory = new Map<string, string | null>()
  function getStorage(): Storage | null {
    try { return storage === undefined ? window.localStorage : storage } catch { return null }
  }
  function load(key: DraftKey): AnswerDraft | null {
    const id = keyFor(key)
    let raw: string | null
    if (memory.has(id)) raw = memory.get(id) ?? null
    else {
      try { raw = getStorage()?.getItem(id) ?? null } catch { raw = null }
    }
    return raw === null ? null : parseDraft(raw)
  }
  function save(key: DraftKey, draft: AnswerDraft): void {
    const id = keyFor(key)
    const raw = JSON.stringify(draft)
    memory.set(id, raw)
    try { getStorage()?.setItem(id, raw) } catch { /* Keep the in-memory draft. */ }
  }
  function discard(key: DraftKey): void {
    const id = keyFor(key)
    memory.set(id, null)
    try { getStorage()?.removeItem(id) } catch { /* Keep the in-memory deletion. */ }
  }
  function list(session: DraftSession): ListedAnswerDraft[] {
    const keys = new Set(memory.keys())
    try {
      const store = getStorage()
      if (store) for (let i = 0; i < store.length; i++) {
        const key = store.key(i)
        if (key) keys.add(key)
      }
    } catch { /* Fall back to in-memory drafts. */ }
    const drafts: ListedAnswerDraft[] = []
    for (const id of keys) {
      if (!id.startsWith(namespace)) continue
      try {
        const parts: unknown = JSON.parse(id.slice(namespace.length))
        if (!Array.isArray(parts) || parts.length !== 4 || parts.some(part => typeof part !== 'string')) continue
        if (parts[0] !== session.prefix || parts[1] !== session.agent || parts[2] !== session.session) continue
        const messageId: string = parts[3]
        const draft = load({ ...session, messageId })
        if (draft) drafts.push({ messageId, ...draft })
      } catch { /* Ignore malformed storage keys. */ }
    }
    return drafts
  }
  return { save, load, list, discard }
}
