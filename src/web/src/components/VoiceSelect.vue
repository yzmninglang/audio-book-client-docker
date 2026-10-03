<script setup lang="ts">
import { computed } from 'vue'

import type { VoiceEntry } from '@/api/types'

/**
 * 可搜索音色下拉框。
 * - filterable:37+ 个中文音色名必须能打字过滤
 * - invalidNames:绑定指向的音色已被删除时,以「失效」高亮展示(见 web/design.md §3.1)
 */
const props = withDefaults(
  defineProps<{
    modelValue: string | null
    voices: VoiceEntry[]
    invalidNames?: string[]
    placeholder?: string
    disabled?: boolean
    size?: 'large' | 'default' | 'small'
    clearable?: boolean
  }>(),
  {
    invalidNames: () => [],
    placeholder: '搜索音色…',
    disabled: false,
    size: 'default',
    clearable: true,
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | null): void
}>()

const voiceNames = computed(() => props.voices.map((v) => v.name))

/** 当前绑定值指向一个不存在的音色 -> 追加一个「失效」选项 */
const danglingOption = computed(() => {
  const v = props.modelValue
  if (!v) return null
  if (voiceNames.value.includes(v)) return null
  return v
})

function onChange(value: string | null) {
  emit('update:modelValue', value)
}

function filterMethod(query: string, option?: { label?: string; value?: string }) {
  if (!option) return false
  const q = query.trim().toLowerCase()
  if (!q) return true
  return (option.label || '').toLowerCase().includes(q) || (option.value || '').toLowerCase().includes(q)
}
</script>

<template>
  <el-select
    :model-value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :size="size"
    :clearable="clearable"
    filterable
    :filter-method="filterMethod"
    class="voice-select"
    @update:model-value="onChange"
  >
    <!-- 失效绑定:置顶 + 高亮,提醒用户重新选择 -->
    <el-option
      v-if="danglingOption"
      :key="`__invalid__${danglingOption}`"
      :label="`${danglingOption}(已失效)`"
      :value="danglingOption"
    >
      <span class="invalid-option">
        <el-icon><WarningFilled /></el-icon>
        {{ danglingOption }}
        <el-tag type="danger" size="small" effect="dark">已失效</el-tag>
      </span>
    </el-option>

    <el-option v-for="name in voiceNames" :key="name" :label="name" :value="name" />
  </el-select>
</template>

<style scoped>
.voice-select {
  width: 100%;
  min-width: 180px;
}
.invalid-option {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--el-color-danger);
}
</style>
