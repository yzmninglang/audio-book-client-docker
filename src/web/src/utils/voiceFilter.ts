/**
 * 音色名搜索过滤(纯函数,便于单测)。
 *
 * 空 query 返回完整列表;否则按大小写不敏感的子串匹配。
 * 中文名直接 `toLowerCase()` 不影响,可用于中英文混合。
 */
export function filterVoiceNames(names: readonly string[], query: string): string[] {
  const q = (query || '').trim().toLowerCase()
  if (!q) return [...names]
  return names.filter((n) => n.toLowerCase().includes(q))
}
