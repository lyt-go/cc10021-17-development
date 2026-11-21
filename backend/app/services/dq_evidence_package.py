"""质量证据包服务：随指定质量报告生成可带走核对过程的质量证据包。

domain_chain: 质量证据包 (dq_evidence_package)
  └─ 取证 → 质量报告 (dq_report)
  └─ 引用 → 质量评分卡 (dq_scorecard)
  └─ 回写 → 评分卡核对清单

核心约束：
1. 只对已签发（已完成/已归档）的质量报告取证，包内明细与报告号、评分版本一一对应；
2. 只采信报告评分依据快照中、且仍处于已确认状态的评分卡及其已确认规则贡献，
   未确认分项只记录跳过原因，绝不混进证据包；
3. 导出中断后按报告快照续取，既不重复采集已签证据，也不会引入快照之外的新评分；
4. 取证结果回写评分卡「核对清单」，记录证据包实际采用的规则贡献；
5. 同一报告重复下载复用同一证据包，保证两次拿到的是同一份已确认质量报告的证据。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.dq_scorecard import CONFIRMED_STATUSES
from app.store import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DqEvidencePackageService(BaseModuleService):
    MODULE = "dq_evidence_package"
    ENTITY = "质量证据包"
    OPERATION_TERM = "取证"
    LIST_FIELDS = ["证据包编号", "报告编号", "报告版本", "评分版本范围", "证据项数",
                   "待确认项数", "生成时间", "采集状态"]
    REQUIRED_FIELDS = ["证据包编号", "报告编号"]
    STATUS_ORDER = ["取集中", "已完成", "已归档"]
    ACTION_RULES = {"继续取证": "取集中", "确认取证完成": "已完成", "归档证据包": "已归档"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 取证
    def build_quality_evidence_package(
        self,
        report_id: int,
        *,
        interrupt_after: int | None = None,
    ) -> dict[str, Any]:
        """随指定质量报告生成质量证据包。

        interrupt_after 仅用于模拟导出中断：前 N 项确认后中断，其余分项保持待确认，
        之后可由 resume_evidence_collection 续取。
        """
        report = store.find("dq_report", report_id)
        if report is None:
            raise ValueError(f"质量报告 {report_id} 不存在，无法取证")
        if report.get("status") not in CONFIRMED_STATUSES:
            raise ValueError("仅已签发（已完成/已归档）的质量报告可以生成证据包")

        # 同一报告只对应一个证据包：第二次下载复用，核对的始终是同一份已确认报告
        existing = self._find_package_by_report(report_id)
        if existing is not None:
            return self._summary(existing, reused=True)

        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        package_no = f"DQ_EP-{new_id:04d}"

        scorecard_rows = store.rows("dq_scorecard")
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        # 严格按报告生成时的评分依据快照取证，报告号 ↔ 评分版本一一对应
        for idx, ref in enumerate(report.get("评分依据", []), start=1):
            card = self._find_scorecard(scorecard_rows,
                                        str(ref.get("记录编号", "")),
                                        str(ref.get("评分版本", "")))
            item_no = f"{package_no}-{idx:02d}"
            if card is None:
                skipped.append({
                    "证据编号": item_no,
                    "记录编号": ref.get("记录编号", ""),
                    "评分版本": ref.get("评分版本", ""),
                    "跳过原因": "报告依据的评分版本不存在或未确认，未混入证据包",
                })
                continue
            if card.get("status") not in CONFIRMED_STATUSES:
                skipped.append({
                    "证据编号": item_no,
                    "记录编号": ref.get("记录编号", ""),
                    "评分版本": ref.get("评分版本", ""),
                    "跳过原因": f"评分卡状态为「{card.get('status')}」，未确认不采信",
                })
                continue
            unconfirmed = [
                c for c in card.get("规则贡献", []) if c.get("确认状态") != "已确认"
            ]
            if not card.get("规则贡献"):
                skipped.append({
                    "证据编号": item_no,
                    "记录编号": ref.get("记录编号", ""),
                    "评分版本": ref.get("评分版本", ""),
                    "跳过原因": "评分卡缺少已确认的规则贡献分项，不予采信",
                })
                continue
            if unconfirmed:
                skipped.append({
                    "证据编号": item_no,
                    "记录编号": ref.get("记录编号", ""),
                    "评分版本": ref.get("评分版本", ""),
                    "跳过原因": f"存在 {len(unconfirmed)} 条未确认规则分项，整张评分卡不予采信",
                })
                continue

            interrupted = interrupt_after is not None and idx > interrupt_after
            items.append(self._build_item(item_no, card, confirmed=not interrupted))

        package = {
            "id": new_id,
            "证据包编号": package_no,
            "报告编号": report.get("报告编号"),
            "报告版本": report.get("报告版本"),
            "报告ID": report_id,
            "评分版本范围": self._version_range(items),
            "证据项数": len(items),
            "待确认项数": sum(1 for i in items if i["确认状态"] != "已确认"),
            "生成时间": _now(),
            "采集状态": "",
            "报告快照": dict(report),
            "证据明细": items,
            "跳过分项": skipped,
            "status": "取集中" if any(i["确认状态"] != "已确认" for i in items) else "已完成",
            "pending": any(i["确认状态"] != "已确认" for i in items),
            "abnormal": False,
        }
        package["采集状态"] = package["status"]
        rows.append(package)

        # 已确认分项立即回写评分卡核对清单
        self._writeback_checklist(package)
        return self._summary(package)

    def resume_evidence_collection(self, package_id: int) -> dict[str, Any]:
        """导出中断后恢复取证：只续取包内待确认分项，不引入快照之外的新评分。"""
        package = store.find(self.MODULE, package_id)
        if package is None:
            raise ValueError(f"质量证据包 {package_id} 不存在")

        scorecard_rows = store.rows("dq_scorecard")
        resumed = 0
        for item in package["证据明细"]:
            if item["确认状态"] == "已确认":
                # 已签证据可继续获取，不重复采集
                continue
            card = self._find_scorecard(scorecard_rows,
                                        item["评分记录编号"], item["评分版本"])
            if card is None or card.get("status") not in CONFIRMED_STATUSES:
                # 来源仍未确认：保持待确认并登记跳过原因，绝不拿别的分项顶替
                reason = "恢复取证时评分版本仍未确认，继续保持待确认"
                if not any(s.get("证据编号") == item["证据编号"] for s in package["跳过分项"]):
                    package["跳过分项"].append({
                        "证据编号": item["证据编号"],
                        "记录编号": item["评分记录编号"],
                        "评分版本": item["评分版本"],
                        "跳过原因": reason,
                    })
                continue
            if not card.get("规则贡献") or any(
                    c.get("确认状态") != "已确认" for c in card.get("规则贡献", [])):
                continue
            item["规则贡献"] = [dict(c) for c in card.get("规则贡献", [])]
            item["综合得分"] = card.get("综合得分")
            item["确认状态"] = "已确认"
            item["采集时间"] = _now()
            resumed += 1

        pending = sum(1 for i in package["证据明细"] if i["确认状态"] != "已确认")
        package["待确认项数"] = pending
        package["pending"] = pending > 0
        package["status"] = "取集中" if pending else "已完成"
        package["采集状态"] = package["status"]
        # 续取确认的分项补写核对清单
        self._writeback_checklist(package)

        summary = self._summary(package)
        summary["本次续取项数"] = resumed
        return summary

    # ------------------------------------------------------------------ 导出
    def export_evidence_package(self, package_id: int) -> str | None:
        """导出证据包CSV：表头、报告汇总、评分明细、规则贡献、核对清单、跳过分项。"""
        package = store.find(self.MODULE, package_id)
        if package is None:
            return None
        report = package["报告快照"]
        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["# 质量证据包头"])
        writer.writerow(["证据包编号", "报告编号", "报告版本", "报告周期",
                         "评分版本范围", "生成时间", "采集状态"])
        writer.writerow([
            package["证据包编号"], package["报告编号"], package["报告版本"],
            report.get("报告周期"), package["评分版本范围"],
            package["生成时间"], package["采集状态"],
        ])
        writer.writerow([])

        writer.writerow(["# 报告汇总（与报告号绑定的报告版本）"])
        writer.writerow(["报告编号", "报告版本", "覆盖表数", "通过规则数",
                         "失败规则数", "趋势分析", "生成时间"])
        writer.writerow([
            report.get("报告编号"), report.get("报告版本"), report.get("覆盖表数"),
            report.get("通过规则数"), report.get("失败规则数"),
            report.get("趋势分析"), report.get("生成时间"),
        ])
        writer.writerow([])

        writer.writerow(["# 评分明细（包内明细与评分版本一一对应）"])
        writer.writerow(["证据编号", "评分记录编号", "目标表名", "评分版本",
                         "综合得分", "确认状态", "采集时间"])
        for item in package["证据明细"]:
            writer.writerow([
                item["证据编号"], item["评分记录编号"], item["目标表名"],
                item["评分版本"], item["综合得分"], item["确认状态"], item["采集时间"],
            ])
        writer.writerow([])

        writer.writerow(["# 规则贡献明细（实际采用的规则分项）"])
        writer.writerow(["证据编号", "规则类型", "权重", "校验结果",
                         "规则得分", "加权贡献", "确认状态"])
        for item in package["证据明细"]:
            for contrib in item["规则贡献"]:
                writer.writerow([
                    item["证据编号"], contrib.get("规则类型"), contrib.get("权重"),
                    contrib.get("校验结果"), contrib.get("规则得分"),
                    contrib.get("加权贡献"), contrib.get("确认状态"),
                ])
        writer.writerow([])

        writer.writerow(["# 评分卡核对清单回写记录"])
        writer.writerow(["证据包编号", "报告编号", "报告版本", "评分记录编号",
                         "评分版本", "采用规则类型", "加权贡献合计", "采集时间"])
        for item in package["证据明细"]:
            if item["确认状态"] != "已确认":
                continue
            adopted = [c.get("规则类型", "") for c in item["规则贡献"]]
            weighted_total = round(
                sum(float(c.get("加权贡献", 0)) for c in item["规则贡献"]), 2)
            writer.writerow([
                package["证据包编号"], package["报告编号"], package["报告版本"],
                item["评分记录编号"], item["评分版本"], "|".join(adopted),
                weighted_total, item["采集时间"],
            ])
        writer.writerow([])

        writer.writerow(["# 未采信分项（未确认，未混入证据包）"])
        writer.writerow(["证据编号", "记录编号", "评分版本", "跳过原因"])
        if package["跳过分项"]:
            for skip in package["跳过分项"]:
                writer.writerow([
                    skip.get("证据编号"), skip.get("记录编号"),
                    skip.get("评分版本"), skip.get("跳过原因"),
                ])
        else:
            writer.writerow(["无", "无", "无", "全部报告依据均已确认采信"])
        return output.getvalue()

    # -------------------------------------------------------------- 动作流转
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "继续取证":
            result = self.resume_evidence_collection(entry_id)
            if entry["status"] == "已完成":
                return entry, f"证据包已恢复完成，本次续取 {result['本次续取项数']} 项"
            return entry, (f"仍有 {entry['待确认项数']} 个分项未确认，"
                           "未确认分项不会混入证据包")
        if action == "确认取证完成":
            if entry.get("pending"):
                return None, (f"尚有 {entry['待确认项数']} 个待确认分项，"
                              "不能完成取证，请先继续取证")
            entry["status"] = "已完成"
            entry["采集状态"] = "已完成"
            entry["pending"] = False
            return entry, "证据包已确认完成"
        if action == "归档证据包":
            if entry["status"] != "已完成":
                return None, "仅已完成的证据包可以归档"
            entry["status"] = "已归档"
            entry["采集状态"] = "已归档"
            entry["pending"] = False
            return entry, "证据包已归档"
        return super().run_action(entry_id, action)

    # ------------------------------------------------------------------ 辅助
    def get_evidence_package(self, package_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, package_id)

    def _find_package_by_report(self, report_id: int) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if int(row.get("报告ID", -1)) == report_id:
                return row
        return None

    @staticmethod
    def _find_scorecard(scorecard_rows: list[dict], record_no: str,
                        version: str) -> dict[str, Any] | None:
        for card in scorecard_rows:
            if str(card.get("记录编号")) == record_no and \
                    str(card.get("评分版本")) == version:
                return card
        return None

    @staticmethod
    def _build_item(item_no: str, card: dict, *, confirmed: bool) -> dict[str, Any]:
        return {
            "证据编号": item_no,
            "评分记录编号": card.get("记录编号"),
            "目标表名": card.get("目标表名"),
            "评分版本": card.get("评分版本"),
            "综合得分": card.get("综合得分"),
            "规则贡献": [dict(c) for c in card.get("规则贡献", [])],
            "确认状态": "已确认" if confirmed else "待确认",
            "采集时间": _now() if confirmed else None,
        }

    @staticmethod
    def _version_range(items: list[dict]) -> str:
        versions = sorted({str(i["评分版本"]) for i in items})
        return "~".join(versions) if versions else "无已采信评分"

    def _writeback_checklist(self, package: dict[str, Any]) -> None:
        """把证据包实际采用的规则贡献回写到对应评分卡的核对清单。"""
        scorecard_rows = store.rows("dq_scorecard")
        for item in package["证据明细"]:
            if item["确认状态"] != "已确认":
                continue
            card = self._find_scorecard(scorecard_rows,
                                        item["评分记录编号"], item["评分版本"])
            if card is None:
                continue
            checklist = card.setdefault("核对清单", [])
            # 幂等：同一证据包对同一评分卡只回写一次，重复下载不产生重复清单
            if any(str(entry.get("证据包编号")) == package["证据包编号"]
                   for entry in checklist):
                continue
            checklist.append({
                "证据包编号": package["证据包编号"],
                "报告编号": package["报告编号"],
                "报告版本": package["报告版本"],
                "评分版本": item["评分版本"],
                "采用规则贡献": [
                    {
                        "规则类型": c.get("规则类型"),
                        "权重": c.get("权重"),
                        "加权贡献": c.get("加权贡献"),
                    }
                    for c in item["规则贡献"]
                ],
                "采集时间": item["采集时间"],
                "核对结论": "已确认",
            })

    def _summary(self, package: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
        return {
            "证据包编号": package["证据包编号"],
            "证据包ID": package["id"],
            "报告编号": package["报告编号"],
            "报告版本": package["报告版本"],
            "评分版本范围": package["评分版本范围"],
            "证据项数": package["证据项数"],
            "待确认项数": package["待确认项数"],
            "跳过分项数": len(package["跳过分项"]),
            "采集状态": package["status"],
            "复用已有证据包": reused,
            "生成时间": package["生成时间"],
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "取证"
        snapshot["desc"] = "随质量报告导出评分数据并生成质量证据包"
        snapshot["artifact"] = "质量证据包数据集"
        snapshot["artifact_desc"] = "与报告号、评分版本一一对应的可核对质量证据包"
        return snapshot
