/** The top of the controls/guard, including the device's safe-area inset. */
export function readableBottom(): number {
    // Floating controls live in the side margin, not in the readable column.
    const composer = document.querySelector(".agent-composer")
    if (composer && composer.getBoundingClientRect().height > 0) return composer.getBoundingClientRect().top
    const boundary = document.querySelector('.page-nav[data-mode="bar"]') ?? document.querySelector(".bottom-guard")
    return boundary?.getBoundingClientRect().top ?? window.innerHeight
}
