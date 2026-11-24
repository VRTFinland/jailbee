// Page turns for the e-ink profile: the document is cut into viewport-sized
// pages and moved a page at a time, jumping (never smooth-scrolling) so the
// screen repaints once per turn. Only the bar and the bottom guard are
// reserved by default; a view with a sticky header reserves that too.
import {computed, onBeforeUnmount, onMounted, ref, watch, type ComputedRef, type Ref} from "vue"

import {readableBottom} from "./readableBottom"

export interface Pager {
    /** Zero-based current page. */
    page: Ref<number>
    pageCount: ComputedRef<number>
    next(): void
    prev(): void
    /** Jump to the last page that contains transcript content. */
    end(): void
    /** Start a page at `el`, continuing page turns from that anchor. */
    goToElement(el: Element): void
    /** Re-measure after the content changed. */
    measure(): void
}

/**
 * `topInset` reserves a sticky header. `trailingSpace` is room at the document's
 * end that holds no content (a spacer that lets the last reply scroll to the
 * top): page turns stop once the content's end is readable, while a jump to an
 * element may still use that room.
 */
// Fractional layout leaves sub-pixel remainders: content ending under a pixel
// past a page boundary adds no page, and a real bottom under a pixel short of
// the computed one is still the bottom.
const SUBPIXEL_SLACK = 1

export function usePager(
    topInset: () => number = () => 0,
    trailingSpace: () => number = () => 0,
): Pager {
    const availableHeight = ref(1)
    function pageHeight(): number {
        return availableHeight.value
    }

    const page = ref(0)
    // A jump can start a new grid at a reply rather than the document origin.
    // Page zero still starts at zero; its shortened turn never skips content.
    const gridShift = ref(0)
    // Pages are bounded by the reachable scroll range, not the content height:
    // the last page is the one the browser can actually scroll to, or the one
    // that shows the content's end if trailing space lies beyond it.
    const lastStop = ref(0)
    // A jump into the trailing space extends the grid to that anchor.
    const anchorStop = ref(0)
    const maxScroll = computed(() => Math.max(lastStop.value, anchorStop.value))
    const pageCount = computed(
        () => Math.ceil((maxScroll.value + gridShift.value - SUBPIXEL_SLACK) / pageHeight()) + 1,
    )

    function currentPage(): number {
        // Only the real bottom is the last page: short of it, Next must still reach the rest.
        if (window.scrollY >= maxScroll.value - SUBPIXEL_SLACK) {
            return pageCount.value - 1
        }
        const index = Math.floor((window.scrollY + gridShift.value) / pageHeight())
        return Math.max(0, Math.min(pageCount.value - 2, index))
    }

    function measure() {
        // Integer steps survive browser scroll-offset rounding without advancing
        // past the readable area or falling back onto the previous page.
        const height = Math.max(1, Math.floor(readableBottom() - topInset()))
        const scrollHeight = document.documentElement.scrollHeight
        const reachable = Math.max(0, scrollHeight - window.innerHeight)
        const contentEnd = scrollHeight - Math.max(0, trailingSpace())
        lastStop.value = Math.min(reachable, Math.max(0, Math.ceil(contentEnd - readableBottom())))
        if (height !== availableHeight.value) {
            availableHeight.value = height
            gridShift.value = 0
            anchorStop.value = 0
            page.value = currentPage()
        }
    }

    function syncFromScroll() {
        measure()
        page.value = currentPage()
    }

    watch(topInset, () => {
        // A resized header changes the page grid without moving the reader.
        measure()
        page.value = currentPage()
    }, {flush: "sync"})

    function goTo(index: number) {
        measure()
        const target = Math.min(Math.max(0, index), pageCount.value - 1)
        page.value = target
        window.scrollTo(0, Math.max(0, Math.min(maxScroll.value, target * pageHeight() - gridShift.value)))
    }

    let observer: ResizeObserver | null = null
    onMounted(() => {
        measure()
        window.addEventListener("scroll", syncFromScroll, {passive: true})
        window.addEventListener("resize", syncFromScroll)
        if (typeof ResizeObserver !== "undefined") {
            observer = new ResizeObserver(measure)
            observer.observe(document.body)
        }
    })
    onBeforeUnmount(() => {
        window.removeEventListener("scroll", syncFromScroll)
        window.removeEventListener("resize", syncFromScroll)
        observer?.disconnect()
    })

    return {
        page,
        pageCount,
        next: () => {
            measure()
            goTo(page.value + 1)
        },
        prev: () => {
            measure()
            goTo(page.value - 1)
        },
        end: () => {
            measure()
            anchorStop.value = 0
            goTo(pageCount.value - 1)
        },
        goToElement: (el) => {
            measure()
            const top = el.getBoundingClientRect().top + window.scrollY
            const target = Math.max(0, Math.round(top - topInset()))
            const index = Math.ceil(target / pageHeight())
            gridShift.value = index * pageHeight() - target
            anchorStop.value = Math.min(target, Math.max(0, document.documentElement.scrollHeight - window.innerHeight))
            goTo(index)
        },
        measure,
    }
}
