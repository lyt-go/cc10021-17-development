"""质量证据包路由：评分数据导出、证据包取证/续取/下载，以及评分更正重放。

与通用资源路由分开注册，路径固定且放在资源路由之前，避免被 /{module} 动态匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.dq_evidence_package import DqEvidencePackageService
from app.services.dq_report import DqReportService
from app.services.dq_scorecard import DqScorecardService

router = APIRouter(prefix="/api", tags=["质量证据包"])


class EvidenceBuildPayload(BaseModel):
    interrupt_after: int | None = Field(default=None, ge=0)


class ScoreCorrectionPayload(BaseModel):
    corrected_rule_results: list[dict[str, Any]]


class ReportAggregatePayload(BaseModel):
    period: str = "daily"


class ScoreCalculatePayload(BaseModel):
    table_name: str
    rule_results: list[dict[str, Any]] | None = None


@router.post("/dq_report/aggregate", response_model=ActionResult)
def aggregate_report(payload: ReportAggregatePayload) -> ActionResult:
    """聚合生成质量报告（含评分依据版本快照）。"""
    report = DqReportService().aggregate_quality_report(payload.period)
    return ActionResult(ok=True, message="质量报告已生成", entry=report)


@router.post("/dq_scorecard/calculate", response_model=ActionResult)
def calculate_score(payload: ScoreCalculatePayload) -> ActionResult:
    """计算表级质量评分，逐条留存已确认的规则贡献。"""
    result = DqScorecardService().calculate_quality_score(
        payload.table_name, payload.rule_results)
    return ActionResult(ok=True, message="质量评分已计算", entry=result)


@router.post("/dq_report/{report_id}/evidence-packages", response_model=ActionResult)
def build_evidence_package(report_id: int, payload: EvidenceBuildPayload) -> ActionResult:
    """随指定质量报告生成质量证据包（评分数据导出）。"""
    service = DqEvidencePackageService()
    try:
        summary = service.build_quality_evidence_package(
            report_id, interrupt_after=payload.interrupt_after)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(ok=True, message="质量证据包已生成", entry=summary)


@router.get("/dq_evidence_package/{package_id}", response_model=dict)
def get_evidence_package(package_id: int) -> dict[str, Any]:
    service = DqEvidencePackageService()
    package = service.get_evidence_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"质量证据包 {package_id} 不存在")
    return package


@router.post("/dq_evidence_package/{package_id}/resume", response_model=ActionResult)
def resume_evidence_collection(package_id: int) -> ActionResult:
    """导出中断后恢复取证：已签证据继续获取，未确认分项不混入。"""
    service = DqEvidencePackageService()
    try:
        summary = service.resume_evidence_collection(package_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message="证据包恢复取证完成", entry=summary)


@router.get("/dq_evidence_package/{package_id}/export")
def export_evidence_package(package_id: int) -> StreamingResponse:
    service = DqEvidencePackageService()
    content = service.export_evidence_package(package_id)
    if content is None:
        raise HTTPException(status_code=404, detail=f"质量证据包 {package_id} 不存在")
    package = service.get_evidence_package(package_id)
    filename = f"{package['证据包编号']}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/dq_scorecard/{scorecard_id}/corrections", response_model=ActionResult)
def correct_quality_score(scorecard_id: int,
                          payload: ScoreCorrectionPayload) -> ActionResult:
    """重放同一个评分更正：补发迟到评分，生成新版本评分卡。"""
    service = DqScorecardService()
    try:
        result = service.correct_quality_score(
            scorecard_id, payload.corrected_rule_results)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"评分记录 {scorecard_id} 不存在")
    return ActionResult(ok=True, message="评分更正已重放并生成新版本", entry=result)
