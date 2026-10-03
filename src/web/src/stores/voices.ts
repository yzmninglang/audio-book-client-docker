import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api } from '@/api/client'
import type { CreateVoiceRequest, UpdateVoiceRequest, VoiceEntry } from '@/api/types'

export const useVoicesStore = defineStore('voices', () => {
  const voices = ref<VoiceEntry[]>([])
  const loading = ref(false)

  async function load() {
    loading.value = true
    try {
      voices.value = await api.listVoices()
    } finally {
      loading.value = false
    }
  }

  function exists(name: string): boolean {
    return voices.value.some((v) => v.name === name)
  }

  function byName(name: string): VoiceEntry | undefined {
    return voices.value.find((v) => v.name === name)
  }

  async function create(req: CreateVoiceRequest) {
    const entry = await api.createVoice(req)
    voices.value = [...voices.value, entry]
    return entry
  }

  async function update(name: string, req: UpdateVoiceRequest) {
    const entry = await api.updateVoice(name, req)
    await load()
    return entry
  }

  async function rename(oldName: string, newName: string) {
    if (newName === oldName) return byName(oldName)
    return update(oldName, { rename: newName })
  }

  async function remove(name: string) {
    await api.deleteVoice(name)
    voices.value = voices.value.filter((v) => v.name !== name)
  }

  async function previewUrl(name: string): Promise<string> {
    return api.voicePreviewUrl(name)
  }

  return { voices, loading, load, exists, byName, create, update, rename, remove, previewUrl }
})
