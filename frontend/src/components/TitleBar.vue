<script setup lang="ts">
import QuickSettings from "./QuickSettings.vue"
import type {RouteLocationRaw} from "vue-router"

defineProps<{title: string; crumbs?: {label: string; to: RouteLocationRaw}[]; context?: "containers" | "session" | null; session?: {agent: string; models: string[]; id: string} | null}>()
</script>

<template>
    <header class="title-bar">
        <nav aria-label="Breadcrumb" class="breadcrumbs">
            <template v-for="crumb in crumbs" :key="crumb.label">
                <router-link :to="crumb.to" :title="crumb.label">{{ crumb.label }}</router-link><span class="separator" aria-hidden="true">›</span>
            </template>
            <h1 :title="session ? `Agent: ${session.agent}\nModels: ${session.models.join(', ')}\nSession ID: ${session.id}` : title">{{ title }}</h1>
        </nav>
        <slot />
        <QuickSettings class="title-menu" :context="context" :session="session" />
    </header>
</template>

<style scoped>
.title-bar { position: sticky; top: var(--app-banner-height, 0px); z-index: 12; display: flex; align-items: center; box-sizing: border-box; height: 44px; background: var(--bg); border-bottom: 1px solid var(--border); min-width: 0; }
.breadcrumbs { display: flex; align-items: center; flex: 1; min-width: 0; overflow: hidden; }
.breadcrumbs a { display: flex; align-items: center; height: 44px; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--muted); font-size: 13px; text-decoration: none; }
.separator { padding: 0 6px; color: var(--muted); }
h1 { font-size: 15px; line-height: 1.2; font-weight: 600; margin: 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
