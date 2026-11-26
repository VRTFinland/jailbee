/** The top of the controls/guard, including the device's safe-area inset. */
export function readableBottom(): number {
    // Floating controls live in the side margin, not in the readable column.
    const panel = document.querySelector(".answer-panel-layout")
    const transcript = document.querySelector(".transcript")
    if (panel && transcript) {
        const rect = panel.getBoundingClientRect()
        const content = transcript.getBoundingClientRect()
        if (rect.height > 0 && rect.left < content.right && rect.right > content.left) return rect.top
    }
    const composer = document.querySelector(".agent-composer")
    if (composer && composer.getBoundingClientRect().height > 0) return composer.getBoundingClientRect().top
    const boundary = document.querySelector('.page-nav[data-mode="bar"]') ?? document.querySelector(".bottom-guard")
    return boundary?.getBoundingClientRect().top ?? window.innerHeight
}
