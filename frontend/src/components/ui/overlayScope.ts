import {ref, watchEffect, type Ref} from "vue"

/** Portals leave their theme/profile ancestors; carry the nearest scopes with them. */
export function useOverlayScope(anchor: Ref<HTMLElement | undefined>, open: Ref<boolean>) {
    const scope = ref<Record<string, string>>({})
    watchEffect((cleanup) => {
        const element = anchor.value
        if (!element || !open.value) return
        const update = () => {
            scope.value = Object.fromEntries(["theme", "profile"].flatMap((name) => {
                const value = element.closest(`[data-${name}]`)?.getAttribute(`data-${name}`)
                return value ? [[`data-${name}`, value]] : []
            }))
        }
        update()
        const observer = new MutationObserver(update)
        for (let parent: HTMLElement | null = element; parent; parent = parent.parentElement) {
            observer.observe(parent, {attributes: true, attributeFilter: ["data-theme", "data-profile"]})
        }
        cleanup(() => observer.disconnect())
    }, {flush: "sync"})
    return scope
}
