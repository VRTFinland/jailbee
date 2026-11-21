import {createApp} from "vue"
import App from "./App.vue"
import {createAppRouter} from "./router"
import "./styles/tokens.css"
import "./styles/highlight.css"

createApp(App).use(createAppRouter()).mount("#app")
