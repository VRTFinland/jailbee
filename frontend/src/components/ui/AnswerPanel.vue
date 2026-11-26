<script setup lang="ts">
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue"
import Icon from "./Icon.vue"
import Button from "./Button.vue"
import IconButton from "./IconButton.vue"
import Field from "./Field.vue"
import {buildAnswerMessage, initialAnswers} from "../../questions"
import type {Answer, QuestionSet} from "../../questions"

const props = withDefaults(defineProps<{set: QuestionSet; available: boolean; busy?: boolean; answers?: Answer[]; reason?: string; error?: string; status?: string; initialHeight?: "bar" | "half" | "full"}>(), {initialHeight: "bar"})
const emit = defineEmits<{send: [text: string]; show: [index: number]; height: [value: "bar" | "half" | "full"]; "update:answers": [value: Answer[]]}>()
const localAnswers = ref(initialAnswers(props.set))
const answers = computed(() => props.answers ?? localAnswers.value)
function updateAnswer(index: number, key: keyof Answer, value: string) {
    const updated = answers.value.map((answer, i) => i === index ? {...answer, [key]: value} : {...answer})
    localAnswers.value = updated
    emit("update:answers", updated)
}
const step = ref(0)
const height = ref<"bar" | "half" | "full">(props.initialHeight)
watch(() => props.set, set => { localAnswers.value = initialAnswers(set); step.value = 0; height.value = props.initialHeight })
watch(height, value => emit("height", value))
const question = computed(() => props.set.questions[step.value])
const preview = computed(() => buildAnswerMessage(props.set, answers.value))
const root = ref<HTMLElement>()
const content = ref<HTMLElement>()
const page = ref(0)
const pages = ref(1)
const eink = ref(false)
function measurePages() {
    eink.value = root.value?.closest('[data-profile]')?.getAttribute('data-profile') === 'eink'
    const el = content.value
    if (!el || !eink.value || !el.clientHeight) { pages.value = 1; return }
    pages.value = Math.max(1, Math.ceil(el.scrollHeight / el.clientHeight))
    page.value = Math.min(page.value, pages.value - 1)
}
function turnPage(delta: number) {
    const el = content.value
    if (!el) return
    page.value = Math.max(0, Math.min(pages.value - 1, page.value + delta))
    // A discrete page jump only; wheel/touch scrolling stays disabled in e-ink.
    el.scrollTop = page.value * el.clientHeight
}
watch([step, height, () => props.set], () => {
    page.value = 0
    if (content.value) content.value.scrollTop = 0
    void nextTick(measurePages)
})
watch(preview, () => void nextTick(measurePages))
let resize: ResizeObserver | undefined
let mutation: MutationObserver | undefined
let profileObserver: MutationObserver | undefined
function syncPage() {
    const el = content.value
    if (!el || !eink.value || !el.clientHeight) return
    page.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 1
        ? pages.value - 1 : Math.floor(el.scrollTop / el.clientHeight)
}
function pageKey(event: KeyboardEvent) {
    if (!eink.value || !['PageUp', 'PageDown'].includes(event.key)) return
    event.preventDefault()
    event.stopPropagation()
    turnPage(event.key === 'PageDown' ? 1 : -1)
}
onMounted(() => {
    if (typeof ResizeObserver !== 'undefined') {
        resize = new ResizeObserver(measurePages)
        if (root.value) resize.observe(root.value)
    }
    if (typeof MutationObserver !== 'undefined' && root.value) {
        mutation = new MutationObserver(() => void nextTick(measurePages))
        mutation.observe(root.value, {childList: true, subtree: true})
        profileObserver = new MutationObserver(() => void nextTick(measurePages))
        for (let parent = root.value.parentElement; parent; parent = parent.parentElement) {
            profileObserver.observe(parent, {attributes: true, attributeFilter: ['data-profile']})
        }
    }
    measurePages()
})
onBeforeUnmount(() => { resize?.disconnect(); mutation?.disconnect(); profileObserver?.disconnect() })
</script>

