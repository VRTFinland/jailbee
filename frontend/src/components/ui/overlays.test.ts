import {afterEach, describe, expect, it} from "vitest"
import {createHash} from "node:crypto"
import {readFileSync} from "node:fs"
import {mount, flushPromises} from "@vue/test-utils"
import {defineComponent} from "vue"
import {getByRole, queryByRole} from "@testing-library/dom"
import userEvent from "@testing-library/user-event"
import Dialog from "./Dialog.vue"
import Button from "./Button.vue"
import Menu from "./Menu.vue"
import Field from "./Field.vue"
import Select from "./Select.vue"

const mounted: ReturnType<typeof mount>[] = []
afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    document.body.innerHTML = ""
})
function render(component: ReturnType<typeof defineComponent>) {
    const wrapper = mount(component, {attachTo: document.body})
    mounted.push(wrapper)
    return wrapper
}
async function settle() {
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 30))
}

describe.each([
    {name: "Dialog", Component: Dialog, role: "dialog", props: {title: "Example"}},
    {name: "Menu", Component: Menu, role: "menu", props: {label: "Example", items: [{value: "one", label: "One"}]}},
    {name: "Select", Component: Select, role: "listbox", props: {label: "Example", options: [{value: "one", label: "One"}]}},
])("$name portal scope", ({Component, role, props}) => {
    it("inherits nearest scopes, follows changes and leaves no empty scopes", async () => {
        const wrapper = render(defineComponent({components: {Component, Button}, data: () => ({props, open: false}), template: `
            <div data-theme="light" data-profile="eink"><div data-theme="dark" data-profile="normal">
                <Component v-bind="props" v-model:open="open"><template #trigger><Button>Open</Button></template></Component>
            </div></div>`,
        }))
        expect(document.body.querySelectorAll(".ui-overlay-scope")).toHaveLength(0)
        await wrapper.setData({open: true})
        await settle()
        const scope = getByRole(document.body, role).closest(".ui-overlay-scope")!
        expect(scope.getAttribute("data-theme")).toBe("dark")
        expect(scope.getAttribute("data-profile")).toBe("normal")
        wrapper.get('[data-theme="dark"]').element.setAttribute("data-theme", "eink")
        wrapper.get('[data-profile="normal"]').element.setAttribute("data-profile", "eink")
        await settle()
        expect(scope.getAttribute("data-theme")).toBe("eink")
        expect(scope.getAttribute("data-profile")).toBe("eink")
        await wrapper.setData({open: false})
        await settle()
        expect(document.body.querySelectorAll(".ui-overlay-scope")).toHaveLength(0)
        await wrapper.setData({open: true})
        await settle()
        wrapper.unmount()
        mounted.splice(mounted.indexOf(wrapper), 1)
        await settle()
        expect(document.body.querySelectorAll(".ui-overlay-scope")).toHaveLength(0)
    })
})

describe("Select", () => {
    it("preserves native platform selection for paginated preference forms", async () => {
        const wrapper = render(defineComponent({components: {Select}, data: () => ({value: "hidden"}), template: `
            <Select v-model="value" native label="Agent context" :options="[
                {value: 'hidden', label: 'Hidden'}, {value: 'shown', label: 'Shown'}]" />
            <output>{{ value }}</output>`,
        }))
        const control = getByRole(document.body, "combobox", {name: "Agent context"})
        expect(control.tagName).toBe("SELECT")
        await userEvent.setup().selectOptions(control, "shown")
        expect(wrapper.get("output").text()).toBe("shown")
    })
    it("keeps every injected viewport stylesheet allowed by the server CSP", async () => {
        const user = userEvent.setup()
        render(defineComponent({components: {Select}, template: '<Select label="Theme" :options="[{value: \'light\', label: \'Light\'}]" />'}))
        getByRole(document.body, "combobox", {name: "Theme"}).focus()
        await user.keyboard("{Enter}")
        await settle()
        const styles = [...document.body.querySelectorAll("style")]
        expect(styles.length).toBeGreaterThan(0)
        const policy = readFileSync("../src/jailbee/web/security_headers.py", "utf8")
        for (const style of styles) {
            const hash = createHash("sha256").update(style.textContent!).digest("base64")
            expect(policy).toContain(`'sha256-${hash}'`)
        }
    })
    it("picks an enabled value by keyboard and returns focus to its labelled control", async () => {
        const user = userEvent.setup()
        const wrapper = render(defineComponent({components: {Select}, data: () => ({theme: "light"}), template: `
            <Select v-model="theme" label="Theme" :options="[
                {value: 'light', label: 'Light'}, {value: 'system', label: 'System', disabled: true},
                {value: 'dark', label: 'Dark'}]" /><output>{{ theme }}</output>`,
        }))
        const control = getByRole(document.body, "combobox", {name: "Theme"})
        control.focus()
        await user.keyboard("{Enter}")
        await settle()
        getByRole(document.body, "listbox")
        await user.keyboard("{ArrowDown}{Enter}")
        await settle()
        expect(wrapper.get("output").text()).toBe("dark")
        expect(control.textContent).toContain("Dark")
        expect(queryByRole(document.body, "listbox")).toBeNull()
        expect(document.activeElement).toBe(control)
    })
})

