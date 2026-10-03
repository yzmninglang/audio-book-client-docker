<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import PathBrowser from '@/components/PathBrowser.vue'
import VoiceSelect from '@/components/VoiceSelect.vue'
import { ApiError } from '@/api/client'
import type { ParamSpec } from '@/api/types'
import { useSettingsStore } from '@/stores/settings'
import { useVoicesStore } from '@/stores/voices'

const settings = useSettingsStore()
const voices = useVoicesStore()

const providerForm = reactive({ base_url: '', timeout: 300, retries: 3 })
const globalForm = reactive({
  workspace_root: '',
  voices_root: '',
  concurrency: 1,
  reference_max_seconds: 15,
})

/** 动态参数值:键名完全来自 param_schema,无任何硬编码字段 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const paramValues = ref<Record<string, any>>({})
const probeVoice = ref<string | null>(null)

const savingProvider = ref(false)
const savingGlobal = ref(false)
const savingParams = ref(false)

const workspaceDialog = ref(false)
const voicesDialog = ref(false)

const activeId = computed(() => settings.settings?.active_provider || '')
const paramSchema = computed<ParamSpec[]>(() => settings.activeProviderInfo?.param_schema || [])
const capabilities = computed(() => settings.activeProviderInfo?.capabilities || null)

const probe = computed(() => settings.probeResult)

/** 测试连接成功时的试听 URL(base64 -> data URL) */
const probeAudioUrl = computed(() => {
  const b64 = settings.probeResult?.sample_audio
  return b64 ? `data:audio/wav;base64,${b64}` : ''
})

onMounted(async () => {
  await settings.load()
  await voices.load().catch(() => undefined)
  syncAll()
})

function syncAll() {
  syncProviderForm()
  syncGlobalForm()
  buildParamValues()
}

function syncProviderForm() {
  const cfg = settings.settings?.providers[activeId.value]
  providerForm.base_url = cfg?.base_url || ''
  providerForm.timeout = cfg?.timeout ?? 300
  providerForm.retries = cfg?.retries ?? 3
}

function syncGlobalForm() {
  if (!settings.settings) return
  globalForm.workspace_root = settings.settings.workspace_root
  globalForm.voices_root = settings.settings.voices_root
  globalForm.concurrency = settings.settings.concurrency
  globalForm.reference_max_seconds = settings.settings.reference_max_seconds
}

