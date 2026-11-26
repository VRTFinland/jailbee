import {mount} from "@vue/test-utils"
import {describe, expect, it} from "vitest"
import {createMemoryHistory} from "vue-router"
import {createAppRouter} from "../router"
import {orchestrationView} from "../test-fixtures"
import RepoList from "./RepoList.vue"

// Phase appearance belongs to StatusChip now, not a view's CSS. Render the
// wire values through the list; Chrome checks the actual monochrome ink.
describe("row phase presentation", () => {
    it.each(["booting", "autostart", "job", "failed"])("words %s with its own icon even beside a waiting agent", (phase) => {
        const view = orchestrationView()
        view.repos[0].containers = [{...view.repos[0].containers[0], phase, agent: "waiting"}]
        const wrapper = mount(RepoList, {props: {view}, global: {plugins: [createAppRouter(createMemoryHistory())]}})
        const row = wrapper.get("li")
        expect(row.get(".phase").text()).toBe(phase)
        expect(row.get(".phase svg path").attributes("d")).not.toBe(row.get(".agent svg path").attributes("d"))
        expect(row.get(".agent").text()).toBe("waiting for you")
        wrapper.unmount()
    })

    it("words Stopped with a vector rather than relying on a faded row", () => {
        const view = orchestrationView()
        view.repos[0].containers = [{...view.repos[0].containers[0], phase: null, state: "Stopped", agent: null}]
        const wrapper = mount(RepoList, {props: {view}, global: {plugins: [createAppRouter(createMemoryHistory())]}})
        expect(wrapper.get(".phase").text()).toBe("Stopped")
        expect(wrapper.get(".phase svg path").attributes("d")).toBeTruthy()
        wrapper.unmount()
    })
})
