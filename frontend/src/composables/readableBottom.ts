/** The top of the controls/guard, including the device's safe-area inset. */
export function readableBottom(): number {
    const boundary = document.querySelector(".page-nav") ?? document.querySelector(".bottom-guard")
    return boundary?.getBoundingClientRect().top ?? window.innerHeight
}
