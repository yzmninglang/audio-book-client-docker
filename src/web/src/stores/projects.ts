import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { BindingsFile, CreateProjectRequest, ProjectDetail, ProjectSummary, RoleView } from '@/api/types'

export const useProjectsStore = defineStore('projects', () => {
  const list = ref<ProjectSummary[]>([])
  const current = ref<ProjectDetail | null>(null)
  const loadingList = ref(false)
  const loadingDetail = ref(false)

  const unboundRoles = computed<RoleView[]>(
    () => current.value?.roles.filter((r) => r.status === 'unbound') || [],
  )
  const invalidRoles = computed<RoleView[]>(
    () => current.value?.roles.filter((r) => r.status === 'invalid') || [],
  )
  const boundCount = computed(
    () => current.value?.roles.filter((r) => r.status === 'ok').length || 0,
  )

  async function loadList() {
    loadingList.value = true
    try {
      list.value = await api.listProjects()
    } finally {
      loadingList.value = false
    }
  }

  async function open(name: string) {
    loadingDetail.value = true
    try {
      current.value = await api.getProject(name)
      return current.value
    } finally {
      loadingDetail.value = false
    }
  }

  async function create(req: CreateProjectRequest) {
    const detail = await api.createProject(req)
    await loadList()
    return detail
  }

  async function saveBindings(bindings: BindingsFile) {
    if (!current.value) throw new Error('未打开工程')
    const saved = await api.updateBindings(current.value.name, bindings)
    current.value.bindings = saved
    // 保存后重新拉详情,拿到后端重新判定的角色状态(ok/unbound/invalid)
    await open(current.value.name)
    return saved
  }

  return {
    list,
    current,
    loadingList,
    loadingDetail,
    unboundRoles,
    invalidRoles,
    boundCount,
    loadList,
    open,
    create,
    saveBindings,
  }
})
