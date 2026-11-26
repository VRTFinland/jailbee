<script setup lang="ts">
import {ref, watch} from "vue"
import Field from "./ui/Field.vue"
import IconButton from "./ui/IconButton.vue"

const props = defineProps<{available: boolean, reason?: string, error?: string, sending?: boolean, awaiting?: boolean, status?: string}>()
const emit = defineEmits<{send: [text: string]}>()
const text = ref("")
watch(() => props.status, status => { if (status === "Delivered") text.value = "" })
function send() {
    if (props.available && !props.sending && !props.awaiting && text.value.trim()) emit("send", text.value)
}
</script>

<template>
    <form class="composer" @submit.prevent="send">
        <Field v-model="text" label="Message agent" multiline :disabled="!available || sending || awaiting"
            :help="!available ? reason : undefined" :error="error" />
        <IconButton icon="page-up" label="Send message" variant="tonal"
            :disabled="!available || sending || awaiting || !text.trim()" @click="send" />
        <span v-if="sending || status" role="status">{{ sending ? 'Sending…' : status }}</span>
    </form>
</template>

<style scoped>
.composer { display: flex; align-items: end; gap: var(--md-space-8); }
.composer > :first-child { flex: 1; min-width: 0; }
</style>
