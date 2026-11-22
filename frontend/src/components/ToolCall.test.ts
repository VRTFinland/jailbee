import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import type {RenderedSubagent, RenderedToolCall, RenderedToolResult} from "../protocol"
import ToolCall from "./ToolCall.vue"

const call: RenderedToolCall = {
    kind: "tool_call",
    id: "r1:0",
    timestamp: null,
    call_id: "toolu_1",
    name: "Bash",
    summary: "Bash: run the tests",
    input_html: '<div class="highlight"><pre>{"command": "pytest"}</pre></div>',
    model: "claude-opus-5-5",
}

const result: RenderedToolResult = {
    kind: "tool_result",
    id: "r2:0",
    timestamp: null,
    call_id: "toolu_1",
    html: '<pre class="tool-output">42 passed</pre>',
    truncated: true,
    full_length: 12000,
    is_error: false,
}

describe("ToolCall", () => {
    it("is folded to its one-line summary", () => {
        const wrapper = mount(ToolCall, {props: {call, result}})
        const toggle = wrapper.get("button.tool-summary")
        expect(toggle.text()).toContain("Bash: run the tests")
        expect(toggle.attributes("aria-expanded")).toBe("false")
        expect(wrapper.find(".tool-input").exists()).toBe(false)
        expect(wrapper.text()).not.toContain("42 passed")
    })

    it("shows the input and the truncated result when expanded, and folds again", async () => {
        const wrapper = mount(ToolCall, {props: {call, result}})
        await wrapper.get("button.tool-summary").trigger("click")
        expect(wrapper.get("button.tool-summary").attributes("aria-expanded")).toBe("true")
        expect(wrapper.get(".tool-input").html()).toContain('"command": "pytest"')
        expect(wrapper.get(".tool-result").text()).toContain("42 passed")
        expect(wrapper.get(".tool-result").text()).toContain("12000")

        await wrapper.get("button.tool-summary").trigger("click")
        expect(wrapper.find(".tool-input").exists()).toBe(false)
    })

    it("says when the result has not arrived yet", async () => {
        const wrapper = mount(ToolCall, {props: {call, result: null}})
        await wrapper.get("button.tool-summary").trigger("click")
        expect(wrapper.text()).toContain("No result yet")
    })

    it("marks an error result on the folded line", () => {
        const wrapper = mount(ToolCall, {props: {call, result: {...result, is_error: true}}})
        expect(wrapper.get("button.tool-summary").classes()).toContain("error")
    })

    it("lists a subagent the call started", () => {
        const sub: RenderedSubagent = {
            kind: "subagent",
            id: "r2:0:agent",
            timestamp: null,
            call_id: "toolu_1",
            agent_id: "a0123456789abcdef",
            agent_type: "general-purpose",
            description: "Review it",
        }
        const wrapper = mount(ToolCall, {props: {call, result, subagents: [sub]}})
        expect(wrapper.get(".tool-subagent").text()).toContain("Review it")
    })
})
