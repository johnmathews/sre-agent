import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getHealth, type HealthResponse } from '../api/health'

export const useHealthStore = defineStore('health', () => {
  const data = ref<HealthResponse | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function refresh(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      data.value = await getHealth()
    } catch (err) {
      data.value = null
      error.value = err instanceof Error ? err.message : 'Unreachable'
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, refresh }
})
