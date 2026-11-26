import { describe, expect, it, vi } from 'vitest'
import { createAnswerDraftStore } from './answerDrafts'

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
