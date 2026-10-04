<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { VoiceEntry } from '@/api/types'
import { filterVoiceNames } from '@/utils/voiceFilter'

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

/**
 * 搜索过滤。
 *
 * ⚠️ 踩坑记录:Element Plus 的 `filter-method` 签名是 `(query: string) => void`
 * —— 它**不**返回布尔值,而是要求你**自己维护被过滤的选项列表**。
 * (Ant Design 的 `filterOption: (input, option) => boolean` 是另一种形状,
 * 照搬过来会**静默失效**:不报错,只是列表不过滤。两个库同名概念 API 不同。)
 */
const filteredNames = ref<string[]>([])

function applyFilter(query: string) {
  filteredNames.value = filterVoiceNames(voiceNames.value, query)
}

// 音色列表异步加载:变化时重建(空 query 恢复完整列表)
watch(voiceNames, () => applyFilter(''), { immediate: true })

function filterMethod(query: string) {
  applyFilter(query)
}

// 每次打开下拉恢复完整列表,避免上次搜索词残留
function onVisibleChange(visible: boolean) {
  if (visible) applyFilter('')
}

function onChange(value: string | null) {
  emit('update:modelValue', value)
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
    @visible-change="onVisibleChange"
  >
    <!-- 失效绑定:置顶 + 高亮,提醒用户重新选择。
         刻意放在 v-for 之外 —— 它是告警,不应被搜索词过滤掉。 -->
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

    <el-option v-for="name in filteredNames" :key="name" :label="name" :value="name" />
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
