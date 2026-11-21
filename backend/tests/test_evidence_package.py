"""质量证据包业务链路测试。

覆盖需求：
- 新增评分数据导出，随指定质量报告生成质量证据包；
- 包内明细与报告号、评分版本一一对应；只采信已签发报告与已确认评分分项；
- 导出中断后恢复：已签证据继续获取，未确认分项不混入；
- 取证结果回写评分卡核对清单，记录实际采用的规则贡献；
- 第二次下载复用同一证据包，核对的仍是同一份已确认质量报告；
- 针对日报取证并重放同一个评分更正，核对下载文件的报告版本与评分卡内实际引用。
"""
from __future__ import annotations

import csv
import io
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.evidence import (
    aggregate_report,
    build_evidence_package,
    calculate_score,
    correct_quality_score,
    export_evidence_package,
    resume_evidence_collection,
)
from app.routers.evidence import (
    EvidenceBuildPayload,
    ReportAggregatePayload,
    ScoreCalculatePayload,
    ScoreCorrectionPayload,
)
from app.services.dq_evidence_package import DqEvidencePackageService
from app.services.dq_report import DqReportService
from app.services.dq_scorecard import DqScorecardService
from app.store import store

TABLE = "ods_daily_report"
RULE_RESULTS = [
    {"规则类型": "空值检查", "结果": "通过", "权重": 0.25},
    {"规则类型": "值域范围", "结果": "通过", "权重": 0.20},
    {"规则类型": "格式校验", "结果": "失败", "权重": 0.15},
    {"规则类型": "唯一性检查", "结果": "通过", "权重": 0.20},
    {"规则类型": "时效性检查", "结果": "通过", "权重": 0.10},
    {"规则类型": "参照完整性", "结果": "失败", "权重": 0.10},
]
# 重放同一个评分更正：迟到评分补发，两条原失败项改判通过
CORRECTED_RESULTS = [
    {"规则类型": "空值检查", "结果": "通过", "权重": 0.25},
    {"规则类型": "值域范围", "结果": "通过", "权重": 0.20},
    {"规则类型": "格式校验", "结果": "通过", "权重": 0.15},
    {"规则类型": "唯一性检查", "结果": "通过", "权重": 0.20},
    {"规则类型": "时效性检查", "结果": "通过", "权重": 0.10},
    {"规则类型": "参照完整性", "结果": "通过", "权重": 0.10},
]


@pytest.fixture()
def signed_report():
    """构造一张已确认评分卡和一份已签发日报。"""
    score_svc = DqScorecardService()
    report_svc = DqReportService()
    score = score_svc.calculate_quality_score(TABLE, [dict(r) for r in RULE_RESULTS])
    report = report_svc.aggregate_quality_report("daily")
    card = next(
        r for r in store.rows("dq_scorecard")
        if r["记录编号"] == score["记录编号"]
    )
    report_row = next(
        r for r in store.rows("dq_report") if r["报告编号"] == report["报告编号"]
    )
    return {
        "score": score,
        "card": card,
        "report": report,
        "report_row": report_row,
    }


