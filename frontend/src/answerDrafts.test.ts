import { describe, expect, it, vi } from 'vitest'
import { createAnswerDraftStore, createCompletionHistory } from './answerDrafts'

describe('answer drafts', () => {
  it('saves, restores, lists and discards drafts by session and source message', () => {
    const storage = new MemoryStorage()
    const store = createAnswerDraftStore(storage)
    const key = { prefix: 'repo', agent: 'claude', session: 's1', messageId: 'm1' }
    const draft = { sourceText: 'question text', answers: [{ value: 'a', note: 'note' }] }
    store.save(key, draft)
    store.save({ ...key, messageId: 'm2' }, { sourceText: 'new', answers: [] })
    store.save({ ...key, session: 's2' }, draft)
    const reloaded = createAnswerDraftStore(storage)
    expect(reloaded.load(key)).toEqual(draft)
    expect(reloaded.list({ prefix: 'repo', agent: 'claude', session: 's1' }).map(x => x.messageId).sort()).toEqual(['m1', 'm2'])
    reloaded.discard(key)
    expect(createAnswerDraftStore(storage).load(key)).toBeNull()
  })

  it('ignores corrupt stored drafts and isolates keys containing punctuation', () => {
    const storage = new MemoryStorage()
    const store = createAnswerDraftStore(storage)
    const key = { prefix: 'a:b', agent: 'c', session: 's', messageId: 'm' }
    store.save(key, { sourceText: 'text', answers: [{ value: 'yes', note: '' }] })
    expect(store.load({ ...key, prefix: 'a', agent: 'b:c' })).toBeNull()
    const storedKey = storage.key(0)!
    storage.setItem(storedKey, '{bad json')
    expect(createAnswerDraftStore(storage).list(key)).toEqual([])
    storage.setItem(storedKey, JSON.stringify({ sourceText: 'text', answers: [{ value: 1, note: '' }] }))
    expect(createAnswerDraftStore(storage).load(key)).toBeNull()
  })

  it('bounds stored drafts across sessions and retains the most recently saved', () => {
    const storage = new MemoryStorage()
    const store = createAnswerDraftStore(storage)
    const draft = { sourceText: 'text', answers: [] }
    for (let i = 0; i < 120; i++) store.save({ prefix: 'repo', agent: 'claude', session: `s${i}`, messageId: 'm' }, draft)
    const reloaded = createAnswerDraftStore(storage)
    expect(storage.length).toBeLessThanOrEqual(100)
    expect(reloaded.load({ prefix: 'repo', agent: 'claude', session: 's0', messageId: 'm' })).toBeNull()
    expect(reloaded.load({ prefix: 'repo', agent: 'claude', session: 's119', messageId: 'm' })).toEqual(draft)
  })

  it('bounds completion IDs and sessions, including after reload', () => {
    const storage = new MemoryStorage()
    const history = createCompletionHistory(storage)
    for (let i = 0; i < 120; i++) history.write(`session${i}`, Array.from({ length: 120 }, (_, n) => `m${n}`))
    const reloaded = createCompletionHistory(storage)
    expect(storage.length).toBeLessThanOrEqual(100)
    expect(reloaded.read('session0')).toEqual([])
    expect(reloaded.read('session119')).toEqual(Array.from({ length: 100 }, (_, n) => `m${n + 20}`))
  })

  it('bounds remembered selections across visits and reloads independently of history', () => {
    const storage = new MemoryStorage()
    const history = createCompletionHistory(storage)
    for (let i = 0; i < 130; i++) history.select(`session${i}`, `m${i}`)
    const reloaded = createCompletionHistory(storage)
    expect(storage.length).toBeLessThanOrEqual(100)
    expect(reloaded.selection('session0')).toBeNull()
    expect(reloaded.selection('session129')).toBe('m129')
    reloaded.select('session129', null)
    expect(createCompletionHistory(storage).selection('session129')).toBeNull()
  })

  it('keeps bounded completion history when storage is unavailable', () => {
    const storage = { getItem: vi.fn(() => { throw Error() }), setItem: vi.fn(() => { throw Error() }), removeItem: vi.fn(() => { throw Error() }), key: vi.fn(() => { throw Error() }), get length(): number { throw Error() }, clear: vi.fn() }
    const history = createCompletionHistory(storage)
    expect(history.read('session')).toEqual([])
    history.write('session', ['first', 'second'])
    expect(history.read('session')).toEqual(['first', 'second'])
    history.select('session', 'first')
    expect(history.selection('session')).toBe('first')
    history.select('session', null)
    expect(history.selection('session')).toBeNull()
  })

  it('keeps usable in-memory drafts when every storage operation throws', () => {
    const storage = { getItem: vi.fn(() => { throw Error() }), setItem: vi.fn(() => { throw Error() }), removeItem: vi.fn(() => { throw Error() }), key: vi.fn(() => { throw Error() }), get length(): number { throw Error() }, clear: vi.fn() }
    const store = createAnswerDraftStore(storage)
    const key = { prefix: 'repo', agent: 'claude', session: 's', messageId: 'm' }
    const draft = { sourceText: 'text', answers: [{ value: 'a', note: '' }] }
    expect(store.load(key)).toBeNull()
    store.save(key, draft)
    expect(store.load(key)).toEqual(draft)
    expect(store.list(key)).toEqual([{ messageId: 'm', ...draft }])
    store.discard(key)
    expect(store.list(key)).toEqual([])
    expect(store.load(key)).toBeNull()
  })
})

class MemoryStorage implements Storage {
  private data = new Map<string, string>()
  get length() { return this.data.size }
  clear() { this.data.clear() }
  getItem(key: string) { return this.data.get(key) ?? null }
  key(index: number) { return [...this.data.keys()][index] ?? null }
  removeItem(key: string) { this.data.delete(key) }
  setItem(key: string, value: string) { this.data.set(key, value) }
}
