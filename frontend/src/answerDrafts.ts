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
const completionNamespace = 'jailbee:answer-completed:'

/** The key is the existing JSON-encoded session tuple, without the namespace. */
export function createCompletionHistory(storage?: Storage | null) {
  const memory = new Map<string, string[]>()
  function getStorage(): Storage | null {
    try { return storage === undefined ? window.localStorage : storage } catch { return null }
  }
  function read(sessionKey: string): string[] {
    if (memory.has(sessionKey)) return [...memory.get(sessionKey)!]
    try {
      const value: unknown = JSON.parse(getStorage()?.getItem(completionNamespace + sessionKey) ?? '[]')
      return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string').slice(-100) : []
    } catch { return [] }
  }
  function write(sessionKey: string, ids: string[]): string[] {
    const bounded = [...new Set(ids)].slice(-100)
    memory.delete(sessionKey)
    memory.set(sessionKey, bounded)
    while (memory.size > 100) memory.delete(memory.keys().next().value!)
    try {
      const store = getStorage()
      if (store) {
        const id = completionNamespace + sessionKey
        store.removeItem(id)
        store.setItem(id, JSON.stringify(bounded))
        const keys: string[] = []
        for (let i = 0; i < store.length; i++) {
          const key = store.key(i)
          if (key?.startsWith(completionNamespace) && !key.endsWith(':selected')) keys.push(key)
        }
        for (const old of keys.filter(key => key !== id).slice(0, Math.max(0, keys.length - 100))) {
          store.removeItem(old)
          store.removeItem(old + ':selected')
          memory.delete(old.slice(completionNamespace.length))
        }
      }
    } catch { /* Keep the in-memory history. */ }
    return [...bounded]
  }
  return { read, write }
}

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
    memory.delete(id)
    memory.set(id, raw)
    try {
      const store = getStorage()
      if (store) {
        store.removeItem(id)
        store.setItem(id, raw)
        const keys: string[] = []
        for (let i = 0; i < store.length; i++) {
          const key = store.key(i)
          if (key?.startsWith(namespace)) keys.push(key)
        }
        for (const old of keys.filter(key => key !== id).slice(0, Math.max(0, keys.length - 100))) {
          store.removeItem(old)
          memory.delete(old)
        }
      }
    } catch { /* Keep the in-memory draft. */ }
    while (memory.size > 100) memory.delete(memory.keys().next().value!)
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
