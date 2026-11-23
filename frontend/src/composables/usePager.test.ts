import {enableAutoUnmount, mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {defineComponent, h} from "vue"
import PageNav from "../components/PageNav.vue"
import {reloadSettings, updateSettings} from "../settings"
import {usePager} from "./usePager"

enableAutoUnmount(afterEach)

// Layout is faked: a viewport of 588px leaves 500px per page (88px reserved
// for the page-turn bar and the bottom guard), and 1750px of content is 4 pages.
const VIEWPORT = 588
const PAGE = 500

function fakeLayout(contentHeight: number, viewport = VIEWPORT, guardHeight = 32) {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(viewport)
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
        return {top: this.classList.contains("page-nav") ? viewport - 56 - guardHeight : 0} as DOMRect
    })
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(contentHeight)
    return vi.spyOn(window, "scrollTo").mockImplementation(() => {})
}

const Harness = defineComponent({
    setup(_, {expose}) {
        const pager = usePager()
        expose({pager})
        return () => h(PageNav, {pager})
    },
})

describe("page turns", () => {
    beforeEach(() => {
        vi.stubGlobal("matchMedia", () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
        reloadSettings()
    })
    afterEach(() => updateSettings({displayProfile: "normal"}))

    it("renders no page controls in the normal profile", () => {
        fakeLayout(1750)
        const wrapper = mount(Harness, {attachTo: document.body})
        expect(wrapper.find(".page-nav").exists()).toBe(false)
    })

    it("shows a page indicator and turns pages in the e-ink profile", async () => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")

        await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 2 / 4")
        expect(scrollTo).toHaveBeenLastCalledWith(0, PAGE)

        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 0)
    })

    it.each([32, 60])("never skips content with a tall sticky header on a short viewport (guard %ipx)", async (guardHeight) => {
        const scrollTo = fakeLayout(900, 320, guardHeight)
        updateSettings({displayProfile: "eink"})
        const ShortViewport = defineComponent({
            setup() {
                const pager = usePager(() => 200)
                return () => h(PageNav, {pager})
            },
        })
        const wrapper = mount(ShortViewport, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, guardHeight === 32 ? 32 : 4)
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, guardHeight === 32 ? 64 : 8)
        wrapper.unmount()
    })

    it("keeps the next turn within readable content after the bottom guard shrinks", async () => {
        const scrollTo = fakeLayout(900, 320, 60)
        let boundary = 204
        vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
            return {top: this.classList.contains("page-nav") ? boundary : 0} as DOMRect
        })
        let y = 0
        vi.spyOn(window, "scrollY", "get").mockImplementation(() => y)
        scrollTo.mockImplementation((_x, top) => {
            y = top ?? 0
            window.dispatchEvent(new Event("scroll"))
        })
        updateSettings({displayProfile: "eink"})
        const ShortViewport = defineComponent({
            setup() {
                const pager = usePager(() => 200)
                return () => h(PageNav, {pager})
            },
        })
        const wrapper = mount(ShortViewport, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        for (let i = 0; i < 10; i++) await wrapper.get("button.next").trigger("click")
        expect(y).toBe(40)
        boundary = 232
        await wrapper.get("button.next").trigger("click")
        expect(y).toBe(64)
    })

    it("stops at the first and last page", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        expect(wrapper.get("button.prev").attributes("disabled")).toBeDefined()
        for (let i = 0; i < 6; i++) await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 4 / 4")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("turns pages from the edge tap zones", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.get(".tap-zone.forward").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 2 / 4")
        await wrapper.get(".tap-zone.back").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")
    })

    it("keeps Next enabled after a manual scroll that has not reached the bottom", async () => {
        // 1388px of content in a 588px viewport: the last reachable scroll is 800px (1.6 pages).
        fakeLayout(1388)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        vi.spyOn(window, "scrollY", "get").mockReturnValue(750)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("Page 2 / 3")
        expect(wrapper.get("button.next").attributes("disabled")).toBeUndefined()
    })

    it("follows the scroll position, e.g. after a keyboard or system scroll", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        vi.spyOn(window, "scrollY", "get").mockReturnValue(PAGE * 2)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("Page 3 / 4")
    })
})
