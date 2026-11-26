import {expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import AnswerPanel from "./AnswerPanel.vue"

it("keeps an answer when collapsed and offers send only in the summary", async () => {
    const wrapper = mount(AnswerPanel, {props: {set: {questions: [{number: 1, title: "Choice", body: "Choose", options: [{letter: "a", text: "First"}, {letter: "b", text: "Second"}], recommendation: "a", start: 0, end: 1}]}, available: true}})
    expect(wrapper.text()).toContain("1 question")
    await wrapper.get('button[aria-label="Expand answers"]').trigger("click")
    expect(wrapper.find('button[aria-label="Send answers"]').exists()).toBe(false)
    await wrapper.get('input[value="b"]').setValue()
    await wrapper.get('button[aria-label="Collapse answers"]').trigger("click")
    await wrapper.get('button[aria-label="Expand answers"]').trigger("click")
    expect((wrapper.get('input[value="b"]').element as HTMLInputElement).checked).toBe(true)
    await wrapper.get('button[aria-label="Next step"]').trigger("click")
    expect(wrapper.text()).toContain("Q1: b")
    await wrapper.get('button[aria-label="Send answers"]').trigger("click")
    expect(wrapper.emitted("send")?.[0]).toEqual(["Q1: b"])
})

it("offers yes and open answers and reports external draft changes", async () => {
    const set = {questions: [{number: 2, title: "Open", body: "Explain", options: [], recommendation: "yes", start: 0, end: 1}]}
    const wrapper = mount(AnswerPanel, {props: {set, available: false, reason: "No live agent", initialHeight: "half", answers: [{value: "yes", note: ""}]}})
    expect((wrapper.get('input[value="yes"]').element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.text()).toContain("No live agent")
    await wrapper.get('input[type="text"]').setValue("A custom answer")
    expect(wrapper.emitted("update:answers")?.[0]).toEqual([[{value: "A custom answer", note: ""}]])
    await wrapper.findAll("button").find(button => button.text() === "Show in conversation")!.trigger("click")
    expect(wrapper.emitted("show")?.[0]).toEqual([0])
})
