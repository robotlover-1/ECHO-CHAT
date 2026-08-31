<script setup lang='ts'>
import { onMounted } from 'vue'
import { useAuthStore } from '@/store'
import { fetchDeviceLogin } from '@/api'

interface Props {
  visible: boolean
}

defineProps<Props>()

const authStore = useAuthStore()

// 免密自动登录：首访生成 device_id，后端 upsert 用户并下发 session token
onMounted(async () => {
  if (!authStore.token) {
    try {
      const data = await fetchDeviceLogin() as unknown as { access_token: string }
      authStore.setToken(data.access_token)
      window.location.reload()
    }
    catch (e) {
      console.error('auto login failed', e)
    }
  }
})
</script>

<template>
  <div
    v-if="visible"
    class="fixed inset-0 z-50 flex items-center justify-center bg-white/70 dark:bg-slate-900/70"
  >
    <p class="text-base text-slate-500 dark:text-neutral-300">正在自动登录…</p>
  </div>
</template>
