import {createApp} from "vue"
import App from "./App.vue"
import {createAppRouter} from "./router"
import {startSettings} from "./settings"
import "./styles/tokens.css"
import "./styles/highlight.css"

startSettings()
createApp(App).use(createAppRouter()).mount("#app")
