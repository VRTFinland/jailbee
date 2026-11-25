import {enableAutoUnmount, mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {defineComponent, h} from "vue"
import PageNav from "../components/PageNav.vue"
import {reloadSettings, updateSettings} from "../settings"
import {usePager, type Pager} from "./usePager"

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

const SpacerHarness = defineComponent({
    setup(_, {expose}) {
        const pager = usePager(() => 0, () => 500)
        expose({pager})
        return () => h(PageNav, {pager})
    },
})

describe("page turns", () => {
    beforeEach(() => {
        vi.stubGlobal("matchMedia", () => ({matches: false, addEventListener() {}, removeEventListener() {}}))
        reloadSettings()
        updateSettings({pageControls: "bar"})
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
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 4")

        await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("2 / 4")
        expect(scrollTo).toHaveBeenLastCalledWith(0, PAGE)

        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 4")
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

    it.each([
        [100, 500, 4], [90, 450, 4], [75, 375, 5], [66, 330, 5], [50, 250, 6],
    ] as const)("uses %i%% steps without changing the content end", async (pageStep, distance, count) => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink", pageStep})
        const wrapper = mount(Harness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        expect(pager.pageCount.value).toBe(count)
        pager.next()
        expect(scrollTo).toHaveBeenLastCalledWith(0, distance)
        pager.next()
        expect(scrollTo).toHaveBeenLastCalledWith(0, distance * 2)
        pager.end()
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1162)
        expect(pager.page.value).toBe(count - 1)
        wrapper.unmount()
    })

    it.each([
        [100, 4, 8], [90, 3, 6], [75, 3, 6], [66, 2, 4], [50, 2, 4],
    ] as const)("never skips a 4px landscape readable area at %i%%", async (pageStep, first, second) => {
        const scrollTo = fakeLayout(900, 320, 60)
        updateSettings({displayProfile: "eink", pageStep})
        const Landscape = defineComponent({
            setup() { const pager = usePager(() => 200); return () => h(PageNav, {pager}) },
        })
        const wrapper = mount(Landscape, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, first)
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, second)
        wrapper.unmount()
    })

    it.each([100, 90, 75, 66, 50] as const)("has one page for short content at %i%%", (pageStep) => {
        fakeLayout(300)
        updateSettings({displayProfile: "eink", pageStep})
        const wrapper = mount(Harness, {attachTo: document.body})
        expect((wrapper.vm as unknown as {pager: Pager}).pager.pageCount.value).toBe(1)
        wrapper.unmount()
    })

    it.each([
        [100, 600, 3], [90, 650, 3], [75, 725, 3], [66, 770, 4], [50, 850, 5],
    ] as const)("anchors jumps and the last content page at %i%%", async (pageStep, previous, index) => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink", pageStep})
        const wrapper = mount(SpacerHarness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        pager.goToElement({getBoundingClientRect: () => ({top: 1100}) as DOMRect} as Element)
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1100)
        expect(pager.page.value).toBe(index)
        pager.prev()
        expect(scrollTo).toHaveBeenLastCalledWith(0, previous)
        pager.end()
        expect(scrollTo).toHaveBeenLastCalledWith(0, 750)
        expect(pager.page.value).toBe(pager.pageCount.value - 1)
        wrapper.unmount()
    })

    it("keeps a single page when a one-pixel step has no scroll range", () => {
        fakeLayout(100, 320, 60)
        updateSettings({displayProfile: "eink", pageStep: 50})
        const Tiny = defineComponent({
            setup(_, {expose}) {
                const pager = usePager(() => 203)
                expose({pager})
                return () => h(PageNav, {pager})
            },
        })
        const wrapper = mount(Tiny, {attachTo: document.body})
        expect((wrapper.vm as unknown as {pager: Pager}).pager.pageCount.value).toBe(1)
    })

    it("updates the page grid immediately when the step changes", () => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        updateSettings({pageStep: 50})
        expect(pager.pageCount.value).toBe(6)
        pager.next()
        expect(scrollTo).toHaveBeenLastCalledWith(0, 250)
    })

    it("marks the previous bottom after an overlapping turn until the next turn or manual scroll", () => {
        const scrollTo = fakeLayout(1750)
        const y = clampScrolls(scrollTo, 1162)
        updateSettings({displayProfile: "eink", pageStep: 75})
        const wrapper = mount(Harness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        expect(pager.guideTop.value).toBeNull()
        pager.next()
        expect(y()).toBe(375)
        expect(pager.guideTop.value).toBe(125)
        pager.next()
        expect(pager.guideTop.value).toBeNull()
        pager.next()
        expect(pager.guideTop.value).toBe(125)
        window.dispatchEvent(new Event("scroll"))
        expect(pager.guideTop.value).toBeNull()
        pager.prev()
        pager.next()
        expect(pager.guideTop.value).not.toBeNull()
        pager.prev()
        expect(pager.guideTop.value).toBeNull()
        pager.next()
        pager.end()
        expect(pager.guideTop.value).toBeNull()
    })

    it("draws no guide for a full-height turn", () => {
        clampScrolls(fakeLayout(1750), 1162)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        pager.next()
        expect(pager.guideTop.value).toBeNull()
    })

    it.each(["m", "PageDown"])("turns forward with %s without native scrolling", (key) => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink", pageStep: 75})
        mount(Harness, {attachTo: document.body})
        const event = new KeyboardEvent("keydown", {key, cancelable: true})
        window.dispatchEvent(event)
        expect(scrollTo).toHaveBeenLastCalledWith(0, 375)
        expect(event.defaultPrevented).toBe(true)
        const back = new KeyboardEvent("keydown", {key: key === "m" ? "n" : "PageUp", cancelable: true})
        window.dispatchEvent(back)
        expect(scrollTo).toHaveBeenLastCalledWith(0, 0)
        expect(back.defaultPrevented).toBe(true)
    })

    it("leaves normal scrolling, form controls and open overlays to their own keys", () => {
        const scrollTo = fakeLayout(1750)
        mount(Harness, {attachTo: document.body})
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "m"}))
        expect(scrollTo).not.toHaveBeenCalled()
        updateSettings({displayProfile: "eink"})
        const input = document.createElement("input")
        document.body.appendChild(input)
        input.dispatchEvent(new KeyboardEvent("keydown", {key: "m", bubbles: true}))
        const dialog = document.createElement("div")
        dialog.setAttribute("role", "dialog")
        document.body.appendChild(dialog)
        window.dispatchEvent(new KeyboardEvent("keydown", {key: "m"}))
        expect(scrollTo).not.toHaveBeenCalled()
        input.remove()
        dialog.remove()
    })

    it("stops at the first and last page", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        expect(wrapper.get("button.prev").attributes("disabled")).toBeDefined()
        for (let i = 0; i < 6; i++) await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("4 / 4")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("jumps to the last content page without the trailing answer spacer", async () => {
        const scrollTo = fakeLayout(2250)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(SpacerHarness, {attachTo: document.body})
        const pager = (wrapper.vm as unknown as {pager: Pager}).pager
        pager.end()
        await wrapper.vm.$nextTick()
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1250)
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("turns pages from the edge tap zones", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.get(".tap-zone.forward").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("2 / 4")
        await wrapper.get(".tap-zone.back").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 4")
    })

    it("keeps Next enabled after a manual scroll that has not reached the bottom", async () => {
        // 1388px of content in a 588px viewport: the last reachable scroll is 800px (1.6 pages).
        fakeLayout(1388)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        vi.spyOn(window, "scrollY", "get").mockReturnValue(750)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("2 / 3")
        expect(wrapper.get("button.next").attributes("disabled")).toBeUndefined()
    })

    it("does not skip unread content after a partial keyboard scroll", async () => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        vi.spyOn(window, "scrollY", "get").mockReturnValue(300)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 4")
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 500)
    })

    it("keeps page turns advancing when the browser rounds fractional scroll offsets", async () => {
        const scrollTo = fakeLayout(1750, 588.4)
        let y = 0
        vi.spyOn(window, "scrollY", "get").mockImplementation(() => y)
        scrollTo.mockImplementation((_x, top) => {
            y = Math.round(top ?? 0)
            window.dispatchEvent(new Event("scroll"))
        })
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        await wrapper.get("button.next").trigger("click")
        await wrapper.get("button.next").trigger("click")
        expect(y).toBe(1000)
        expect(wrapper.get(".page-indicator").text()).toBe("3 / 4")
    })

    it("ends page turns where the content does, before trailing blank space", async () => {
        // 1750px document whose last 500px are a spacer: the content ends at 1250px,
        // readable once scrolled to 1250 - 500 = 750px.
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(SpacerHarness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 3")
        await wrapper.get("button.next").trigger("click")
        await wrapper.get("button.next").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 750)
        expect(wrapper.get(".page-indicator").text()).toBe("3 / 3")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("still jumps into trailing blank space to start a page at an element", async () => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(SpacerHarness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        const el = {getBoundingClientRect: () => ({top: 1100}) as DOMRect} as Element
        ;(wrapper.vm as unknown as {pager: Pager}).pager.goToElement(el)
        await wrapper.vm.$nextTick()
        expect(scrollTo).toHaveBeenLastCalledWith(0, 1100)
        expect(wrapper.get(".page-indicator").text()).toBe("4 / 4")
        await wrapper.get("button.prev").trigger("click")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 600)
    })

    // The browser clamps a scroll to the reachable range and reports it back.
    function clampScrolls(scrollTo: ReturnType<typeof fakeLayout>, maxScroll: number) {
        let y = 0
        vi.spyOn(window, "scrollY", "get").mockImplementation(() => y)
        scrollTo.mockImplementation((_x, top) => {
            y = Math.min(top ?? 0, maxScroll)
            window.dispatchEvent(new Event("scroll"))
        })
        return () => y
    }

    it("ends page turns at the reachable bottom without trailing space", async () => {
        // 1550px of content in a 588px viewport: the browser stops at 962px, short
        // of where the content's end would meet the bar (1050px).
        const y = clampScrolls(fakeLayout(1550), 1550 - VIEWPORT)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 3")
        for (let i = 0; i < 2; i++) await wrapper.get("button.next").trigger("click")
        expect(y()).toBe(962)
        expect(wrapper.get(".page-indicator").text()).toBe("3 / 3")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("counts a fractional real bottom as the last page", async () => {
        // The computed bottom is 1162px; a fractional viewport stops the browser at 1161.5px.
        const y = clampScrolls(fakeLayout(1750), 1161.5)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        for (let i = 0; i < 3; i++) await wrapper.get("button.next").trigger("click")
        expect(y()).toBe(1161.5)
        expect(wrapper.get(".page-indicator").text()).toBe("4 / 4")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("adds no extra page for content ending under a pixel past a page boundary", async () => {
        // The bar's top is at 500.6px, so pages are 500px; the content (2001px
        // document, 500px spacer) is readable from 1000.4px: still three pages.
        const scrollTo = fakeLayout(2001, 588.6)
        clampScrolls(scrollTo, 2001 - 588.6)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(SpacerHarness, {attachTo: document.body})
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("1 / 3")
        for (let i = 0; i < 2; i++) await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("3 / 3")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("follows the scroll position, e.g. after a keyboard or system scroll", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness, {attachTo: document.body})
        vi.spyOn(window, "scrollY", "get").mockReturnValue(PAGE * 2)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("3 / 4")
    })
})
