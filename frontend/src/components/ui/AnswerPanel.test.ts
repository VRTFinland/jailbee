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
    expect(wrapper.get('button[aria-label="Discard answers"]').attributes('data-variant')).toBe('danger')
    expect(wrapper.get('button[aria-label="Next step"]').attributes('data-variant')).toBe('primary')
})

it("uses directional controls to expand, reduce and hide the panel", async () => {
    const set = {questions: [{number: 1, title: "Choice", body: "Choose", options: [], recommendation: null, start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half"}})
    expect(wrapper.get('[aria-label="Expand answer panel"] svg path').attributes('d')).toContain('240-240')
    await wrapper.get('[aria-label="Expand answer panel"]').trigger('click')
    expect(wrapper.get('[aria-label="Reduce answer panel"] svg path').attributes('d')).toContain('240 240')
    expect(wrapper.get('[aria-label="Hide"] svg path').attributes('d')).not.toBe(wrapper.get('[aria-label="Reduce answer panel"] svg path').attributes('d'))
    await wrapper.get('[aria-label="Hide"]').trigger('click')
    expect(wrapper.attributes('data-height')).toBe('bar')
})

it("advances free-text Enter through questions to the summary without sending", async () => {
    const set = {questions: [1, 2].map(number => ({number, title: "Choice", body: "Choose", options: [], recommendation: number === 2 ? "yes" : null, start: 0, end: 1}))}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half"}})
    for (const options of [{shiftKey: true}, {isComposing: true}, {keyCode: 229}]) {
        const event = new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true, ...options})
        wrapper.get('textarea').element.dispatchEvent(event)
        expect(event.defaultPrevented).toBe(false)
    }
    await wrapper.get('textarea').trigger("keydown", {key: "Enter"})
    expect(wrapper.get('header').text()).toContain("Q2")
    expect(wrapper.emitted("send")).toBeUndefined()
    await wrapper.get('textarea').trigger("keydown", {key: "Enter"})
    expect(wrapper.get('h2').text()).toBe("Summary")
    expect(wrapper.emitted("send")).toBeUndefined()
    expect(wrapper.get('[aria-label="Send answers"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="Question 1"]').trigger('click')
    await wrapper.get('textarea').setValue("hello")
    await wrapper.get('textarea').trigger('keydown', {key: 'Enter'})
    await wrapper.get('textarea').trigger('keydown', {key: 'Enter'})
    await wrapper.get('[aria-label="Send answers"]').trigger('click')
    expect(wrapper.emitted("send")?.[0]).toEqual(["Q1: hello\nQ2: yes"])
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

it("requires confirmation before discarding the answer set and lets the user keep it", async () => {
    const set = {questions: [{number: 1, title: "Choice", body: "Choose", options: [], recommendation: null, start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: true, initialHeight: "half"}})
    await wrapper.get('textarea').setValue('keep this')
    await wrapper.get('[aria-label="Discard answers"]').trigger('click')
    expect(wrapper.emitted('discard')).toBeUndefined()
    await wrapper.get('[aria-label="Keep answers"]').trigger('click')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('keep this')
    expect(wrapper.find('[aria-label="Confirm discard answers"]').exists()).toBe(false)
    await wrapper.get('[aria-label="Discard answers"]').trigger('click')
    await wrapper.get('[aria-label="Confirm discard answers"]').trigger('click')
    expect(wrapper.emitted('discard')).toEqual([[]])
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
    const wrapper = mount(AnswerPanel, {props: {set, available: false, initialHeight: "half", answers: [{value: "yes", note: ""}]}})
    expect((wrapper.get('input[value="yes"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('textarea').setValue("A custom answer")
    expect(wrapper.emitted("update:answers")?.[0]).toEqual([[{value: "A custom answer", note: ""}]])
    await wrapper.get('[aria-label="Show in conversation"]').trigger("click")
    expect(wrapper.emitted("show")?.[0]).toEqual([0])
})
