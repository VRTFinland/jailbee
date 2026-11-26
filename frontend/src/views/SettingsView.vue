<script setup lang="ts">
import TitleBar from "../components/TitleBar.vue"
import SettingsPanel from "../components/SettingsPanel.vue"
</script>

<template>
    <div class="page">
        <TitleBar title="Settings" :crumbs="[{label: 'JailBee', to: {name: 'front'}}]" />
        <SettingsPanel />
    </div>
</template>

<style scoped>
.page { margin: 0 auto; }
</style>
