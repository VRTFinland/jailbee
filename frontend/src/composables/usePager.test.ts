import {mount} from "@vue/test-utils"
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest"
import {defineComponent, h} from "vue"
import PageNav from "../components/PageNav.vue"
import {reloadSettings, updateSettings} from "../settings"
import {usePager} from "./usePager"

// Layout is faked: a viewport of 588px leaves 500px per page (88px reserved
// for the page-turn bar and the bottom guard), and 1750px of content is 4 pages.
const VIEWPORT = 588
const PAGE = 500

function fakeLayout(contentHeight: number) {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(VIEWPORT)
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
        const wrapper = mount(Harness)
        expect(wrapper.find(".page-nav").exists()).toBe(false)
    })

    it("shows a page indicator and turns pages in the e-ink profile", async () => {
        const scrollTo = fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness)
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")

        await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 2 / 4")
        expect(scrollTo).toHaveBeenLastCalledWith(0, PAGE)

        await wrapper.get("button.prev").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")
        expect(scrollTo).toHaveBeenLastCalledWith(0, 0)
    })

    it("stops at the first and last page", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness)
        expect(wrapper.get("button.prev").attributes("disabled")).toBeDefined()
        for (let i = 0; i < 6; i++) await wrapper.get("button.next").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 4 / 4")
        expect(wrapper.get("button.next").attributes("disabled")).toBeDefined()
    })

    it("turns pages from the edge tap zones", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness)
        await wrapper.get(".tap-zone.forward").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 2 / 4")
        await wrapper.get(".tap-zone.back").trigger("click")
        expect(wrapper.get(".page-indicator").text()).toBe("Page 1 / 4")
    })

    it("follows the scroll position, e.g. after a keyboard or system scroll", async () => {
        fakeLayout(1750)
        updateSettings({displayProfile: "eink"})
        const wrapper = mount(Harness)
        vi.spyOn(window, "scrollY", "get").mockReturnValue(PAGE * 2)
        window.dispatchEvent(new Event("scroll"))
        await wrapper.vm.$nextTick()
        expect(wrapper.get(".page-indicator").text()).toBe("Page 3 / 4")
    })
})