class TestEvidencePackageBuild:
    """取证基础链路：随已签发报告生成证据包，明细与版本一一对应。"""

    def test_build_package_for_signed_report(self, signed_report):
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(signed_report["report_row"]["id"])

        assert summary["采集状态"] == "已完成"
        assert summary["待确认项数"] == 0
        assert summary["报告编号"] == signed_report["report"]["报告编号"]
        assert summary["报告版本"] == signed_report["report_row"]["报告版本"]
        # 包内至少包含本用例创建的已确认评分卡
        assert summary["证据项数"] >= 1

        package = svc.get_evidence_package(summary["证据包ID"])
        item = next(
            i for i in package["证据明细"]
            if i["评分记录编号"] == signed_report["score"]["记录编号"]
        )
        # 包内明细与评分版本一一对应
        assert item["评分版本"] == signed_report["score"]["评分版本"]
        assert item["确认状态"] == "已确认"
        # 规则贡献逐条留存，可带走核对过程
        assert len(item["规则贡献"]) == len(RULE_RESULTS)
        assert all(c["确认状态"] == "已确认" for c in item["规则贡献"])

    def test_unsigned_report_cannot_build_package(self):
        # 登记一份草稿报告（未签发）
        rows = store.rows("dq_report")
        new_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": new_id,
            "报告编号": f"DQ_R-draft-{new_id}",
            "报告周期": "daily",
            "报告版本": "v0.9",
            "评分依据": [],
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        })
        svc = DqEvidencePackageService()
        with pytest.raises(ValueError, match="已签发"):
            svc.build_quality_evidence_package(new_id)

    def test_unknown_report_raises(self):
        svc = DqEvidencePackageService()
        with pytest.raises(ValueError, match="不存在"):
            svc.build_quality_evidence_package(999_999)

    def test_unconfirmed_scorecard_is_skipped(self, signed_report):
        """报告依据里若有未确认评分卡，只登记跳过原因，绝不混进证据包。"""
        score_svc = DqScorecardService()
        # confirm=False：评分卡为草稿，规则分项待确认
        pending = score_svc.calculate_quality_score(
            "ods_pending_table", [dict(r) for r in RULE_RESULTS], confirm=False)
        pending_card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == pending["记录编号"]
        )
        assert pending_card["status"] == "草稿"

        # 把未确认评分卡塞进一份新日报的评分依据快照并签发
        report_rows = store.rows("dq_report")
        new_id = max(int(r["id"]) for r in report_rows) + 1
        report_rows.append({
            "id": new_id,
            "报告编号": f"DQ_R-pending-{new_id}",
            "报告周期": "daily-pending",
            "报告版本": "v1.0",
            "评分依据": [{
                "记录编号": pending["记录编号"],
                "目标表名": "ods_pending_table",
                "评分版本": pending["评分版本"],
                "综合得分": pending["综合得分"],
            }],
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        })

        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(new_id)
        assert summary["证据项数"] == 0
        assert summary["跳过分项数"] == 1
        package = svc.get_evidence_package(summary["证据包ID"])
        assert "未确认" in package["跳过分项"][0]["跳过原因"]
        assert package["证据明细"] == []


class TestResumeAfterInterruption:
    """导出中断恢复：已签证据继续获取，未确认分项不能混入。"""

    def test_interrupt_then_resume(self, signed_report):
        score_svc = DqScorecardService()
        # 再建两张已确认评分卡，与 fixture 的评分卡组成 3 张受控评分依据
        score2 = score_svc.calculate_quality_score(
            "dwd_trade_detail", [dict(r) for r in RULE_RESULTS])
        score3 = score_svc.calculate_quality_score(
            "dws_daily_summary", [dict(r) for r in RULE_RESULTS])

        # 自建只含 3 张受控评分卡的已签发报告，避免其他用例数据影响中断计数
        report_rows = store.rows("dq_report")
        report_id = max(int(r["id"]) for r in report_rows) + 1
        controlled_refs = [
            signed_report["score"], score2, score3,
        ]
        report_rows.append({
            "id": report_id,
            "报告编号": f"DQ_R-resume-{report_id}",
            "报告周期": "daily-resume-controlled",
            "报告版本": "v1.0",
            "评分依据": [
                {
                    "记录编号": s["记录编号"],
                    "目标表名": s["目标表名"],
                    "评分版本": s["评分版本"],
                    "综合得分": s["综合得分"],
                }
                for s in controlled_refs
            ],
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        })

        svc = DqEvidencePackageService()
        # 模拟中断：第 1 个报告依据完成确认，其余 2 个保持待确认
        summary = svc.build_quality_evidence_package(
            report_id, interrupt_after=1)
        package_id = summary["证据包ID"]
        assert summary["采集状态"] == "取集中"
        pending_count = summary["待确认项数"]
        total_items = summary["证据项数"]
        assert total_items == 3
        assert pending_count == 2

        package = svc.get_evidence_package(package_id)
        confirmed_before = [i for i in package["证据明细"] if i["确认状态"] == "已确认"]
        pending_before = [i for i in package["证据明细"] if i["确认状态"] != "已确认"]
        assert len(confirmed_before) == 1
        assert len(pending_before) == pending_count
        assert all(i["采集时间"] is None for i in pending_before)

        # 恢复取证
        resumed = svc.resume_evidence_collection(package_id)
        assert resumed["本次续取项数"] == pending_count
        assert resumed["采集状态"] == "已完成"
        assert resumed["待确认项数"] == 0

        package = svc.get_evidence_package(package_id)
        # 已签证据未被重复采集：采集时间保持首次取证时间
        first_item = next(
            i for i in package["证据明细"] if i["证据编号"].endswith("-01"))
        assert first_item["采集时间"] == confirmed_before[0]["采集时间"]
        assert all(i["确认状态"] == "已确认" for i in package["证据明细"])

    def test_resume_keeps_unconfirmed_item_out(self):
        """恢复时来源仍未确认：继续保持待确认，不拿其他分项顶替。"""
        score_svc = DqScorecardService()
        pending = score_svc.calculate_quality_score(
            "ods_blocked_table", [dict(r) for r in RULE_RESULTS], confirm=False)

        report_rows = store.rows("dq_report")
        new_id = max(int(r["id"]) for r in report_rows) + 1
        report_rows.append({
            "id": new_id,
            "报告编号": f"DQ_R-blocked-{new_id}",
            "报告周期": "daily-blocked",
            "报告版本": "v1.0",
            "评分依据": [{
                "记录编号": pending["记录编号"],
                "目标表名": "ods_blocked_table",
                "评分版本": pending["评分版本"],
                "综合得分": pending["综合得分"],
            }],
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        })
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(new_id)
        # 未确认评分卡直接跳过，证据明细为空
        assert summary["证据项数"] == 0
        package = svc.get_evidence_package(summary["证据包ID"])
        assert len(package["跳过分项"]) == 1
        # 再次恢复也不会把它混进来
        svc.resume_evidence_collection(summary["证据包ID"])
        package = svc.get_evidence_package(summary["证据包ID"])
        assert package["证据明细"] == []
        assert len(package["跳过分项"]) == 1


