import {
    createRouter,
    createWebHashHistory,
    type RouteRecordRaw,
    type Router,
    type RouterHistory,
} from "vue-router"
import {getToken} from "./api"
import FrontPage from "./views/FrontPage.vue"
import LoginView from "./views/LoginView.vue"

export const routes: RouteRecordRaw[] = [
    {path: "/login", name: "login", component: LoginView, meta: {public: true}},
    {path: "/", name: "front", component: FrontPage},
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
