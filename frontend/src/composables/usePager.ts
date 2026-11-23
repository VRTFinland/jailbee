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
    /** Start a page at `el`, continuing page turns from that anchor. */
    goToElement(el: Element): void
    /** Re-measure after the content changed. */
    measure(): void
}

export function usePager(topInset: () => number = () => 0): Pager {
    const availableHeight = ref(1)
    function pageHeight(): number {
        return availableHeight.value
    }

    const page = ref(0)
    // A jump can start a new grid at a reply rather than the document origin.
    // Page zero still starts at zero; its shortened turn never skips content.
    const gridShift = ref(0)
    const contentHeight = ref(0)
    const viewportHeight = ref(window.innerHeight)
    // Pages are bounded by the reachable scroll range, not the content height:
    // the last page is the one the browser can actually scroll to.
    const maxScroll = computed(() => Math.max(0, contentHeight.value - viewportHeight.value))
    const pageCount = computed(() => Math.ceil((maxScroll.value + gridShift.value) / pageHeight()) + 1)

    function currentPage(): number {
        // Only the real bottom is the last page: short of it, Next must still reach the rest.
        if (window.scrollY >= maxScroll.value) {
            return pageCount.value - 1
        }
        const index = Math.floor((window.scrollY + gridShift.value) / pageHeight())
        return Math.max(0, Math.min(pageCount.value - 2, index))
    }

    function measure() {
        // Integer steps survive browser scroll-offset rounding without advancing
        // past the readable area or falling back onto the previous page.
        const height = Math.max(1, Math.floor(readableBottom() - topInset()))
        contentHeight.value = document.documentElement.scrollHeight
        viewportHeight.value = window.innerHeight
        if (height !== availableHeight.value) {
            availableHeight.value = height
            gridShift.value = 0
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
        window.scrollTo(0, Math.max(0, target * pageHeight() - gridShift.value))
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
        goToElement: (el) => {
            measure()
            const top = el.getBoundingClientRect().top + window.scrollY
            const target = Math.max(0, Math.round(top - topInset()))
            const index = Math.ceil(target / pageHeight())
            gridShift.value = index * pageHeight() - target
            goTo(index)
        },
        measure,
    }
}