class TestChecklistWriteback:
    """取证结果回写评分卡核对清单。"""

    def test_checklist_records_adopted_contributions(self, signed_report):
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(signed_report["report_row"]["id"])
        card = signed_report["card"]
        checklist = card["核对清单"]
        assert len(checklist) == 1
        entry = checklist[0]
        assert entry["证据包编号"] == summary["证据包编号"]
        assert entry["报告编号"] == signed_report["report"]["报告编号"]
        assert entry["报告版本"] == signed_report["report_row"]["报告版本"]
        assert entry["评分版本"] == signed_report["score"]["评分版本"]
        assert entry["核对结论"] == "已确认"
        # 实际采用的规则贡献逐条可核对
        adopted = {c["规则类型"] for c in entry["采用规则贡献"]}
        assert adopted == {r["规则类型"] for r in RULE_RESULTS}

    def test_resume_writeback_is_idempotent(self, signed_report):
        score_svc = DqScorecardService()
        score_svc.calculate_quality_score(
            "dwd_second_table", [dict(r) for r in RULE_RESULTS])
        report_svc = DqReportService()
        report = report_svc.aggregate_quality_report("daily-writeback")
        report_row = next(
            r for r in store.rows("dq_report") if r["报告编号"] == report["报告编号"]
        )
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(
            report_row["id"], interrupt_after=1)
        svc.resume_evidence_collection(summary["证据包ID"])
        # 再恢复一次：已确认分项不重复回写
        svc.resume_evidence_collection(summary["证据包ID"])

        for card in store.rows("dq_scorecard"):
            for entry in card.get("核对清单", []):
                if entry["证据包编号"] == summary["证据包编号"]:
                    same = [
                        e for e in card["核对清单"]
                        if e["证据包编号"] == summary["证据包编号"]
                    ]
                    assert len(same) == 1


