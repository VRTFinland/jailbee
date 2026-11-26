import {computed, ref, watch} from "vue"
import {createAnswerDraftStore, createCompletionHistory, type DraftSession} from "../answerDrafts"
import {initialAnswers, parseQuestionSet, type Answer, type QuestionSet} from "../questions"

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
    const history = createCompletionHistory()
    const sets = ref<SessionAnswerSet[]>([])
    const selectedId = ref<string | null>(null)
    const dismissed = new Set<string>()
    const edited = new Set<string>()
    let current = session()
    let stateKey = ""
    const selected = computed(() => sets.value.find(set => set.id === selectedId.value) ?? null)
    const latestId = computed(() => sources().at(-1)?.id ?? null)
    const hasNewer = computed(() => !!selected.value && latestId.value !== null && selected.value.id !== latestId.value)
    function rememberSelection() { history.select(stateKey, selectedId.value) }
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
        stateKey = key
        dismissed.clear()
        edited.clear()
        history.read(key).forEach(id => dismissed.add(id))
        sets.value = []
        for (const draft of store.list(current)) {
            const set = parseQuestionSet(draft.sourceText)
            if (!set || !validAnswers(set, draft.answers) || dismissed.has(draft.messageId)) {
                store.discard({...current, messageId: draft.messageId})
                continue
            }
            sets.value.push({id: draft.messageId, text: draft.sourceText, set, answers: draft.answers})
            edited.add(draft.messageId)
        }
        const remembered = history.selection(stateKey)
        selectedId.value = sets.value.find(set => set.id === remembered)?.id ?? sets.value[0]?.id ?? null
    }, {immediate: true, flush: "sync"})
    watch([() => sources().map(source => ({...source})), () => JSON.stringify(session())], () => {
        // The live source is authoritative if a damaged draft claims its id.
        for (const item of [...sets.value]) {
            const source = sources().find(source => source.id === item.id)
            if (source && source.text !== item.text) {
                // One API message can append text records while its answers are edited.
                const extended = source.text.startsWith(item.text) ? parseQuestionSet(source.text) : null
                if (extended) {
                    const answers = initialAnswers(extended)
                    for (const [index, question] of extended.questions.entries()) {
                        const previous = item.set.questions.findIndex(old => old.number === question.number)
                        const answer = item.answers[previous]
                        if (answer && validAnswers({questions: [question]}, [answer])) answers[index] = {...answer}
                    }
                    item.text = source.text
                    item.set = extended
                    item.answers = answers
                    if (edited.has(item.id)) save(item)
                } else if (!source.text.startsWith(item.text)) {
                    store.discard({...current, messageId: item.id})
                    edited.delete(item.id)
                    sets.value = sets.value.filter(set => set.id !== item.id)
                }
            }
        }
        const latest = sources().at(-1)
        sets.value = sets.value.filter(item => edited.has(item.id) || item.id === latest?.id)
        if (latest && !dismissed.has(latest.id) && !sets.value.some(set => set.id === latest.id)) {
            const set = parseQuestionSet(latest.text)
            if (set) {
                const item = {...latest, set, answers: initialAnswers(set)}
                sets.value.push(item)
            }
        }
        if (!sets.value.some(set => set.id === selectedId.value)) {
            selectedId.value = sets.value.find(set => set.id === latest?.id)?.id ?? sets.value[0]?.id ?? null
        }
        rememberSelection()
    }, {immediate: true})
    function updateAnswers(answers: Answer[]) {
        const item = selected.value
        if (!item || !validAnswers(item.set, answers)) return
        item.answers = answers.map(answer => ({...answer}))
        edited.add(item.id)
        save(item)
    }
    /** Used for both deliberate discard and a send accepted by the shared gate. */
    function dismiss(id: string) {
        dismissed.add(id)
        edited.delete(id)
        const retained = history.write(stateKey, [...dismissed])
        dismissed.clear()
        retained.forEach(id => dismissed.add(id))
        store.discard({...current, messageId: id})
        sets.value = sets.value.filter(set => set.id !== id)
        if (selectedId.value === id) selectedId.value = sets.value[0]?.id ?? null
        rememberSelection()
    }
    return {sets, selected, hasNewer, select, updateAnswers, dismiss}
}
