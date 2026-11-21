"""质量评分卡服务：计算表级数据质量多维度评分，聚合规则执行结果生成综合评分。

domain_chain: 质量评分卡 (dq_scorecard)
  └─ 计算 → 质量趋势 (dq_trend)
  └─ 聚合 → 质量报告 (dq_report)
  └─ 取证 → 质量证据包 (dq_evidence_package)

评分卡按「评分版本」留存每次计算与更正结果：迟到评分补发时生成新版本记录，
原版本归档保留，证据包始终引用生成时确认的版本，不会被后续更正覆盖。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 已确认、可被质量证据包采信的评分卡状态
CONFIRMED_STATUSES = ("已完成", "已归档")


def bump_major_version(version: str) -> str:
    """v1.0 → v2.0；无法解析的版本号按 v1.0 之后处理。"""
    try:
        major = int(str(version).lower().lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        major = 1
    return f"v{major + 1}.0"


class DqScorecardService(BaseModuleService):
    MODULE = "dq_scorecard"
    ENTITY = "评分记录"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["记录编号", "目标表名", "完整性得分", "准确性得分", "时效性得分", "综合得分", "评分时间", "评分版本"]
    REQUIRED_FIELDS = ["记录编号", "目标表名", "完整性得分"]
    STATUS_ORDER = ["草稿", "计算中", "已完成", "已归档"]
    ACTION_RULES = {"发起评分": "计算中", "确认完成": "已完成", "归档记录": "已归档"}
    NEGATIVE_ACTIONS = []

    def calculate_quality_score(self, table_name: str, rule_results: list[dict] | None = None,
                                *, confirm: bool = True) -> dict[str, Any]:
        """计算表级综合质量评分：完整性、准确性、时效性、一致性、唯一性五个维度。

        聚合该表所有质量规则执行结果，分别计算各维度得分，加权汇总为综合得分。
        每条规则的权重、得分与加权贡献按「规则贡献」分项留在评分卡上，
        供质量证据包逐条核对；显式传入的规则结果视为已确认分项。
        计算完成后将评分结果关联写入质量趋势记录。
        """
        simulated = rule_results is None
        if simulated:
            # 模拟规则执行结果
            rule_results = [
                {"规则类型": "空值检查", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.25},
                {"规则类型": "值域范围", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "格式校验", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.15},
                {"规则类型": "唯一性检查", "结果": random.choice(["通过", "通过", "通过", "通过", "失败"]), "权重": 0.20},
                {"规则类型": "时效性检查", "结果": random.choice(["通过", "通过", "失败"]), "权重": 0.10},
                {"规则类型": "参照完整性", "结果": random.choice(["通过", "通过", "通过", "失败"]), "权重": 0.10},
            ]

        evaluation = self._evaluate_rule_results(rule_results, randomize=simulated)
        dimension_scores = evaluation["dimension_scores"]
        contributions = evaluation["contributions"]
        passed = evaluation["passed"]
        overall = evaluation["overall"]
        # 系统产出的分项默认已确认；显式要求不确认时（如补发待审核评分）分项保持待确认
        for item in contributions:
            item["确认状态"] = "已确认" if confirm else "待确认"

        completeness = dimension_scores.get("空值检查", 85)
        accuracy = (dimension_scores.get("值域范围", 85) + dimension_scores.get("格式校验", 85)) / 2
        timeliness = dimension_scores.get("时效性检查", 85)

        # 写入评分卡记录
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "记录编号": f"DQ_S-{new_id:04d}",
            "目标表名": table_name,
            "完整性得分": str(round(completeness, 1)),
            "准确性得分": str(round(accuracy, 1)),
            "时效性得分": str(round(timeliness, 1)),
            "综合得分": str(round(overall, 1)),
            "评分时间": datetime.now(timezone.utc).isoformat(),
            "评分版本": "v1.0",
            "规则贡献": contributions,
            "核对清单": [],
            "status": "已完成" if confirm else "草稿",
            "pending": not confirm,
            "abnormal": overall < 60,
        }
        rows.append(entry)

        # 聚合评分结果写入趋势记录
        if confirm:
            self._write_trend_record(table_name, completeness, accuracy, timeliness, overall)

        return {
            "目标表名": table_name,
            "规则总数": len(rule_results),
            "通过数": passed,
            "通过率": f"{passed / len(rule_results) * 100:.1f}%",
            "完整性得分": round(completeness, 1),
            "准确性得分": round(accuracy, 1),
            "时效性得分": round(timeliness, 1),
            "综合得分": round(overall, 1),
            "等级": "A" if overall >= 90 else "B" if overall >= 75 else "C" if overall >= 60 else "D",
            "记录编号": entry["记录编号"],
            "评分版本": entry["评分版本"],
        }

    def correct_quality_score(self, scorecard_id: int,
                              corrected_rule_results: list[dict]) -> dict[str, Any] | None:
        """重放同一个评分更正：对已确认评分卡补发迟到评分，生成新版本评分记录。

        原评分记录不被覆盖，归档为历史版本；新记录沿用同一目标表名和记录编号族，
        评分版本递增（v1.0 → v2.0），规则贡献逐条重算并标记为已确认。
        质量证据包通过版本号区分更正前后的两份评分。
        """
        old = store.find(self.MODULE, scorecard_id)
        if old is None:
            return None
        if old.get("status") not in CONFIRMED_STATUSES:
            raise ValueError("仅已确认的评分记录可以补发评分更正")
        if not corrected_rule_results:
            raise ValueError("评分更正必须提供重放的规则结果")

        evaluation = self._evaluate_rule_results(corrected_rule_results, randomize=False)
        contributions = evaluation["contributions"]
        for item in contributions:
            item["确认状态"] = "已确认"
        dimension_scores = evaluation["dimension_scores"]
        overall = evaluation["overall"]

        completeness = dimension_scores.get("空值检查", 85)
        accuracy = (dimension_scores.get("值域范围", 85) + dimension_scores.get("格式校验", 85)) / 2
        timeliness = dimension_scores.get("时效性检查", 85)
        new_version = bump_major_version(str(old.get("评分版本", "v1.0")))

        # 原版本归档留存，证据包已引用的历史版本不受影响
        old["status"] = "已归档"
        old["pending"] = False
        old["被替代版本"] = new_version

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": new_id,
            "记录编号": f"DQ_S-{new_id:04d}",
            "原记录编号": old.get("记录编号"),
            "目标表名": old.get("目标表名"),
            "完整性得分": str(round(completeness, 1)),
            "准确性得分": str(round(accuracy, 1)),
            "时效性得分": str(round(timeliness, 1)),
            "综合得分": str(round(overall, 1)),
            "评分时间": datetime.now(timezone.utc).isoformat(),
            "评分版本": new_version,
            "规则贡献": contributions,
            "核对清单": [],
            "更正来源记录": old.get("记录编号"),
            "更正时间": datetime.now(timezone.utc).isoformat(),
            "status": "已完成",
            "pending": False,
            "abnormal": overall < 60,
        }
        rows.append(entry)
        self._write_trend_record(str(old.get("目标表名")), completeness, accuracy,
                                 timeliness, overall)

        return {
            "原记录编号": old.get("记录编号"),
            "记录编号": entry["记录编号"],
            "目标表名": entry["目标表名"],
            "评分版本": new_version,
            "综合得分": round(overall, 1),
            "规则贡献数": len(contributions),
        }

    def _evaluate_rule_results(self, rule_results: list[dict], *,
                               randomize: bool) -> dict[str, Any]:
        """按规则结果计算维度得分与每条规则的加权贡献，显式结果下结果可重放。"""
        dimension_scores: dict[str, float] = {}
        contributions: list[dict[str, Any]] = []
        passed = 0
        total_weight = 0.0
        for rule in rule_results:
            dim = rule["规则类型"]
            weight = float(rule["权重"])
            if rule["结果"] == "通过":
                score = 100.0
                passed += 1
            elif "得分" in rule:
                score = float(rule["得分"])
            elif randomize:
                score = random.uniform(20, 60)
            else:
                score = 40.0
            weighted = round(weight * score, 2)
            dimension_scores[dim] = round(score, 1)
            total_weight += weighted
            contributions.append({
                "规则类型": dim,
                "权重": weight,
                "校验结果": rule["结果"],
                "规则得分": round(score, 1),
                "加权贡献": weighted,
            })
        return {
            "dimension_scores": dimension_scores,
            "contributions": contributions,
            "passed": passed,
            "overall": round(total_weight, 1),
        }

    def _write_trend_record(self, table_name: str, completeness: float, accuracy: float,
                            timeliness: float, overall: float) -> None:
        """聚合评分数据写入质量趋势记录。"""
        trend_rows = store.rows("dq_trend")
        new_tid = max((int(r.get("id", 0)) for r in trend_rows), default=0) + 1
        trend_rows.append({
            "id": new_tid,
            "记录编号": f"DQ_T-{new_tid:04d}",
            "目标表名": table_name,
            "日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完备率": f"{completeness:.1f}%",
            "准确率": f"{accuracy:.1f}%",
            "及时率": f"{timeliness:.1f}%",
            "综合质量": f"{overall:.1f}",
            "趋势标记": "正常" if overall >= 75 else "下降趋势",
            "status": "正常" if overall >= 75 else "下降趋势",
            "pending": False,
            "abnormal": overall < 60,
        })

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "计算"
        snapshot["desc"] = "计算表级多维度质量评分并聚合为评分卡"
        snapshot["artifact"] = "质量评分卡数据集"
        snapshot["artifact_desc"] = "聚合质量规则执行结果的评分卡数据集"
        return snapshot
