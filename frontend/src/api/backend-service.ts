import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

/** 随指定质量报告生成质量证据包（评分数据导出），同一报告重复取证复用同一证据包。 */
export async function buildEvidencePackage(
  reportId: number,
  interruptAfter?: number,
): Promise<ActionResult> {
  const response = await request(`/api/dq_report/${reportId}/evidence-packages`, {
    method: 'POST',
    body: JSON.stringify(
      interruptAfter === undefined ? {} : { interrupt_after: interruptAfter },
    ),
  })
  if (!response.ok) throw new Error((await response.json())?.detail ?? '证据包生成失败')
  return await response.json()
}

/** 导出中断后恢复取证：已签证据继续获取，未确认分项不会混入。 */
export async function resumeEvidencePackage(packageId: number): Promise<ActionResult> {
  const response = await request(`/api/dq_evidence_package/${packageId}/resume`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('证据包恢复取证失败')
  return await response.json()
}

/** 下载证据包CSV（含表头、报告汇总、评分明细、规则贡献、核对清单、未采信分项）。 */
export async function downloadEvidencePackage(packageId: number): Promise<void> {
  const response = await request(`/api/dq_evidence_package/${packageId}/export`)
  if (!response.ok) throw new Error('证据包下载失败')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const matched = disposition.match(/filename="?([^"]+)"?/)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = matched?.[1] ?? `dq_evidence_package_${packageId}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

/** 重放同一个评分更正：补发迟到评分，生成新版本评分卡（v1.0 → v2.0）。 */
export async function correctQualityScore(
  scorecardId: number,
  correctedRuleResults: Record<string, unknown>[],
): Promise<ActionResult> {
  const response = await request(`/api/dq_scorecard/${scorecardId}/corrections`, {
    method: 'POST',
    body: JSON.stringify({ corrected_rule_results: correctedRuleResults }),
  })
  if (!response.ok) throw new Error((await response.json())?.detail ?? '评分更正失败')
  return await response.json()
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
