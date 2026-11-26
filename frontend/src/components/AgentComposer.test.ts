import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import AgentComposer from "./AgentComposer.vue"

describe("agent composer", () => {
    it("emits the intact message and exposes sending state", async () => {
        const w = mount(AgentComposer, {props: {available: true}})
        await w.get("textarea").setValue("first\nsecond")
        await w.get('[aria-label="Send message"]').trigger("click")
        expect(w.emitted("send")?.[0]).toEqual(["first\nsecond"])
        await w.setProps({sending: true})
        expect(w.get('[aria-label="Send message"]').attributes("disabled")).toBeDefined()
    })
    it("locks the draft until the transcript confirms delivery", async () => {
        const w = mount(AgentComposer, {props: {available: true}})
        await w.get("textarea").setValue("first draft")
        await w.setProps({awaiting: true, status: "Waiting for transcript"})
        expect(w.get("textarea").attributes("disabled")).toBeDefined()
        expect(w.get('[aria-label="Send message"]').attributes("disabled")).toBeDefined()
        await w.get('[aria-label="Send message"]').trigger("click")
        expect(w.emitted("send")).toBeUndefined()
        await w.setProps({awaiting: false, status: "Delivered"})
        expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("")
    })
    it("retains text on refusal and explains unavailable chat", async () => {
        const w = mount(AgentComposer, {props: {available: true}})
        await w.get("textarea").setValue("keep me")
        await w.setProps({error: "No pane"})
        expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("keep me")
        expect(w.text()).toContain("No pane")
        await w.setProps({available: false, reason: "Not permitted"})
        expect(w.get("textarea").attributes("disabled")).toBeDefined()
        expect(w.text()).toContain("Not permitted")
    })
})
