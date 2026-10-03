import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { HealthResult, ProviderInfo, Settings } from '@/api/types'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<Settings | null>(null)
  const providers = ref<ProviderInfo[]>([])
  const loading = ref(false)
  const saving = ref(false)
  const probing = ref(false)
  const probeResult = ref<HealthResult | null>(null)

  const activeProviderInfo = computed(() =>
    providers.value.find((p) => p.id === settings.value?.active_provider) || null,
  )

  async function load() {
    loading.value = true
    try {
      const [s, p] = await Promise.all([api.getSettings(), api.getProviders()])
      settings.value = s
      providers.value = p
    } finally {
      loading.value = false
    }
  }

  async function loadProviders() {
    providers.value = await api.getProviders()
  }

  async function save(patch: Partial<Settings>) {
    saving.value = true
    try {
      settings.value = await api.updateSettings(patch)
      return settings.value
    } finally {
      saving.value = false
    }
  }

  /** 保存某个 provider 的参数(每个 provider 独立存储,切换后端不丢失) */
  async function saveProviderParams(providerId: string, params: Record<string, unknown>) {
    if (!settings.value) await load()
    const current = settings.value!
    const providersPatch = {
      ...current.providers,
      [providerId]: { ...current.providers[providerId], params },
    }
    return save({ providers: providersPatch })
  }

  async function testConnection(payload: { provider?: string; base_url?: string; voice?: string } = {}) {
    probing.value = true
    probeResult.value = null
    try {
      probeResult.value = await api.probe(payload)
      return probeResult.value
    } finally {
      probing.value = false
    }
  }

  return {
    settings,
    providers,
    loading,
    saving,
    probing,
    probeResult,
    activeProviderInfo,
    load,
    loadProviders,
    save,
    saveProviderParams,
    testConnection,
  }
})
