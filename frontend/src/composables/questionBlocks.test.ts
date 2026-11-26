import {describe, expect, it} from "vitest"
import {parseQuestionSet} from "../questions"
import {questionBlocks} from "./questionBlocks"

describe("questionBlocks", () => {
    it("locates full adjacent question blocks without prose mentions or fenced examples", () => {
        const set = parseQuestionSet("Intro mentions Q1.\n❓ **Q12** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe\n❓ **Q1** - **Owner**: Who?")!
        const prose = document.createElement("div")
        prose.innerHTML = '<pre><code>❓ <strong>Q1</strong> - Example</code></pre><p>Intro mentions Q1.\n❓ <strong>Q12</strong> - <strong>Deploy</strong>: Choose.\na) Canary\n➡️ a: Safe\n❓ <strong>Q1</strong> - <strong>Owner</strong>: Who?</p>'
        const first = questionBlocks(prose, set, 0)
        expect(first.map(el => el.textContent)).toEqual(["❓ Q12 - Deploy: Choose.\na) Canary\n➡️ a: Safe\n"])
        expect(questionBlocks(prose, set, 1).map(el => el.textContent)).toEqual(["❓ Q1 - Owner: Who?"])
        expect(prose.querySelector("pre")?.textContent).toBe("❓ Q1 - Example")
        expect(prose.querySelector("p")?.textContent).toBe("Intro mentions Q1.\n")
    })
})
