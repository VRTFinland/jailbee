import {
    createRouter,
    createWebHashHistory,
    type RouteRecordRaw,
    type Router,
    type RouterHistory,
} from "vue-router"
import {getToken} from "./api"
import FrontPage from "./views/FrontPage.vue"
import RepoView from "./views/RepoView.vue"
import SessionPlaceholder from "./views/SessionPlaceholder.vue"
import LoginView from "./views/LoginView.vue"

export const routes: RouteRecordRaw[] = [
    {path: "/login", name: "login", component: LoginView, meta: {public: true}},
    {path: "/", name: "front", component: FrontPage},
    {path: "/repos/:prefix", name: "repo", component: RepoView},
    // Ticket 05 replaces the placeholder with the session view; the route name and params stay.
    {
        path: "/repos/:prefix/sessions/:agent/:sessionId",
        name: "session",
        component: SessionPlaceholder,
    },
    {path: "/:pathMatch(.*)*", redirect: "/"},
]

/** Hash routing, so bookmarks work behind any reverse-proxy path. */
export function createAppRouter(history: RouterHistory = createWebHashHistory()): Router {
    const router = createRouter({history, routes})
    router.beforeEach((to) => {
        if (!to.meta.public && !getToken()) return {name: "login"}
        return true
    })
    return router
}
