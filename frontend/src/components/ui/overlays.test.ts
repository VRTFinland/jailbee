import {afterEach, describe, expect, it} from "vitest"
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

describe("Select", () => {
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

describe("Field", () => {
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
