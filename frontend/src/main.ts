import {createApp} from "vue"
import App from "./App.vue"
import {DASHBOARD, Dashboard} from "./dashboard"
import {createAppRouter} from "./router"
import {startSettings} from "./settings"
import {DashboardSocket} from "./socket"
import "./styles/tokens.css"
import "./styles/highlight.css"

startSettings()
createApp(App)
    .use(createAppRouter())
    .provide(DASHBOARD, new Dashboard((handlers) => new DashboardSocket(handlers)))
    .mount("#app")
