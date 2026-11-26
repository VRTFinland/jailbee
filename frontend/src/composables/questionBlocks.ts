import type {QuestionSet} from "../questions"

/** Locate source question blocks without re-rendering or trusting raw Markdown as HTML. */
export function questionBlocks(prose: Element, set: QuestionSet, index: number): HTMLElement[] {
    const number = set.questions[index]?.number
    if (number === undefined) return []
    // Markdown soft breaks can put prose and several questions in one paragraph.
    // Split only at a real line-leading question marker, preserving all inline
    // nodes from the server's sanitised rendering (and leaving code samples alone).
    for (const strong of prose.querySelectorAll<HTMLElement>(":scope > p > strong")) {
        if (!set.questions.some(q => strong.textContent === `Q${q.number}`)) continue
        const before = strong.previousSibling
        const after = strong.nextSibling
        if (before?.nodeType !== Node.TEXT_NODE || !after?.textContent?.startsWith(" - ")) continue
        const marker = before.textContent?.match(/(?:^|\n)(❓ )$/)
        if (!marker) continue
        const paragraph = strong.parentElement!
        const offset = before.textContent!.length - marker[1]!.length
        if (before === paragraph.firstChild && offset === 0) continue
        const range = document.createRange()
        range.setStart(before, offset)
        range.setEnd(paragraph, paragraph.childNodes.length)
        const question = document.createElement("p")
        question.append(range.extractContents())
        paragraph.after(question)
    }
    const blocks = Array.from(prose.children) as HTMLElement[]
    const isHeader = (el: HTMLElement, n: number) => el.tagName === "P" &&
        el.textContent?.trimStart().startsWith(`❓ Q${n} - `) &&
        el.firstElementChild?.tagName === "STRONG" && el.firstElementChild.textContent === `Q${n}`
    const start = blocks.findIndex(el => isHeader(el, number))
    if (start < 0) return []
    const next = blocks.findIndex((el, i) => i > start && set.questions.some(q => isHeader(el, q.number)))
    return blocks.slice(start, next < 0 ? undefined : next)
}