<template>
    <aside ref="root" class="answer-panel" :data-height="height" aria-label="Answer questions" @keydown="pageKey">
        <header class="panel-header">
            <Icon name="question" /><strong>{{ set.questions.length }} {{ set.questions.length === 1 ? 'question' : 'questions' }}</strong>
            <Button v-if="height === 'bar'" variant="text" aria-label="Expand answers" @click="height = 'half'">Answer</Button>
            <template v-else>
                <IconButton v-if="height === 'half'" icon="expand" label="Expand answer panel" @click="height = 'full'" />
                <IconButton v-else icon="collapse" label="Reduce answer panel" @click="height = 'half'" />
                <IconButton icon="close" label="Collapse answers" @click="height = 'bar'" />
            </template>
        </header>
        <div v-if="height !== 'bar'" ref="content" class="panel-content" @scroll="syncPage" @focusin="nextTick(syncPage)">
            <slot name="sets" />
            <nav class="steps" aria-label="Answer steps">
                <button v-for="(item, index) in set.questions" :key="item.number" type="button" :aria-label="`Question ${item.number}`" :aria-current="step === index ? 'step' : undefined" @click="step = index"><span class="dot" aria-hidden="true" /></button>
                <button type="button" aria-label="Summary" :aria-current="step === set.questions.length ? 'step' : undefined" @click="step = set.questions.length"><span class="dot" aria-hidden="true" /></button>
            </nav>
            <template v-if="question">
                <h2>Q{{ question.number }}: {{ question.title }}</h2>
                <p>{{ question.body }}</p>
                <Button variant="text" @click="emit('show', step)">Show in conversation</Button>
                <fieldset v-if="question.options.length || question.recommendation === 'yes'">
                    <legend>Answer</legend>
                    <label v-if="question.recommendation === 'yes'" class="option"><input :checked="answers[step].value === 'yes'" type="radio" value="yes" :disabled="busy" :name="`answer-${question.number}`" @change="updateAnswer(step, 'value', 'yes')"> <span>Yes <strong>Recommendation</strong></span></label>
                    <label v-for="option in question.options" :key="option.letter" class="option">
                        <input :checked="answers[step].value === option.letter" type="radio" :value="option.letter" :disabled="busy" :name="`answer-${question.number}`" @change="updateAnswer(step, 'value', option.letter)">
                        <span>{{ option.letter }}) {{ option.text }} <strong v-if="question.recommendation === option.letter">Recommendation</strong></span>
                    </label>
                </fieldset>
                <Field v-if="!question.options.length" :model-value="answers[step].value" label="Answer" :disabled="busy" @update:model-value="updateAnswer(step, 'value', $event)" />
                <Field :model-value="answers[step].note" label="Optional note" :disabled="busy" @update:model-value="updateAnswer(step, 'note', $event)" />
            </template>
            <template v-else>
                <h2>Summary</h2>
                <button v-for="(item, index) in set.questions" :key="item.number" type="button" class="summary-row" @click="step = index">
                    Q{{ item.number }}: {{ answers[index].value || 'Unanswered' }} — {{ answers[index].value === item.recommendation ? 'Recommendation' : 'Changed' }}
                </button>
                <h3>Message preview</h3><pre>{{ preview }}</pre>
            </template>
            <p v-if="!available && reason" role="status">{{ reason }}</p>
            <p v-if="error" role="alert">{{ error }}</p>
            <p v-if="status" role="status">{{ status }}</p>
            <footer>
                <Button v-if="step > 0" variant="text" aria-label="Previous step" @click="step--">Back</Button>
                <Button v-if="question" variant="tonal" aria-label="Next step" @click="step++">Next</Button>
                <Button v-else variant="primary" aria-label="Send answers" :disabled="!available || busy || answers.some(answer => !answer.value.trim())" @click="emit('send', preview)">Send</Button>
            </footer>
            <slot name="draft-actions" />
        </div>
        <nav v-if="height !== 'bar' && eink && pages > 1" class="answer-pages" aria-label="Answer pages">
            <Button variant="text" aria-label="Previous answer page" :disabled="page === 0" @click="turnPage(-1)">Previous</Button>
            <span>{{ page + 1 }} / {{ pages }}</span>
            <Button variant="text" aria-label="Next answer page" :disabled="page >= pages - 1" @click="turnPage(1)">Next</Button>
        </nav>
    </aside>
</template>

<style scoped>
.answer-panel { display: flex; flex-direction: column; background: var(--md-surface-container-low); color: var(--md-on-surface); border: 1px solid var(--md-outline-variant); border-radius: var(--md-shape-medium); overflow: hidden; min-height: 0; }
.panel-header, footer, .steps { display: flex; align-items: center; gap: var(--md-space-8); }
.panel-header { flex: none; padding: var(--md-space-8); }
.panel-header > strong { margin-right: auto; }
.option input { accent-color: var(--md-primary); }
.option strong { display: inline-block; padding: 0 var(--md-space-8); border: 1px solid var(--md-outline-variant); border-radius: var(--md-shape-small); font-size: var(--md-type-label-small-size); }
.option span { overflow-wrap: anywhere; }
[data-profile="eink"] .answer-panel { border-color: var(--md-on-surface); }
.panel-content { overflow: auto; padding: var(--md-space-16); max-height: var(--md-layout-answer-half); min-height: 0; }
[data-profile="eink"] .panel-content { overflow: hidden; }
.answer-pages { flex: none; display: flex; justify-content: space-between; align-items: center; padding: var(--md-space-4); }
[data-height="full"] .panel-content { max-height: var(--md-layout-answer-full); }
.steps { flex-wrap: wrap; }
.steps button, .summary-row { min-height: var(--md-target-min); color: var(--md-on-surface); background: var(--md-surface-container-high); border: 1px solid var(--md-outline-variant); border-radius: var(--md-shape-small); padding: var(--md-space-8); cursor: pointer; }
.steps button { min-width: var(--md-target-min); border: 0; background: transparent; }
.dot { display: block; width: var(--md-space-8); height: var(--md-space-8); border-radius: var(--md-shape-full); background: var(--md-outline); }
.steps [aria-current] .dot { background: var(--md-primary); outline: 1px solid var(--md-primary); outline-offset: var(--md-space-4); }
.option, .summary-row { display: flex; width: 100%; gap: var(--md-space-8); padding: var(--md-space-12); margin-bottom: var(--md-space-8); min-height: var(--md-target-row); border: 1px solid var(--md-outline-variant); border-radius: var(--md-shape-small); }
fieldset { border: 0; padding: 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: var(--md-font-mono); }
footer { justify-content: space-between; margin-top: var(--md-space-16); }
</style>