class TestRepeatedDownload:
    """第二次下载仍核对同一份已确认质量报告。"""

    def test_second_build_reuses_same_package(self, signed_report):
        svc = DqEvidencePackageService()
        first = svc.build_quality_evidence_package(signed_report["report_row"]["id"])
        second = svc.build_quality_evidence_package(signed_report["report_row"]["id"])

        assert second["证据包编号"] == first["证据包编号"]
        assert second["复用已有证据包"] is True
        assert first["复用已有证据包"] is False
        # 导出内容完全一致：同一份已确认报告、同一组评分版本
        first_csv = svc.export_evidence_package(first["证据包ID"])
        second_csv = svc.export_evidence_package(second["证据包ID"])
        assert first_csv == second_csv

    def test_late_correction_does_not_change_existing_package(self, signed_report):
        """已签日报取证后补发迟到评分：旧证据包仍引用更正前的评分版本。"""
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(signed_report["report_row"]["id"])
        old_version = signed_report["score"]["评分版本"]
        old_card_id = signed_report["card"]["id"]

        # 重放同一个评分更正
        score_svc = DqScorecardService()
        correction = score_svc.correct_quality_score(
            old_card_id, [dict(r) for r in CORRECTED_RESULTS])
        assert correction is not None
        assert correction["评分版本"] != old_version

        # 原证据包内容不变，仍绑定 v1.0 评分
        package = svc.get_evidence_package(summary["证据包ID"])
        item = next(
            i for i in package["证据明细"]
            if i["评分记录编号"] == signed_report["score"]["记录编号"]
        )
        assert item["评分版本"] == old_version
        # 原评分卡已归档，但核对清单仍保留证据包实际采用的引用
        archived = store.find("dq_scorecard", old_card_id)
        assert archived["status"] == "已归档"
        assert archived["核对清单"][0]["评分版本"] == old_version


class TestCorrectionReplayChain:
    """端到端：日报取证 → 重放同一个评分更正 → 新日报 → 核对两份报告版本与引用。"""

    def test_daily_report_correction_replay(self):
        score_svc = DqScorecardService()
        report_svc = DqReportService()
        evidence_svc = DqEvidencePackageService()

        # 1. 首版评分 + 已签日报（v1.0）
        score_v1 = score_svc.calculate_quality_score(
            TABLE, [dict(r) for r in RULE_RESULTS])
        report_v1 = report_svc.aggregate_quality_report("daily-chain")
        report_row_v1 = next(
            r for r in store.rows("dq_report")
            if r["报告编号"] == report_v1["报告编号"]
        )
        assert report_row_v1["报告版本"] == "v1.0"
        ref_v1 = next(
            ref for ref in report_row_v1["评分依据"]
            if ref["记录编号"] == score_v1["记录编号"]
        )
        assert ref_v1["评分版本"] == "v1.0"

        # 2. 审核人员下载已签日报的证据包
        pkg_v1 = evidence_svc.build_quality_evidence_package(report_row_v1["id"])
        csv_v1 = evidence_svc.export_evidence_package(pkg_v1["证据包ID"])
        assert report_row_v1["报告编号"] in csv_v1
        assert "v1.0" in csv_v1

        # 3. 同一张表补发迟到评分，重放同一个评分更正 → v2.0
        old_card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == score_v1["记录编号"]
        )
        correction = score_svc.correct_quality_score(
            old_card["id"], [dict(r) for r in CORRECTED_RESULTS])
        assert correction["评分版本"] == "v2.0"
        assert correction["原记录编号"] == score_v1["记录编号"]
        # 更正后综合得分高于原版本（全部通过）
        assert float(correction["综合得分"]) > float(score_v1["综合得分"])

        # 4. 同一周期重出日报 → 报告版本递增 v2.0，依据切到评分 v2.0
        report_v2 = report_svc.aggregate_quality_report("daily-chain")
        report_row_v2 = next(
            r for r in store.rows("dq_report")
            if r["报告编号"] == report_v2["报告编号"]
        )
        assert report_row_v2["报告版本"] == "v2.0"
        ref_v2 = next(
            ref for ref in report_row_v2["评分依据"]
            if ref["目标表名"] == TABLE
        )
        assert ref_v2["记录编号"] == correction["记录编号"]
        assert ref_v2["评分版本"] == "v2.0"

        # 5. 新日报取证：证据包明细与新报告号、评分 v2.0 一一对应
        pkg_v2 = evidence_svc.build_quality_evidence_package(report_row_v2["id"])
        assert pkg_v2["证据包编号"] != pkg_v1["证据包编号"]
        package_v2 = evidence_svc.get_evidence_package(pkg_v2["证据包ID"])
        item = next(
            i for i in package_v2["证据明细"]
            if i["评分记录编号"] == correction["记录编号"]
        )
        assert item["评分版本"] == "v2.0"
        assert package_v2["报告版本"] == "v2.0"

        # 6. 核对下载文件采用的报告版本
        csv_v2 = evidence_svc.export_evidence_package(pkg_v2["证据包ID"])
        parsed = _parse_sections(csv_v2)
        header = parsed["质量证据包头"][0]
        assert header[1] == report_row_v2["报告编号"]
        assert header[2] == "v2.0"
        summary_row = parsed["报告汇总（与报告号绑定的报告版本）"][0]
        assert summary_row[1] == "v2.0"
        # 评分明细只出现 v2.0，不混入已归档的 v1.0
        score_rows = parsed["评分明细（包内明细与评分版本一一对应）"]
        assert all(row[3] == "v2.0" for row in score_rows
                   if row[2] == TABLE)

        # 7. 评分卡内实际记录的引用：新评分卡核对清单指向新证据包/报告 v2.0
        new_card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == correction["记录编号"]
        )
        checklist_entry = next(
            e for e in new_card["核对清单"]
            if e["证据包编号"] == pkg_v2["证据包编号"]
        )
        assert checklist_entry["报告版本"] == "v2.0"
        assert checklist_entry["评分版本"] == "v2.0"

        # 8. 旧证据包仍可下载且保持报告 v1.0 / 评分 v1.0
        old_package = evidence_svc.get_evidence_package(pkg_v1["证据包ID"])
        assert old_package["报告版本"] == "v1.0"
        old_item = next(
            i for i in old_package["证据明细"]
            if i["评分记录编号"] == score_v1["记录编号"]
        )
        assert old_item["评分版本"] == "v1.0"

    def test_correction_requires_confirmed_scorecard(self):
        score_svc = DqScorecardService()
        pending = score_svc.calculate_quality_score(
            "ods_not_confirmed", [dict(r) for r in RULE_RESULTS], confirm=False)
        pending_card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == pending["记录编号"]
        )
        with pytest.raises(ValueError, match="已确认"):
            score_svc.correct_quality_score(
                pending_card["id"], [dict(r) for r in CORRECTED_RESULTS])


