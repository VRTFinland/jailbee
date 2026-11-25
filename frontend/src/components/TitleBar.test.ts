import {flushPromises, mount} from "@vue/test-utils"
import {afterEach, describe, expect, it} from "vitest"
import {createMemoryHistory, createRouter} from "vue-router"
import TitleBar from "./TitleBar.vue"
import {reloadSettings, updateSettings} from "../settings"

const mounted: {unmount(): void}[] = []
afterEach(() => { for (const wrapper of mounted.splice(0)) wrapper.unmount() })

async function titleBar() {
    const router = createRouter({history: createMemoryHistory(), routes: [
        {path: "/", component: {template: "<div />"}},
        {path: "/repo", component: {template: "<div />"}},
        {path: "/parent", component: {template: "<div />"}},
    ]})
    await router.push("/")
    const wrapper = mount(TitleBar, {props: {
        title: "Subagent",
        crumbs: [{label: "JailBee", to: "/"}, {label: "Repository", to: "/repo"}, {label: "Parent session", to: "/parent"}],
    }, global: {plugins: [router]}})
    mounted.push(wrapper)
    return {wrapper, router}
}

describe("TitleBar breadcrumbs", () => {
    it("renders only one heading and one link per ancestor, including after title updates", async () => {
        const {wrapper} = await titleBar()
        expect(wrapper.findAll("h1").map(heading => heading.text())).toEqual(["Subagent"])
        expect(wrapper.findAll("a").map(link => link.text())).toEqual(["JailBee", "Repository", "Parent session"])
        await wrapper.setProps({title: "Updated subagent"})
        expect(wrapper.findAll("h1").map(heading => heading.text())).toEqual(["Updated subagent"])
    })
    it("keeps the menu and parent navigation when the bar is hidden", async () => {
        reloadSettings()
        updateSettings({showTitleBar: false})
        const {wrapper, router} = await titleBar()
        expect(wrapper.find("header").exists()).toBe(false)
        await wrapper.get('[aria-label="Quick settings"]').trigger("click")
        await wrapper.get("a").trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.path).toBe("/parent")
        updateSettings({showTitleBar: true})
    })
    it("links each named ancestor to its route and updates the current title", async () => {
        const {wrapper, router} = await titleBar()
        const links = wrapper.get('nav[aria-label="Breadcrumb"]').findAll("a")
        expect(links.map(link => link.attributes("aria-label"))).toEqual(["JailBee", "Repository", "Parent session"])
        await links[2]!.trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.path).toBe("/parent")
        await wrapper.setProps({title: "Updated subagent", crumbs: [{label: "Another parent", to: "/repo"}]})
        expect(wrapper.get("nav h1").text()).toBe("Updated subagent")
        expect(wrapper.get("nav a").attributes("aria-label")).toBe("Another parent")
        await wrapper.get("nav a").trigger("click")
        await flushPromises()
        expect(router.currentRoute.value.path).toBe("/repo")
    })

    it("keeps equally named ancestors distinct when the path changes", async () => {
        const {wrapper} = await titleBar()
        const crumb = (to: string) => ({label: "Same title", to})
        await wrapper.setProps({crumbs: [crumb("/"), crumb("/repo"), crumb("/parent")]})
        await wrapper.setProps({crumbs: [crumb("/parent"), crumb("/repo"), crumb("/"), {label: "New ancestor", to: "/repo"}]})
        await wrapper.setProps({crumbs: [crumb("/repo"), {label: "New ancestor", to: "/parent"}, crumb("/")]})
        expect(wrapper.findAll("nav a").map(link => [link.text(), link.attributes("href")])).toEqual([
            ["Same title", "/repo"], ["New ancestor", "/parent"], ["Same title", "/"],
        ])
    })

    it("keeps session details on the current title without making them breadcrumb levels", async () => {
        const {wrapper} = await titleBar()
        await wrapper.setProps({session: {agent: "codex", models: ["model-a", "model-b"], id: "session-123"}})
        expect(wrapper.get("nav h1").attributes("title")).toBe("Agent: codex\nModels: model-a, model-b\nSession ID: session-123")
        await wrapper.setProps({crumbs: []})
        expect(wrapper.findAll("nav a")).toHaveLength(0)
        expect(wrapper.get("nav h1").text()).toBe("Subagent")
    })
})
