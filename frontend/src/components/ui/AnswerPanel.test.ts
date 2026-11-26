import {expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import AnswerPanel from "./AnswerPanel.vue"

it("keeps the question title and actions in one compact header with a discard footer", () => {
    const set = {questions: [{number: 1, title: "Deploy", body: "Choose the safest route", options: [], recommendation: null, start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half"}})
    const header = wrapper.get('header')
    expect(header.text()).toContain('Q1 · Deploy')
    expect(header.text()).toContain('Choose the safest route')
    expect(header.find('[aria-label="Answer steps"]').exists()).toBe(true)
    expect(header.find('[aria-label="Show in conversation"]').exists()).toBe(true)
    expect(wrapper.get('button[aria-label="Discard answer"]').attributes('data-variant')).toBe('danger')
    expect(wrapper.get('button[aria-label="Next step"]').attributes('data-variant')).toBe('primary')
})

it("uses directional controls to expand, reduce and hide the panel", async () => {
    const set = {questions: [{number: 1, title: "Choice", body: "Choose", options: [], recommendation: null, start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half"}})
    expect(wrapper.get('[aria-label="Expand answer panel"] svg path').attributes('d')).toContain('240-240')
    await wrapper.get('[aria-label="Expand answer panel"]').trigger('click')
    expect(wrapper.get('[aria-label="Reduce answer panel"] svg path').attributes('d')).toContain('240 240')
    await wrapper.get('[aria-label="Hide"]').trigger('click')
    expect(wrapper.attributes('data-height')).toBe('bar')
})

it("sends a completed free-text answer on Enter, but not during composition", async () => {
    const set = {questions: [{number: 1, title: "Choice", body: "Choose", options: [], recommendation: null, start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half", answers: [{value: "hello", note: ""}]}})
    const input = wrapper.get('textarea')
    await input.trigger("keydown", {key: "Enter", isComposing: true})
    expect(wrapper.emitted("send")).toBeUndefined()
    await input.trigger("keydown", {key: "Enter"})
    expect(wrapper.emitted("send")?.[0]).toEqual(["Q1: hello"])
})

it("keeps an answer when collapsed and offers send only in the summary", async () => {
    const wrapper = mount(AnswerPanel, {props: {set: {questions: [{number: 1, title: "Choice", body: "Choose", options: [{letter: "a", text: "First"}, {letter: "b", text: "Second"}], recommendation: "a", start: 0, end: 1}]}, available: true}})
    expect(wrapper.text()).toContain("1 question")
    await wrapper.get('button[aria-label="Expand answers"]').trigger("click")
    expect(wrapper.find('button[aria-label="Send answers"]').exists()).toBe(false)
    await wrapper.get('input[value="b"]').setValue()
    await wrapper.get('button[aria-label="Hide"]').trigger("click")
    await wrapper.get('button[aria-label="Expand answers"]').trigger("click")
    expect((wrapper.get('input[value="b"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('button[aria-label="Next step"]').trigger("click")
    expect(wrapper.text()).toContain("Q1: b")
    await wrapper.get('button[aria-label="Send answers"]').trigger("click")
    expect(wrapper.emitted("send")?.[0]).toEqual(["Q1: b"])
})

it("shows summary labels only for answered questions with recommendations", async () => {
    const questions = [1, 2, 3, 4].map(number => ({number, title: "Choice", body: "Choose", options: [], recommendation: number === 2 ? "" : "yes", start: 0, end: 1}))
    const wrapper = mount(AnswerPanel, {props: {set: {questions}, available: true, initialHeight: "half", answers: [{value: "", note: ""}, {value: "custom", note: ""}, {value: "yes", note: ""}, {value: "no", note: ""}]}})
    await wrapper.get('button[aria-label="Summary"]').trigger("click")
    const rows = wrapper.findAll('.summary-row').map(row => row.text())
    expect(rows).toEqual(["Q1: Unanswered", "Q2: custom", "Q3: yes — Recommendation", "Q4: no — Changed"])
})

it("offers yes and open answers and reports external draft changes", async () => {
    const set = {questions: [{number: 2, title: "Open", body: "Explain", options: [], recommendation: "yes", start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: false, reason: "No live agent", initialHeight: "half", answers: [{value: "yes", note: ""}]}})
    expect((wrapper.get('input[value="yes"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('textarea').setValue("A custom answer")
    expect(wrapper.emitted("update:answers")?.[0]).toEqual([[{value: "A custom answer", note: ""}]])
    await wrapper.get('[aria-label="Show in conversation"]').trigger("click")
    expect(wrapper.emitted("show")?.[0]).toEqual([0])
})
