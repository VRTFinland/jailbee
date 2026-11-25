/** The top of the controls/guard, including the device's safe-area inset. */
export function readableBottom(): number {
    // Floating controls live in the side margin, not in the readable column.
    const boundary = document.querySelector('.page-nav[data-mode="bar"]') ?? document.querySelector(".bottom-guard")
    return boundary?.getBoundingClientRect().top ?? window.innerHeight
}
