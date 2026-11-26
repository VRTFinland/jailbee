import {
    createRouter,
    createWebHashHistory,
    type RouteLocationNormalized,
    type RouteRecordRaw,
    type Router,
    type RouterHistory,
} from "vue-router"
import {authIsDisabled, fetchServerMode, getToken, NO_LOGIN_TOKEN, setToken} from "./api"
import FrontPage from "./views/FrontPage.vue"
import RepoView from "./views/RepoView.vue"
import ContainerView from "./views/ContainerView.vue"
import SessionView from "./views/SessionView.vue"
import SettingsView from "./views/SettingsView.vue"
import LoginView from "./views/LoginView.vue"
import SetupView from "./views/SetupView.vue"
import GalleryView from "./views/GalleryView.vue"

export const routes: RouteRecordRaw[] = [
    {path: "/login", name: "login", component: LoginView, meta: {public: true}},
    {path: "/setup", name: "setup", component: SetupView, meta: {public: true}},
    {path: "/", name: "front", component: FrontPage},
    {path: "/repos/:prefix", name: "repo", component: RepoView},
    {path: "/containers/:name", name: "container", component: ContainerView},
    {path: "/repos/:prefix/sessions/:agent/:sessionId", name: "session", component: SessionView},
    {
        path: "/repos/:prefix/sessions/:agent/:sessionId/subagents/:agentId",
        name: "subagent",
        component: SessionView,
    },
    {path: "/settings", name: "settings", component: SettingsView},
    // The design system gallery (#167): behind login, linked from no menu.
    {path: "/ui", name: "gallery", component: GalleryView},
    {path: "/:pathMatch(.*)*", redirect: "/"},
]

let resumePath: string | null = null

/**
 * Remembers where the tab was before setup mode took it over. Public routes
 * (login with a single-use device link, setup itself) are never resumed, so
 * they clear any older value instead: it must not outlive its setup episode.
 */
export function rememberResumePath(to: Pick<RouteLocationNormalized, "fullPath" | "meta">): void {
    resumePath = to.meta.public === true ? null : to.fullPath
}

/** The remembered route, once; the front page when there is none. */
export function takeResumePath(): string {
    const path = resumePath ?? "/"
    resumePath = null
    return path
}

/** Hash routing, so bookmarks work behind any reverse-proxy path. */
export function createAppRouter(history: RouterHistory = createWebHashHistory()): Router {
    const router = createRouter({history, routes})
    // A dashboard answer is kept, so later clicks cost no request; a server
    // replaced while the tab is open is noticed by App.vue instead (the socket's
    // setup close code, or the login and setup views' poll). Setup mode is never
    // kept: setup ends with Ctrl-C, so each navigation asks again.
    let mode: Promise<"setup" | "dashboard" | null> | null = null
    router.beforeEach(async (to, from) => {
        to.meta.fromSession = from.name === "session" ? from.path : undefined
        mode ??= fetchServerMode()
        const current = await mode
        if (current !== "dashboard") mode = null
        if (current === "setup") {
            if (to.name !== "setup") rememberResumePath(to)
            return to.name === "setup" ? true : {name: "setup", replace: true}
        }
        if (to.meta.public || getToken()) return true
        if (await authIsDisabled()) {
            setToken(NO_LOGIN_TOKEN)
            return true
        }
        return {name: "login"}
    })
    // Leaving setup by hand (Back, a typed URL) ends the episode.
    router.afterEach((to, from) => {
        if (from.name === "setup" && to.name !== "setup") resumePath = null
    })
    return router
}
