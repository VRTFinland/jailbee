import {computed, ref, watch} from "vue"
import {createAnswerDraftStore, type DraftSession} from "../answerDrafts"
import {initialAnswers, parseQuestionSet, type Answer, type QuestionSet} from "../questions"
import {readItem, writeItem} from "../storage"

export interface AnswerSource {id: string; text: string}
export interface SessionAnswerSet extends AnswerSource {set: QuestionSet; answers: Answer[]}

function validAnswers(set: QuestionSet, answers: Answer[]): boolean {
    return answers.length === set.questions.length && answers.every((answer, index) => {
        const question = set.questions[index]!
        return answer.value === "" || !question.options.length ||
            question.options.some(option => option.letter === answer.value) ||
            (question.recommendation === "yes" && answer.value === "yes")
    })
}

/** Draft lifetime is independent of the displayed transcript (including e-ink pending entries). */
export function useAnswerSets(session: () => DraftSession, sources: () => AnswerSource[]) {
    const store = createAnswerDraftStore()
    const sets = ref<SessionAnswerSet[]>([])
    const selectedId = ref<string | null>(null)
    const dismissed = new Set<string>()
    let current = session()
    let stateKey = ""
    const selected = computed(() => sets.value.find(set => set.id === selectedId.value) ?? null)
    const latestId = computed(() => sources().at(-1)?.id ?? null)
    const hasNewer = computed(() => !!selected.value && latestId.value !== null && selected.value.id !== latestId.value)
    function rememberSelection() { writeItem(stateKey + ":selected", selectedId.value ?? "") }
    function save(item: SessionAnswerSet) {
        store.save({...current, messageId: item.id}, {sourceText: item.text, answers: item.answers})
    }
    function select(id: string) {
        if (!sets.value.some(set => set.id === id)) return
        selectedId.value = id
        rememberSelection()
    }
    watch(() => JSON.stringify([session().prefix, session().agent, session().session]), key => {
        current = {...session()}
        stateKey = "jailbee:answer-completed:" + key
        dismissed.clear()
        try {
            const stored: unknown = JSON.parse(readItem(stateKey) ?? "[]")
            if (Array.isArray(stored)) stored.forEach(id => { if (typeof id === "string") dismissed.add(id) })
        } catch { /* Corrupt completion history does not block the composer. */ }
        sets.value = []
        for (const draft of store.list(current)) {
            const set = parseQuestionSet(draft.sourceText)
            if (!set || !validAnswers(set, draft.answers) || dismissed.has(draft.messageId)) {
                store.discard({...current, messageId: draft.messageId})
                continue
            }
            sets.value.push({id: draft.messageId, text: draft.sourceText, set, answers: draft.answers})
        }
        const remembered = readItem(stateKey + ":selected")
        selectedId.value = sets.value.find(set => set.id === remembered)?.id ?? sets.value[0]?.id ?? null
    }, {immediate: true, flush: "sync"})
    watch([() => sources().map(source => ({...source})), () => JSON.stringify(session())], () => {
        // The live source is authoritative if a damaged draft claims its id.
        for (const item of [...sets.value]) {
            const source = sources().find(source => source.id === item.id)
            if (source && source.text !== item.text) {
                store.discard({...current, messageId: item.id})
                sets.value = sets.value.filter(set => set.id !== item.id)
            }
        }
        const latest = sources().at(-1)
        if (latest && !dismissed.has(latest.id) && !sets.value.some(set => set.id === latest.id)) {
            const set = parseQuestionSet(latest.text)
            if (set) {
                const item = {...latest, set, answers: initialAnswers(set)}
                sets.value.push(item)
                save(item)
            }
        }
        if (!sets.value.some(set => set.id === selectedId.value)) selectedId.value = sets.value[0]?.id ?? null
        rememberSelection()
    }, {immediate: true})
    function updateAnswers(answers: Answer[]) {
        const item = selected.value
        if (!item || !validAnswers(item.set, answers)) return
        item.answers = answers.map(answer => ({...answer}))
        save(item)
    }
    /** Used for both deliberate discard and a send accepted by the shared gate. */
    function dismiss(id: string) {
        dismissed.add(id)
        writeItem(stateKey, JSON.stringify([...dismissed]))
        store.discard({...current, messageId: id})
        sets.value = sets.value.filter(set => set.id !== id)
        if (selectedId.value === id) selectedId.value = sets.value[0]?.id ?? null
        rememberSelection()
    }
    return {sets, selected, hasNewer, select, updateAnswers, dismiss}
}
