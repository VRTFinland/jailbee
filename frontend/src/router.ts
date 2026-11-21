import {
    createRouter,
    createWebHashHistory,
    type RouteRecordRaw,
    type Router,
    type RouterHistory,
} from "vue-router"
import {authIsDisabled, getToken, NO_LOGIN_TOKEN, setToken} from "./api"
import FrontPage from "./views/FrontPage.vue"
import RepoView from "./views/RepoView.vue"
import ContainerView from "./views/ContainerView.vue"
import SessionView from "./views/SessionView.vue"
import SettingsView from "./views/SettingsView.vue"
import LoginView from "./views/LoginView.vue"
import SetupView from "./views/SetupView.vue"

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
    {path: "/:pathMatch(.*)*", redirect: "/"},
]

/** Hash routing, so bookmarks work behind any reverse-proxy path. */
export function createAppRouter(history: RouterHistory = createWebHashHistory()): Router {
    const router = createRouter({history, routes})
    router.beforeEach(async (to) => {
        if (to.meta.public || getToken()) return true
        if (await authIsDisabled()) {
            setToken(NO_LOGIN_TOKEN)
            return true
        }
        return {name: "login"}
    })
    return router
}