/** 用 param_schema 的 default 填充,再覆盖已保存的 provider 参数 */
function buildParamValues() {
  const saved = settings.settings?.providers[activeId.value]?.params || {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const next: Record<string, any> = {}
  for (const spec of paramSchema.value) {
    next[spec.name] = spec.name in saved ? saved[spec.name] : spec.default
  }
  paramValues.value = next
}

async function onProviderChange(id: string) {
  if (!settings.settings) return
  settings.settings.active_provider = id
  try {
    await settings.save({ active_provider: id })
    syncProviderForm()
    buildParamValues()
    settings.probeResult = null
    ElMessage.success(`已切换后端:${settings.providers.find((p) => p.id === id)?.label || id}`)
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

async function saveProvider() {
  if (!settings.settings) return
  savingProvider.value = true
  try {
    const providers = {
      ...settings.settings.providers,
      [activeId.value]: {
        ...settings.settings.providers[activeId.value],
        base_url: providerForm.base_url,
        timeout: Number(providerForm.timeout),
        retries: Number(providerForm.retries),
      },
    }
    await settings.save({ providers })
    ElMessage.success('后端连接设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    savingProvider.value = false
  }
}

async function saveGlobal() {
  savingGlobal.value = true
  try {
    await settings.save({
      workspace_root: globalForm.workspace_root,
      voices_root: globalForm.voices_root,
      concurrency: Number(globalForm.concurrency),
      reference_max_seconds: Number(globalForm.reference_max_seconds),
    })
    ElMessage.success('全局设置已保存')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    savingGlobal.value = false
  }
}

async function saveParams() {
  savingParams.value = true
  try {
    await settings.saveProviderParams(activeId.value, { ...paramValues.value })
    ElMessage.success('生成参数已保存(该 provider 独立存储)')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  } finally {
    savingParams.value = false
  }
}

async function testConnection() {
  try {
    await settings.testConnection({
      provider: activeId.value,
      base_url: providerForm.base_url,
      voice: probeVoice.value || undefined,
    })
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : String(err))
  }
}

/** 动态控件:step 由类型决定(int -> 1,float -> 0.01) */
function stepOf(spec: ParamSpec): number {
  return spec.type === 'int' ? 1 : 0.01
}
function minOf(spec: ParamSpec): number {
  return spec.minimum ?? 0
}
function maxOf(spec: ParamSpec): number {
  return spec.maximum ?? (spec.type === 'int' ? 100 : 1)
}

const probeKindMeta: Record<string, { type: 'success' | 'error' | 'warning' | 'info'; title: string }> = {
  ok: { type: 'success', title: '连通' },
  unreachable: { type: 'error', title: '后端不可达' },
  model_not_loaded: { type: 'error', title: '模型未加载' },
  server_error: { type: 'error', title: '服务端错误' },
  bad_request: { type: 'warning', title: '请求参数有误' },
}
</script>

<template>
  <div class="page" v-loading="settings.loading">
    <h2 class="page-title">设置</h2>

    <!-- 后端选择 -->
    <el-card shadow="never" class="mb">
      <template #header>后端选择</template>
      <el-form label-width="120px">
        <el-form-item label="TTS 后端">
          <el-select :model-value="activeId" style="width: 320px" @change="onProviderChange">
            <el-option v-for="p in settings.providers" :key="p.id" :label="p.label" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="capabilities" label="能力">
          <el-tag size="small" class="chip">情感向量:{{ capabilities.emotion.includes('vector') ? '支持' : '不支持' }}</el-tag>
          <el-tag size="small" class="chip" type="info">向量维度:{{ capabilities.emotion_vector_dims ?? '—' }}</el-tag>
          <el-tag size="small" class="chip" type="info">参考文本:{{ capabilities.needs_prompt_text ? '必需' : '不需要' }}</el-tag>
          <el-tag size="small" class="chip" type="info">可并发:{{ capabilities.supports_concurrent ? '是' : '否' }}</el-tag>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 后端连接 + 测试连接 -->
    <el-card shadow="never" class="mb">
      <template #header>后端连接与测试</template>
      <el-form label-width="120px">
        <el-form-item label="后端地址">
          <el-input v-model="providerForm.base_url" placeholder="http://hinas-v4.ninglang.top:8300" />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="providerForm.timeout" :min="1" :max="3600" />
        </el-form-item>
        <el-form-item label="重试次数">
          <el-input-number v-model="providerForm.retries" :min="0" :max="10" />
        </el-form-item>
        <el-form-item label="探测音色">
          <div style="width: 320px">
            <VoiceSelect v-model="probeVoice" :voices="voices.voices" placeholder="选择用于探测的参考音色(可选)" />
          </div>
        </el-form-item>
        <el-form-item>
          <el-button :loading="savingProvider" @click="saveProvider">保存连接设置</el-button>
          <el-button type="primary" :loading="settings.probing" @click="testConnection">测试连接</el-button>
        </el-form-item>
      </el-form>

      <!-- 四种测试结果 -->
      <template v-if="probe">
        <el-alert
          :type="probeKindMeta[probe.kind]?.type || 'info'"
          show-icon
          :closable="false"
          :title="`${probeKindMeta[probe.kind]?.title || probe.kind}:${probe.message}`"
        >
          <div v-if="probe.kind === 'ok'" class="probe-detail">
            <span>延迟 {{ probe.latency_ms ?? '—' }} ms</span>
            <span v-if="probe.duration_ms != null">音频时长 {{ probe.duration_ms }} ms</span>
            <span v-if="probe.sample_rate">采样率 {{ probe.sample_rate }} Hz</span>
          </div>
        </el-alert>
        <audio v-if="probe.kind === 'ok' && probeAudioUrl" :src="probeAudioUrl" controls class="probe-audio" />
      </template>
    </el-card>

    <!-- 全局设置 -->
    <el-card shadow="never" class="mb">
      <template #header>全局设置</template>
      <el-form label-width="120px">
        <el-form-item label="工作区路径">
          <el-input v-model="globalForm.workspace_root" readonly style="width: 320px" />
          <el-button class="ml" @click="workspaceDialog = true">浏览容器内目录</el-button>
        </el-form-item>
        <el-form-item label="音色库路径">
          <el-input v-model="globalForm.voices_root" readonly style="width: 320px" />
          <el-button class="ml" @click="voicesDialog = true">浏览容器内目录</el-button>
        </el-form-item>
        <el-form-item label="并发数">
          <el-input-number v-model="globalForm.concurrency" :min="1" :max="8" />
          <span class="muted ml">后端无并发保护,默认 1(严格串行)</span>
        </el-form-item>
        <el-form-item label="参考音色上限(秒)">
          <el-input-number v-model="globalForm.reference_max_seconds" :min="1" :max="60" />
        </el-form-item>
        <el-form-item>
          <el-button :loading="savingGlobal" @click="saveGlobal">保存全局设置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 动态生成参数 -->
    <el-card shadow="never">
      <template #header>
        生成参数(由 param_schema 动态渲染)
        <span class="muted">— 新增 provider 时本表单零改动</span>
      </template>

      <el-empty v-if="paramSchema.length === 0" description="该后端未声明可调参数" :image-size="60" />
      <el-form v-else label-width="180px" class="param-form">
        <el-form-item v-for="spec in paramSchema" :key="spec.name" :label="spec.label">
          <!-- bool -> 开关 -->
          <el-switch v-if="spec.type === 'bool'" v-model="paramValues[spec.name]" />

          <!-- enum -> 下拉 -->
          <el-select v-else-if="spec.type === 'enum'" v-model="paramValues[spec.name]" style="width: 220px">
            <el-option v-for="c in spec.choices || []" :key="c" :label="c" :value="c" />
          </el-select>

          <!-- str -> 输入框 -->
          <el-input v-else-if="spec.type === 'str'" v-model="paramValues[spec.name]" style="width: 220px" />

          <!-- int / float -> 滑块 + 数字输入 -->
          <div v-else class="num-row">
            <el-slider
              v-model="paramValues[spec.name]"
              :min="minOf(spec)"
              :max="maxOf(spec)"
              :step="stepOf(spec)"
              style="width: 320px"
            />
            <el-input-number
              v-model="paramValues[spec.name]"
              :min="minOf(spec)"
              :max="maxOf(spec)"
              :step="stepOf(spec)"
              :precision="spec.type === 'int' ? 0 : 2"
            />
            <span class="muted">默认 {{ spec.default }}</span>
          </div>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="savingParams" @click="saveParams">保存生成参数</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 路径浏览对话框 -->
    <el-dialog v-model="workspaceDialog" title="选择工作区目录" width="640px">
      <PathBrowser v-model="globalForm.workspace_root" :start-path="globalForm.workspace_root" />
      <template #footer>
        <el-button type="primary" @click="workspaceDialog = false">完成</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="voicesDialog" title="选择音色库目录" width="640px">
      <PathBrowser v-model="globalForm.voices_root" :start-path="globalForm.voices_root" />
      <template #footer>
        <el-button type="primary" @click="voicesDialog = false">完成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 16px;
}
.ml {
  margin-left: 10px;
}
.chip {
  margin-right: 8px;
}
.param-form {
  max-width: 720px;
}
.num-row {
  display: flex;
  align-items: center;
  gap: 14px;
}
.probe-detail {
  display: flex;
  gap: 18px;
  margin-top: 4px;
}
.probe-audio {
  width: 100%;
  margin-top: 10px;
}
</style>
