import {ref} from "vue"
import {useRoute, useRouter} from "vue-router"
import {settings} from "../settings"

/** Per-visit filtering and the one-time shortcut after a list loads. */
export function useSessionList() {
    const route = useRoute()
    const router = useRouter()
    const liveOnly = ref(settings.liveSessionsOnly)

    async function openSingleLive(prefix: string, sessions: {agent: string; session_id: string}[]) {
        if (!liveOnly.value || !settings.openOnlyLiveSession || sessions.length !== 1) return
        const session = sessions[0]!
        const target = {name: "session", params: {prefix, agent: session.agent, sessionId: session.session_id}}
        // Breadcrumbs and browser Back must reach the list rather than bounce.
        if (route.meta.fromSession === router.resolve(target).path) return
        await router.replace(target)
    }

    return {liveOnly, openSingleLive}
}
