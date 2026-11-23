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
    /** Go to the page that holds `el`. */
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
    const contentHeight = ref(0)
    const pageCount = computed(() =>
        Math.max(1, Math.ceil(contentHeight.value / pageHeight())),
    )

    function measure() {
        const height = Math.max(1, readableBottom() - topInset())
        if (height !== availableHeight.value) {
            availableHeight.value = height
            page.value = Math.max(0, Math.floor(window.scrollY / height))
        }
        contentHeight.value = document.documentElement.scrollHeight
    }

    function syncFromScroll() {
        measure()
        page.value = Math.max(0, Math.floor(window.scrollY / pageHeight()))
    }

    watch(topInset, () => {
        // A resized header changes the page grid without moving the reader.
        measure()
        page.value = Math.max(0, Math.floor(window.scrollY / pageHeight()))
    }, {flush: "sync"})

    function goTo(index: number) {
        measure()
        const target = Math.min(Math.max(0, index), pageCount.value - 1)
        page.value = target
        window.scrollTo(0, target * pageHeight())
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
            goTo(Math.floor(Math.max(0, top - topInset()) / pageHeight()))
        },
        measure,
    }
}