describe.each([Field, Select])("labelled control", (Control) => {
    it("merges external descriptions with help and error and updates forwarded attrs", async () => {
        const wrapper = render(defineComponent({components: {Control}, data: () => ({autocomplete: "off"}), template: `
            <p id="external-help">External instructions.</p>
            <Control label="Name" aria-describedby="external-help" help="Short name." error="Required."
                :autocomplete="autocomplete" :options="[]" />`,
        }))
        const control = getByRole(document.body, Control === Field ? "textbox" : "combobox", {name: "Name"})
        expect(control.getAttribute("aria-describedby")!.split(" ")
            .map((id) => document.getElementById(id)!.textContent).join(" "))
            .toBe("External instructions. Short name. Required.")
        await wrapper.setData({autocomplete: "on"})
        expect(control.getAttribute("autocomplete")).toBe("on")
    })
    it("keeps custom ids, wrapper classes and announced descriptions wired to the control", () => {
        render(defineComponent({components: {Control}, template: `
            <Control id="custom-control" class="layout-field" label="Name" help="Use a short name."
                error="Name is required." autocomplete="off" :options="[]" />`,
        }))
        const control = getByRole(document.body, Control === Field ? "textbox" : "combobox", {name: "Name"})
        expect(control.id).toBe("custom-control")
        expect(control.getAttribute("autocomplete")).toBe("off")
        expect(control.classList.contains("layout-field")).toBe(false)
        expect(control.closest(".layout-field")).not.toBeNull()
        expect(control.getAttribute("aria-invalid")).toBe("true")
        expect(control.getAttribute("aria-describedby")!.split(" ")
            .map((id) => document.getElementById(id)!.textContent).join(" "))
            .toBe("Use a short name. Name is required.")
        expect(getByRole(document.body, "alert").textContent).toBe("Name is required.")
    })
})

describe("Field", () => {
    it("preserves a caller's more descriptive accessible name", () => {
        render(defineComponent({components: {Field}, template: '<Field label="Name" aria-label="Full legal name" />'}))
        expect(getByRole(document.body, "textbox", {name: "Full legal name"})).toBeTruthy()
    })
    it("associates its label, help and error with the input and updates its value", async () => {
        const user = userEvent.setup()
        const wrapper = render(defineComponent({components: {Field}, data: () => ({name: ""}), template: `
            <Field v-model="name" label="Container name" help="Use a short name." error="Name is required." required />
            <output>{{ name }}</output>`,
        }))
        const input = getByRole(document.body, "textbox", {name: "Container name"}) as HTMLInputElement
        expect(input.required).toBe(true)
        expect(input.getAttribute("aria-invalid")).toBe("true")
        const description = input.getAttribute("aria-describedby")!.split(" ")
            .map((id) => document.getElementById(id)!.textContent).join(" ")
        expect(description).toBe("Use a short name. Name is required.")
        await user.type(input, "dev-plan")
        expect(wrapper.get("output").text()).toBe("dev-plan")
    })
})

describe("Menu", () => {
    it("navigates enabled items by arrow key, activates with Enter and restores focus", async () => {
        const user = userEvent.setup()
        const wrapper = render(defineComponent({components: {Menu, Button}, data: () => ({chosen: ""}), template: `
            <Menu label="Container actions" :items="[
                {value: 'shell', label: 'Shell'}, {value: 'logs', label: 'Logs', disabled: true},
                {value: 'stop', label: 'Stop'}]" @select="chosen = $event">
                <template #trigger><Button>Actions</Button></template>
            </Menu><output>{{ chosen }}</output>`,
        }))
        const opener = getByRole(document.body, "button", {name: "Actions"})
        opener.focus()
        await user.keyboard("{ArrowDown}")
        await settle()
        const menu = getByRole(document.body, "menu", {name: "Container actions"})
        expect(document.activeElement).toBe(getByRole(menu, "menuitem", {name: "Shell"}))
        await user.keyboard("{ArrowDown}")
        expect(document.activeElement).toBe(getByRole(menu, "menuitem", {name: "Stop"}))
        await user.keyboard("{Enter}")
        await settle()
        expect(wrapper.get("output").text()).toBe("stop")
        expect(queryByRole(document.body, "menu")).toBeNull()
        expect(document.activeElement).toBe(opener)
    })
})

