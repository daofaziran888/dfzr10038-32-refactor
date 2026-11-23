"""特征报告数据共用组装：目录审计与质量阻断共用的成稿写入。

治理审计为特征报告数据提交已核实目录，质量规则交付阻断目标，
两条链路都经由本模块的共用组装写入 report_task 草稿：

- 成功时沿用原有草稿领用顺序（第一条「草稿」状态的报告），
  报告类型、特征范围、触发来源保持两类业务的原有口径；
- 成稿材料未提交时草稿保持原样，不会带上另一场处置的字段；
- 同一草稿被两类成稿同时领用时，由锁保证只有一份来源完整取得；
- 每次组装都登记到成稿清单，后续成稿可据此区分
  「审计/阻断已结束」还是「只是报告没生成」。
"""
from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass, field
from typing import Any

from app.store import store

# 成稿来源档案：触发来源的编号字段、视为「处置未结束」的状态、
# 以及成稿材料与来源字段的对应关系（材料值即报告的特征范围）。
SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "feat_catalog": {
        "key_field": "索引编号",
        "open_statuses": {"已发布"},
        "materials": {"审计对象": "目录名称"},
    },
    "quality_rule": {
        "key_field": "规则编号",
        "open_statuses": {"已阻断"},
        "materials": {"阻断特征": "目标特征"},
    },
    "govern_audit": {
        "key_field": "记录编号",
        "open_statuses": {"待审计", "审计中", "已通过"},
        "materials": {"审计对象": "审计对象"},
    },
}

RESULT_ISSUED = "已签发"
RESULT_MATERIAL_MISSING = "材料未齐"
RESULT_NO_DRAFT = "无草稿可领"


@dataclass
class AssemblyResult:
    """单次成稿组装结果。"""
    ok: bool
    reason: str
    missing: list[str] = field(default_factory=list)
    report: dict[str, Any] | None = None
    record: dict[str, Any] = field(default_factory=dict)


class ReportAssembly:
    """特征报告成稿的共用组装器：领草稿、写字段、记清单。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ledger: list[dict[str, Any]] = []

    # ---------------- 成稿写入 ----------------

    def finalize_report(
        self,
        *,
        source_module: str,
        source_entry_id: int,
        source_key: str,
        submitter: str,
        materials: dict[str, Any],
        report_type: str | None = None,
        conclusion: str | None = None,
    ) -> AssemblyResult:
        """把一场处置（目录审计/质量阻断）的成稿材料组装进特征报告草稿。

        材料未齐或没有草稿可领时不改动任何草稿，只登记成稿清单；
        成功时按原有口径写状态、特征范围、触发来源和报告类型。
        """
        missing = [
            name for name, value in materials.items()
            if not str(value or "").strip()
        ]
        with self._lock:
            if missing:
                record = self._record(
                    source_module=source_module, source_entry_id=source_entry_id,
                    source_key=source_key, submitter=submitter, materials=materials,
                    missing=missing, conclusion=conclusion,
                    result=RESULT_MATERIAL_MISSING, report=None,
                )
                return AssemblyResult(False, RESULT_MATERIAL_MISSING, missing, None, record)

            draft = self._claim_draft()
            if draft is None:
                record = self._record(
                    source_module=source_module, source_entry_id=source_entry_id,
                    source_key=source_key, submitter=submitter, materials=materials,
                    missing=missing, conclusion=conclusion,
                    result=RESULT_NO_DRAFT, report=None,
                )
                return AssemblyResult(False, RESULT_NO_DRAFT, missing, None, record)

            draft["status"] = "已生成"
            draft["pending"] = False
            if report_type is not None:
                draft["报告类型"] = report_type
            # 特征范围取成稿材料值：目录报告记审计对象，质量报告记阻断特征
            draft["特征范围"] = str(next(iter(materials.values()), ""))
            draft["最近生成"] = datetime.datetime.now().isoformat()
            draft["triggered_by"] = source_key
            record = self._record(
                source_module=source_module, source_entry_id=source_entry_id,
                source_key=source_key, submitter=submitter, materials=materials,
                missing=missing, conclusion=conclusion,
                result=RESULT_ISSUED, report=draft,
            )
            return AssemblyResult(True, RESULT_ISSUED, missing, draft, record)

    @staticmethod
    def _claim_draft() -> dict[str, Any] | None:
        """按原有领用顺序取第一条「草稿」状态的报告。"""
        for row in store.rows("report_task"):
            if row.get("status") == "草稿":
                return row
        return None

    def _record(self, *, report: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
        """把一次组装登记到成稿清单，审计结论随清单留存。"""
        record = {
            "时间": datetime.datetime.now().isoformat(),
            "触发来源": fields["source_key"],
            "来源模块": fields["source_module"],
            "来源条目": fields["source_entry_id"],
            "提交方": fields["submitter"],
            "成稿材料": dict(fields["materials"]),
            "缺失材料": list(fields["missing"]),
            "审计结论": fields["conclusion"],
            "结果": fields["result"],
            "报告编号": report.get("报告编号") if report else None,
        }
        self._ledger.append(record)
        return record

    # ---------------- 成稿查询 ----------------

    def ledger(self) -> list[dict[str, Any]]:
        """成稿清单：每次组装的登记，审计结论随清单可查。"""
        with self._lock:
            return [dict(record) for record in self._ledger]

    def assembly_status(self, module: str, entry_id: int) -> dict[str, Any]:
        """查询来源条目的成稿状态：处置是否已结束、续交资格、能否成稿。"""
        profile = SOURCE_PROFILES.get(module)
        if profile is None:
            return {
                "module": module,
                "entry_id": entry_id,
                "supported": False,
                "message": "该模块不是特征报告成稿来源",
                "attempts": [],
            }
        entry = store.find(module, entry_id)
        if entry is None:
            return {
                "module": module,
                "entry_id": entry_id,
                "supported": True,
                "found": False,
                "message": f"{module} {entry_id} 不存在或已归档",
                "attempts": [],
            }
        source_key = self._source_key(module, entry, profile)
        with self._lock:
            attempts = [dict(r) for r in self._ledger if r["触发来源"] == source_key]
        issued = any(r["结果"] == RESULT_ISSUED for r in attempts)
        source_status = str(entry.get("status", ""))
        source_open = source_status in profile["open_statuses"]
        missing = [
            name for name, field in profile["materials"].items()
            if not str(entry.get(field) or "").strip()
        ]
        draft_available = self._claim_draft() is not None
        if issued:
            disposition = "已签发"
        elif not source_open:
            disposition = "处置已结束"
        else:
            disposition = "报告未生成"
        resubmit_eligible = disposition == "报告未生成"
        return {
            "module": module,
            "entry_id": entry_id,
            "supported": True,
            "found": True,
            "source": source_key,
            "source_status": source_status,
            "source_open": source_open,
            "disposition": disposition,
            "report_issued": issued,
            "resubmit_eligible": resubmit_eligible,
            "can_finalize": resubmit_eligible and not missing and draft_available,
            "missing_materials": missing,
            "draft_available": draft_available,
            "attempts": attempts,
        }

    @staticmethod
    def _source_key(module: str, entry: dict[str, Any], profile: dict[str, Any]) -> str:
        # 治理审计的触发来源沿用审计记录上的 triggered_by（目录发布链路写入）
        if module == "govern_audit":
            triggered = str(entry.get("triggered_by") or "").strip()
            if triggered:
                return triggered
        return f"{module}/{entry.get(profile['key_field'], '')}"


report_assembly = ReportAssembly()
