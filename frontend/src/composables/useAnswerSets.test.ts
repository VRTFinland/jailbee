import {effectScope, nextTick, ref} from "vue"
import {beforeEach, describe, expect, it, vi} from "vitest"
import {createAnswerDraftStore} from "../answerDrafts"
import {useAnswerSets, type AnswerSource} from "./useAnswerSets"

const session = {prefix: "repo", agent: "claude", session: "session"}
const text = "❓ **Q12** - **Deploy**: Choose.\na) Canary\nb) All\n➡️ a: Safe"
const newer = "❓ **Q13** - **Owner**: Who?"
function setup(sources = ref<AnswerSource[]>([{id: "old", text}])) {
    const scope = effectScope()
    const state = scope.run(() => useAnswerSets(() => session, () => sources.value))!
    return {scope, state, sources}
}

beforeEach(() => localStorage.clear())
describe("session answer drafts", () => {
    it("restores answers and keeps a newer set independent without replacing the selected draft", async () => {
        const first = setup()
        first.state.updateAnswers([{value: "b", note: "after review"}])
        first.sources.value.push({id: "new", text: newer})
        await nextTick()
        expect(first.state.selected.value?.id).toBe("old")
        expect(first.state.hasNewer.value).toBe(true)
        expect(first.state.sets.value.map(set => set.id)).toEqual(["old", "new"])
        first.state.select("new")
        first.state.updateAnswers([{value: "Platform", note: ""}])
        first.state.select("old")
        first.scope.stop()
        const reloaded = setup(ref([{id: "new", text: newer}]))
        expect(reloaded.state.selected.value?.id).toBe("old")
        expect(reloaded.state.selected.value?.answers).toEqual([{value: "b", note: "after review"}])
        reloaded.state.select("new")
        expect(reloaded.state.selected.value?.answers).toEqual([{value: "Platform", note: ""}])
        reloaded.scope.stop()
    })

    it("selects the newest set when the selected untouched set expires beside an older edited draft", async () => {
        const {state, sources, scope} = setup()
        state.updateAnswers([{value: "b", note: "old edited"}])
        sources.value.push({id: "middle", text: newer})
        await nextTick()
        state.select("middle")
        sources.value.push({id: "latest", text: "❓ **Q14** - **Team**: Who?"})
        await nextTick()
        expect(state.selected.value?.id).toBe("latest")
        expect(state.sets.value.map(set => set.id)).toEqual(["old", "latest"])
        scope.stop()
    })

    it("retains edited answers when more text records complete the same API message", async () => {
        const {state, sources, scope} = setup()
        state.updateAnswers([{value: "b", note: "keep this"}])
        sources.value = [{id: "old", text: text + "\n\n" + newer}]
        await nextTick()
        expect(state.selected.value?.answers).toEqual([{value: "b", note: "keep this"}, {value: "", note: ""}])
        expect(state.hasNewer.value).toBe(false)
        scope.stop()
        const reloaded = setup(sources)
        expect(reloaded.state.selected.value?.answers[0]).toEqual({value: "b", note: "keep this"})
        reloaded.scope.stop()
    })

    it("dismisses completed or discarded sets across reload without recreating them", async () => {
        const {state, sources, scope} = setup()
        state.dismiss("old")
        sources.value = [{id: "old", text}]
        await nextTick()
        expect(state.sets.value).toEqual([])
        expect(createAnswerDraftStore().list(session)).toEqual([])
        scope.stop()
        const reloaded = setup()
        expect(reloaded.state.selected.value).toBeNull()
        reloaded.sources.value.push({id: "new", text: newer})
        await nextTick()
        expect(reloaded.state.selected.value?.id).toBe("new")
        reloaded.scope.stop()
    })

    it("validates restored source, answer lengths and option values before exposing panel answers", () => {
        const store = createAnswerDraftStore()
        store.save({...session, messageId: "old"}, {sourceText: text, answers: []})
        store.save({...session, messageId: "invalid"}, {sourceText: "not questions", answers: []})
        store.save({...session, messageId: "bad-choice"}, {sourceText: text, answers: [{value: "z", note: ""}]})
        const {state, scope} = setup()
        expect(state.sets.value.map(set => set.id)).toEqual(["old"])
        expect(state.selected.value?.answers).toEqual([{value: "a", note: ""}])
        scope.stop()
    })

    it("replaces a damaged draft source with authoritative transcript text", () => {
        createAnswerDraftStore().save({...session, messageId: "old"}, {
            sourceText: newer, answers: [{value: "corrupt source answer", note: ""}],
        })
        const {state, scope} = setup()
        expect(state.selected.value?.text).toBe(text)
        expect(state.selected.value?.answers).toEqual([{value: "a", note: ""}])
        scope.stop()
    })

    it("isolates identical source ids across repositories, agents and sessions", async () => {
        const identity = ref({...session})
        const sources = ref([{id: "old", text}])
        const scope = effectScope()
        const state = scope.run(() => useAnswerSets(() => identity.value, () => sources.value))!
        state.updateAnswers([{value: "b", note: "first session"}])
        identity.value = {...session, prefix: "other", agent: "other-agent", session: "other-session"}
        await nextTick()
        expect(state.selected.value?.answers).toEqual([{value: "a", note: ""}])
        state.dismiss("old")
        identity.value = {...session}
        await nextTick()
        expect(state.selected.value?.answers).toEqual([{value: "b", note: "first session"}])
        scope.stop()
    })

    it("keeps current-session drafts and dismissed sets usable when storage is blocked", async () => {
        const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw Error("blocked") })
        const put = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("blocked") })
        const remove = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw Error("blocked") })
        try {
            const {state, sources, scope} = setup()
            state.updateAnswers([{value: "b", note: "kept in memory"}])
            sources.value.push({id: "new", text: newer})
            await nextTick()
            state.select("new")
            state.select("old")
            expect(state.selected.value?.answers[0]?.note).toBe("kept in memory")
            state.dismiss("old")
            sources.value = [...sources.value]
            await nextTick()
            expect(state.sets.value.map(set => set.id)).toEqual(["new"])
            scope.stop()
        } finally { get.mockRestore(); put.mockRestore(); remove.mockRestore() }
    })

    it("follows newer questions without turning untouched recommendations into drafts", async () => {
        const {state, sources, scope} = setup()
        expect(createAnswerDraftStore().list(session)).toEqual([])
        sources.value.push({id: "new", text: newer})
        await nextTick()
        expect(state.sets.value.map(set => set.id)).toEqual(["new"])
        expect(state.selected.value?.id).toBe("new")
        expect(state.hasNewer.value).toBe(false)
        scope.stop()
        const reloaded = setup(sources)
        expect(reloaded.state.selected.value?.id).toBe("new")
        reloaded.sources.value.push({id: "plain", text: "No questions."})
        await nextTick()
        expect(reloaded.state.selected.value).toBeNull()
        reloaded.scope.stop()
    })

    it("never offers older non-drafted questions and marks an edited draft stale for a plain newer reply", async () => {
        const sources = ref([{id: "history", text}, {id: "old", text: newer}])
        const {state, scope} = setup(sources)
        expect(state.sets.value.map(set => set.id)).toEqual(["old"])
        state.updateAnswers([{value: "Platform", note: ""}])
        sources.value.push({id: "plain", text: "No questions."})
        await nextTick()
        expect(state.selected.value?.id).toBe("old")
        expect(state.hasNewer.value).toBe(true)
        scope.stop()
    })
})