describe("Dialog", () => {
    it("supports a labelled anchored preferences form without freezing the live document", async () => {
        const user = userEvent.setup()
        render(defineComponent({components: {Dialog, Button, Field}, template: `
            <Dialog title="Quick settings" placement="popover" preserve-scroll close-label="Close preferences">
                <template #trigger><Button>Preferences</Button></template>
                <Field label="Device" /><Button>Save</Button>
            </Dialog>`,
        }))
        const opener = getByRole(document.body, "button", {name: "Preferences"})
        await user.click(opener)
        await settle()
        const dialog = getByRole(document.body, "dialog", {name: "Quick settings"})
        expect(dialog.getAttribute("data-placement")).toBe("popover")
        expect(document.body.style.overflow).not.toBe("hidden")
        expect(getByRole(dialog, "button", {name: "Close preferences"})).toBeTruthy()
        await user.keyboard("{Escape}")
        await settle()
        expect(document.activeElement).toBe(opener)
    })
    it("renders actions inside the modal and forwards attrs to the modal, not its anchor", async () => {
        const user = userEvent.setup()
        render(defineComponent({components: {Dialog, Button}, template: `
            <Dialog title="Settings" data-testid="settings" class="settings-layout">
                <template #trigger><Button>Open</Button></template><p>Preferences</p>
                <template #actions="{close}"><Button @click="close">Save settings</Button></template>
            </Dialog>`,
        }))
        await user.click(getByRole(document.body, "button", {name: "Open"}))
        await settle()
        const dialog = getByRole(document.body, "dialog", {name: "Settings"})
        expect(dialog.getAttribute("data-testid")).toBe("settings")
        expect(dialog.classList.contains("settings-layout")).toBe(true)
        expect(document.querySelectorAll('[data-testid="settings"]')).toHaveLength(1)
        await user.click(getByRole(dialog, "button", {name: "Save settings"}))
        await settle()
        expect(queryByRole(document.body, "dialog")).toBeNull()
    })
    it("dismisses on outside pointer interaction and returns focus", async () => {
        const user = userEvent.setup()
        render(defineComponent({components: {Dialog, Button}, template: `
            <Dialog title="Settings" description="Preferences">
                <template #trigger><Button>Open</Button></template><Button>Save</Button>
            </Dialog>`,
        }))
        const opener = getByRole(document.body, "button", {name: "Open"})
        await user.click(opener)
        await settle()
        await user.click(document.querySelector(".ui-dialog-scrim")!)
        await settle()
        expect(queryByRole(document.body, "dialog")).toBeNull()
        expect(document.activeElement).toBe(opener)
    })

    it("labels its modal, traps focus, closes on Escape and returns focus to the opener", async () => {
        const user = userEvent.setup()
        render(defineComponent({components: {Dialog, Button}, template: `
            <Dialog title="Destroy container?" description="Uncommitted work will be lost.">
                <template #trigger><Button>Open dialog</Button></template>
                <Button>Cancel</Button><Button>Destroy</Button>
            </Dialog><button>Outside</button>`,
        }))
        const opener = getByRole(document.body, "button", {name: "Open dialog"})
        await user.click(opener)
        await settle()
        const dialog = getByRole(document.body, "dialog", {name: "Destroy container?"})
        expect(dialog.getAttribute("aria-modal")).toBe("true")
        expect(document.getElementById(dialog.getAttribute("aria-describedby")!)?.textContent)
            .toBe("Uncommitted work will be lost.")
        const last = getByRole(dialog, "button", {name: "Destroy"})
        last.focus()
        await user.tab()
        expect(dialog.contains(document.activeElement)).toBe(true)
        await user.tab({shift: true})
        expect(document.activeElement).toBe(last)
        await user.keyboard("{Escape}")
        await settle()
        expect(queryByRole(document.body, "dialog")).toBeNull()
        expect(document.activeElement).toBe(opener)
    })
})