class TestPackageActions:
    """证据包动作流转：继续取证、确认完成、归档，以及未确认时禁止完成。"""

    def test_action_flow(self, signed_report):
        score_svc = DqScorecardService()
        score_svc.calculate_quality_score(
            "dwd_action_table", [dict(r) for r in RULE_RESULTS])
        report_svc = DqReportService()
        report = report_svc.aggregate_quality_report("daily-action")
        report_row = next(
            r for r in store.rows("dq_report")
            if r["报告编号"] == report["报告编号"]
        )
        svc = DqEvidencePackageService()
        summary = svc.build_quality_evidence_package(
            report_row["id"], interrupt_after=1)
        pid = summary["证据包ID"]

        # 取集中时不能确认完成
        entry, message = svc.run_action(pid, "确认取证完成")
        assert entry is None
        assert "待确认分项" in message

        # 继续取证后允许确认完成
        svc.run_action(pid, "继续取证")
        entry, message = svc.run_action(pid, "确认取证完成")
        assert entry is not None and entry["status"] == "已完成"

        # 归档
        entry, _ = svc.run_action(pid, "归档证据包")
        assert entry["status"] == "已归档"

        # 非法动作被拒绝
        entry, message = svc.run_action(pid, "不存在的动作")
        assert entry is None
        assert "可执行范围" in message


