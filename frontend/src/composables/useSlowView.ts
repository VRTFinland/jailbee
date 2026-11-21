// A displayed copy of a live value. While `slow` is on, the copy changes only
// on the timer or on `refresh()`, so an e-ink screen is not repainted on every
// update; while off, it follows the source at once.
import {onBeforeUnmount, ref, watch, type Ref} from "vue"

export function useSlowView<T>(
    source: () => T | null,
    slow: () => boolean,
    intervalMs: () => number,
): {shown: Ref<T | null>; refresh: () => void} {
    const shown = ref(source()) as Ref<T | null>
    let timer: ReturnType<typeof setInterval> | null = null

    function refresh() {
        shown.value = source()
    }

    function stop() {
        if (timer !== null) clearInterval(timer)
        timer = null
    }

    watch(source, () => {
        // The first snapshot is always shown; later ones wait while slow.
        if (!slow() || shown.value === null) refresh()
    })
    watch(
        [slow, intervalMs],
        () => {
            stop()
            if (!slow()) {
                refresh()
                return
            }
            timer = setInterval(refresh, intervalMs())
        },
        {immediate: true},
    )
    onBeforeUnmount(stop)

    return {shown, refresh}
}
