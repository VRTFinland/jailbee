import {describe, expect, it} from "vitest"
import {mount} from "@vue/test-utils"
import Icon from "./Icon.vue"

describe("Icon", () => {
    it("hides a decorative icon from assistive technology", () => {
        const wrapper = mount(Icon, {props: {name: "settings"}})
        expect(wrapper.attributes("aria-hidden")).toBe("true")
        expect(wrapper.attributes("role")).toBeUndefined()
        expect(wrapper.attributes("aria-label")).toBeUndefined()
        expect(wrapper.attributes("focusable")).toBe("false")
    })

    it("exposes a labelled icon as an image to assistive technology", () => {
        const wrapper = mount(Icon, {props: {name: "settings", label: "Settings"}})
        expect(wrapper.attributes("role")).toBe("img")
        expect(wrapper.attributes("aria-label")).toBe("Settings")
        expect(wrapper.attributes("aria-hidden")).toBeUndefined()
        expect(wrapper.attributes("focusable")).toBe("false")
    })
})