class TestEvidenceApi:
    """路由层：取证、续取、下载与评分更正接口（直接调用路由函数，不依赖测试客户端）。"""

    def test_full_api_flow(self, signed_report):
        report_id = signed_report["report_row"]["id"]

        # 取证
        result = build_evidence_package(report_id, EvidenceBuildPayload())
        assert result.ok is True
        package_id = result.entry["证据包ID"]

        # 详情
        package = DqEvidencePackageService().get_evidence_package(package_id)
        assert package["报告编号"] == signed_report["report"]["报告编号"]

        # 重复取证：复用同一证据包
        again = build_evidence_package(report_id, EvidenceBuildPayload())
        assert again.entry["证据包ID"] == package_id

        # 下载：返回 CSV 流式响应
        response = export_evidence_package(package_id)
        assert response.media_type == "text/csv; charset=utf-8"
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment; filename=")
        assert disposition.endswith('.csv"')
        raw = DqEvidencePackageService().export_evidence_package(package_id)
        assert raw is not None
        content = raw.lstrip("\ufeff")
        assert "质量证据包头" in content
        assert "评分卡核对清单回写记录" in content

        # 未签发报告返回 409
        rows = store.rows("dq_report")
        draft_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": draft_id,
            "报告编号": f"DQ_R-api-draft-{draft_id}",
            "报告周期": "daily",
            "报告版本": "v0.1",
            "评分依据": [],
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        })
        with pytest.raises(HTTPException) as exc:
            build_evidence_package(draft_id, EvidenceBuildPayload())
        assert exc.value.status_code == 409

    def test_resume_and_correction_api(self, signed_report):
        score_svc = DqScorecardService()
        score2 = score_svc.calculate_quality_score(
            "dwd_api_table", [dict(r) for r in RULE_RESULTS])
        report_svc = DqReportService()
        report = report_svc.aggregate_quality_report("daily-api")
        report_row = next(
            r for r in store.rows("dq_report")
            if r["报告编号"] == report["报告编号"]
        )

        # 中断式取证
        result = build_evidence_package(
            report_row["id"], EvidenceBuildPayload(interrupt_after=1))
        assert result.ok is True
        package_id = result.entry["证据包ID"]
        assert result.entry["采集状态"] == "取集中"

        # 恢复
        resumed = resume_evidence_collection(package_id)
        assert resumed.ok is True
        assert resumed.entry["采集状态"] == "已完成"

        # 评分更正接口
        card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == score2["记录编号"]
        )
        correction = correct_quality_score(
            card["id"],
            ScoreCorrectionPayload(corrected_rule_results=CORRECTED_RESULTS),
        )
        assert correction.ok is True
        assert correction.entry["评分版本"] == "v2.0"

        # 不存在的评分卡返回 404
        with pytest.raises(HTTPException) as exc:
            correct_quality_score(
                999999,
                ScoreCorrectionPayload(corrected_rule_results=CORRECTED_RESULTS),
            )
        assert exc.value.status_code == 404

        # 不存在的证据包下载返回 404
        with pytest.raises(HTTPException) as exc:
            export_evidence_package(999999)
        assert exc.value.status_code == 404

    def test_aggregate_and_calculate_routes(self, signed_report):
        """报告聚合与评分计算路由：报告携带评分依据快照，评分携带规则贡献。"""
        calc = calculate_score(ScoreCalculatePayload(
            table_name="route_demo",
            rule_results=[dict(r) for r in RULE_RESULTS],
        ))
        assert calc.ok is True
        assert calc.entry["评分版本"] == "v1.0"
        card = next(
            r for r in store.rows("dq_scorecard")
            if r["记录编号"] == calc.entry["记录编号"]
        )
        assert len(card["规则贡献"]) == len(RULE_RESULTS)

        result = aggregate_report(ReportAggregatePayload(period="daily-route"))
        assert result.ok is True
        report_row = next(
            r for r in store.rows("dq_report")
            if r["报告编号"] == result.entry["报告编号"]
        )
        refs = {ref["记录编号"]: ref["评分版本"]
                for ref in report_row["评分依据"]}
        assert refs[calc.entry["记录编号"]] == "v1.0"

    def test_unsigned_report_route_returns_409(self):
        rows = store.rows("dq_report")
        draft_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": draft_id,
            "报告编号": f"DQ_R-route-draft-{draft_id}",
            "报告周期": "daily",
            "报告版本": "v0.1",
            "评分依据": [],
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        })
        with pytest.raises(HTTPException) as exc:
            build_evidence_package(draft_id, EvidenceBuildPayload())
        assert exc.value.status_code == 409
        assert "已签发" in exc.value.detail


def _parse_sections(content: str) -> dict[str, list[list[str]]]:
    """把分节 CSV 解析成 {节标题: 数据行}。"""
    reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
    sections: dict[str, list[list[str]]] = {}
    current: str | None = None
    for row in reader:
        if not row:
            continue
        if row[0].startswith("# "):
            current = row[0][2:]
            sections[current] = []
            continue
        if current is not None and row and row[0] in (
            "证据包编号", "报告编号", "证据编号",
        ):
            # 跳过节内表头
            continue
        if current is not None:
            sections[current].append(row)
    return sections
