"""质量报告服务：聚合巡检和质量事件数据，生成多维度质量分析报告。

domain_chain: 质量报告 (dq_report)
  └─ 聚合 → 质量趋势 (dq_trend)
  └─ 导出 → 告警通知 (dq_alert_channel)
  └─ 取证 → 质量证据包 (dq_evidence_package)

报告生成时把当时已确认评分卡的「记录编号 + 评分版本」快照到报告上，
同一报告周期再次聚合（例如补发迟到评分后重出日报）会递增报告版本；
质量证据包始终绑定生成时的报告版本与评分版本，不被后续重算覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.dq_scorecard import CONFIRMED_STATUSES
from app.store import store


class DqReportService(BaseModuleService):
    MODULE = "dq_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间", "报告版本"]
    REQUIRED_FIELDS = ["报告编号", "报告周期", "覆盖表数"]
    STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
    ACTION_RULES = {"生成报告": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
    NEGATIVE_ACTIONS = []

    def aggregate_quality_report(self, period: str = "daily") -> dict[str, Any]:
        """聚合全量巡检结果和质量事件，生成多维度质量分析报告。

        查询巡检执行记录、质量事件、评分卡和趋势数据，
        聚合各维度指标计算结果，写入质量报告记录并导出趋势快照。
        仅已确认（已完成/已归档）的评分卡计入报告，并把其评分版本快照到报告上。
        """
        # 查询各模块数据
        incident_rows = store.rows("dq_incident")
        check_rows = store.rows("dq_check_run")
        scorecard_rows = store.rows("dq_scorecard")
        trend_rows = store.rows("dq_trend")

        # 只有已确认评分卡才能作为报告依据，未确认分项不进入报告
        confirmed_cards = [
            s for s in scorecard_rows if s.get("status") in CONFIRMED_STATUSES
        ]
        # 同一记录编号族取最新确认版本（迟到评分补发后旧版本已归档）
        latest_cards = self._latest_confirmed_cards(confirmed_cards)

        # 聚合巡检结果
        total_checks = len(check_rows)
        passed_checks = sum(1 for r in check_rows if not r.get("abnormal"))
        failed_checks = total_checks - passed_checks

        # 聚合质量事件
        total_incidents = len(incident_rows)
        open_incidents = sum(1 for r in incident_rows if r.get("status") in ("待确认", "调查中"))
        resolved_incidents = sum(1 for r in incident_rows if r.get("status") in ("已修复", "已关闭"))

        # 计算质量趋势
        quality_trends = [t for t in trend_rows]
        avg_quality = 0
        quality_values = []
        for t in quality_trends:
            try:
                quality_values.append(float(t.get("综合质量", "0")))
            except (ValueError, TypeError):
                pass
        if quality_values:
            avg_quality = sum(quality_values) / len(quality_values)

        # 同一周期重新聚合时递增报告版本：已签发日报与补发评分后的新报告版本不同
        report_rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in report_rows), default=0) + 1
        report_version = self._next_period_version(period, report_rows)
        report = {
            "id": new_id,
            "报告编号": f"DQ_R-{new_id:04d}",
            "报告周期": period,
            "覆盖表数": str(len(latest_cards)),
            "通过规则数": str(passed_checks),
            "失败规则数": str(failed_checks),
            "趋势分析": "上升" if avg_quality >= 85 else "平稳" if avg_quality >= 70 else "下降",
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "报告版本": report_version,
            # 报告依据的评分版本快照：证据包按此一一对应，不随后续评分更正漂移
            "评分依据": [
                {
                    "记录编号": s.get("记录编号"),
                    "目标表名": s.get("目标表名"),
                    "评分版本": s.get("评分版本"),
                    "综合得分": s.get("综合得分"),
                }
                for s in latest_cards
            ],
            "status": "已完成",
            "pending": False,
            "abnormal": failed_checks > passed_checks,
        }
        report_rows.append(report)

        # 计算质量维度得分汇总（只统计已确认评分卡）
        dim_scores = self._compute_dimension_scores(latest_cards)

        return {
            "报告编号": report["报告编号"],
            "报告版本": report_version,
            "报告周期": period,
            "巡检总次数": total_checks,
            "巡检通过率": f"{passed_checks / total_checks * 100:.1f}%" if total_checks > 0 else "N/A",
            "质量事件总数": total_incidents,
            "未处理事件": open_incidents,
            "已修复事件": resolved_incidents,
            "修复率": f"{resolved_incidents / total_incidents * 100:.1f}%" if total_incidents > 0 else "N/A",
            "平均综合质量": round(avg_quality, 2),
            "维度得分": dim_scores,
            "评分依据": report["评分依据"],
            "生成时间": report["生成时间"],
            "趋势": report["趋势分析"],
        }

    @staticmethod
    def _latest_confirmed_cards(cards: list[dict]) -> list[dict]:
        """同一目标表名（记录族）只保留最新确认评分版本。"""
        latest: dict[str, dict] = {}
        for card in cards:
            key = str(card.get("目标表名", ""))
            current = latest.get(key)
            if current is None or int(card.get("id", 0)) > int(current.get("id", 0)):
                latest[key] = card
        return list(latest.values())

    @staticmethod
    def _next_period_version(period: str, report_rows: list[dict]) -> str:
        """同周期报告版本递增：daily 首次 v1.0，重放更正后 v2.0。"""
        max_major = 0
        for row in report_rows:
            if row.get("报告周期") != period:
                continue
            raw = str(row.get("报告版本", "v1.0")).lower().lstrip("v").split(".")[0]
            try:
                max_major = max(max_major, int(raw))
            except ValueError:
                continue
        return f"v{max_major + 1}.0"

    def _compute_dimension_scores(self, scorecard_rows: list[dict]) -> dict[str, float]:
        """计算质量维度得分汇总：完整性、准确性、时效性、唯一性。"""
        dims = {"完整性": [], "准确性": [], "时效性": []}
        for s in scorecard_rows:
            for dim_key, field in [("完整性", "完整性得分"), ("准确性", "准确性得分"), ("时效性", "时效性得分")]:
                try:
                    val = float(s.get(field, "0"))
                    dims[dim_key].append(val)
                except (ValueError, TypeError):
                    pass
        return {
            k: round(sum(v) / len(v), 1) if v else 0.0
            for k, v in dims.items()
        }

    def export_report_csv(self, report_id: int) -> str | None:
        """导出质量报告为CSV格式，包含所有维度数据和趋势标记。"""
        report = store.find(self.MODULE, report_id)
        if not report:
            return None

        lines = [
            "报告编号,报告周期,覆盖表数,通过规则数,失败规则数,趋势,生成时间,版本",
            f"{report.get('报告编号')},{report.get('报告周期')},{report.get('覆盖表数')},{report.get('通过规则数')},{report.get('失败规则数')},{report.get('趋势分析')},{report.get('生成时间')},{report.get('报告版本')}",
        ]
        return "\n".join(lines)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合巡检和质量事件数据生成质量分析报告"
        snapshot["artifact"] = "质量报告数据集"
        snapshot["artifact_desc"] = "聚合多维度质量指标的质量报告数据集"
        return snapshot
