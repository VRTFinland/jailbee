<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import SettingsPanel from "../components/SettingsPanel.vue"
</script>

<template>
    <div>
        <TitleBar title="Settings" :crumbs="[{label: 'JailBee', to: {name: 'front'}}]" />
        <div class="page">
            <SettingsPanel />
        </div>
    </div>
</template>

<style scoped>
.page { margin: 0 auto; }
</style>
