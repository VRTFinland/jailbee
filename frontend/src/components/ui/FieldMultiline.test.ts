import {mount} from "@vue/test-utils"
import {expect, it} from "vitest"
import Field from "./Field.vue"

it("exposes a labelled multiline field and preserves line breaks", async () => {
    const field = mount(Field, {props: {label: "Message", multiline: true}})
    const input = field.get("textarea")
    expect(input.attributes("aria-labelledby")).toBeTruthy()
    await input.setValue("first\nsecond")
    expect(field.emitted("update:modelValue")?.[0]).toEqual(["first\nsecond"])
})
