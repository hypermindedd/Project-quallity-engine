"""Report renderers: JSON (machine), Markdown (human, fa/en), JUnit (CI test
reporting) and SARIF 2.1.0 (GitHub code scanning)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..model import Severity

DECISION_ICON = {"GO": "✅", "CONDITIONAL": "⚠️", "NO_GO": "⛔"}
GOAL_ICON = {"MET": "✅", "NOT_MET": "❌", "PARTIAL": "🟡", "UNVERIFIED": "⚪"}
STATUS_ICON = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥", "SKIPPED": "⏭️", "UNCALIBRATED": "📏", "NOT_COVERED": "🕳️"}

TEXT = {
    "fa": {
        "title": "گزارش کیفیت پروژه",
        "decision": "تصمیم انتشار",
        "reasons": "دلایل",
        "goals": "اهداف پروژه — آیا محقق شده‌اند؟",
        "goal": "هدف",
        "status": "وضعیت",
        "gate": "گیت سخت",
        "evidence": "شواهد",
        "coverage": "پوشش ابعاد کیفیت",
        "dimension": "بُعد",
        "checks": "چک‌ها",
        "results": "نتیجهٔ چک‌ها",
        "summary": "خلاصه",
        "time": "زمان (ثانیه)",
        "findings": "یافته‌ها",
        "none": "یافته‌ای وجود ندارد.",
        "minor": "یافته‌های جزئی و مشاهده‌ای",
        "module": "ماژول",
        "location": "محل",
        "lifecycle": "نسبت به خط مبنا",
        "next": "قدم‌های بعدی",
        "briefs": "برای هر ماژول یک برگهٔ اصلاح ساخته شد (پوشهٔ fix-briefs).",
        "uncal": "این اندازه‌گیری‌ها آستانهٔ تأییدشده ندارند؛ مقدار ثبت شد ولی قبول/رد نشد:",
        "attest": "معیارهایی که منتظر تأیید انسانی‌اند:",
        "unverified_note": "هدفِ بدون شواهدِ اجراشده «تأییدنشده» می‌ماند و هرگز محقق فرض نمی‌شود.",
        "proposed_note": "🧪 = هدف پیشنهادی هوش مصنوعی از روی مستندات؛ گزارش می‌شود ولی تا تأیید انسان، جلوی انتشار را نمی‌گیرد.",
        "yes": "بله",
        "no": "خیر",
        "meta": "پروژه",
        "engine": "نسخهٔ موتور",
        "commit": "کامیت",
        "date": "تاریخ",
    },
    "en": {
        "title": "Project quality report",
        "decision": "Release decision",
        "reasons": "Reasons",
        "goals": "Project goals — are they achieved?",
        "goal": "Goal",
        "status": "Status",
        "gate": "Hard gate",
        "evidence": "Evidence",
        "coverage": "Quality dimension coverage",
        "dimension": "Dimension",
        "checks": "Checks",
        "results": "Check results",
        "summary": "Summary",
        "time": "Time (s)",
        "findings": "Findings",
        "none": "No findings.",
        "minor": "Minor findings and observations",
        "module": "Module",
        "location": "Location",
        "lifecycle": "vs baseline",
        "next": "Next steps",
        "briefs": "A fix brief was written per module (fix-briefs folder).",
        "uncal": "These measurements have no approved threshold; recorded, not judged:",
        "attest": "Criteria waiting for human attestation:",
        "unverified_note": "A goal without executed evidence stays UNVERIFIED; it is never assumed met.",
        "proposed_note": "🧪 = proposed by the AI planner from the documents; reported, never blocking until a human accepts it.",
        "yes": "yes",
        "no": "no",
        "meta": "Project",
        "engine": "Engine",
        "commit": "Commit",
        "date": "Date",
    },
}

DIMENSION_FA = {
    "GOAL": "تحقق اهداف و ردیابی نیازمندی‌ها",
    "FUNCTIONAL": "عملکرد (تست‌های واحد و یکپارچگی)",
    "BUILD": "ساخت و کامپایل",
    "ARCHITECTURE": "انطباق با معماری",
    "CODE_QUALITY": "کیفیت کد",
    "TEST_QUALITY": "کیفیت خودِ تست‌ها",
    "API_CONTRACT": "قرارداد API",
    "API_ROBUSTNESS": "مقاومت API در برابر ورودی خصمانه",
    "E2E_FLOW": "سناریوهای سرتاسری",
    "SECURITY_SECRETS": "امنیت: کلیدهای لو رفته",
    "SECURITY_SAST": "امنیت: تحلیل ایستای کد",
    "SECURITY_DEPENDENCIES": "امنیت: وابستگی‌های آسیب‌پذیر",
    "SECURITY_RUNTIME": "امنیت در اجرا (احراز هویت، هدرها، CORS)",
    "PERFORMANCE": "سرعت پاسخ",
    "LOAD": "رفتار زیر بار",
    "UI_INTERACTION": "رابط کاربری: کلیک‌ها و خطاها",
    "UI_VISUAL": "رابط کاربری: کیفیت بصری",
    "UI_RESPONSIVE": "رابط کاربری: واکنش‌گرایی و جهت متن",
    "ACCESSIBILITY": "دسترس‌پذیری",
    "WEB_LINKS": "وب‌سایت: لینک‌ها و فایل‌های سالم",
    "WEB_SEO": "وب‌سایت: سئو و ساختار صفحه",
    "MOBILE_STABILITY": "اپ موبایل: پایداری (کرش، هنگ، زمان اجرا)",
    "PLATFORM_SECURITY": "امنیت پیکربندی اپ (مانیفست، مجوزها، ترافیک)",
    "INTEGRITY": "یکپارچگی فایل‌ها",
    "DOCUMENTATION": "سازگاری مستندات",
}


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")[:300]


def render_markdown(report: dict[str, Any]) -> str:
    lang = report.get("language", "fa")
    t = TEXT[lang]
    decision = report["decision"]["decision"]
    out: list[str] = []
    if lang == "fa":
        out.append('<div dir="rtl">\n')
    out += [
        f"# {t['title']} — {report['project']}",
        "",
        f"| {t['meta']} | {t['date']} | {t['commit']} | {t['engine']} |",
        "|---|---|---|---|",
        f"| {report['project']} | {report['finished_at']} | `{report.get('commit') or '-'}` | `{report['engine_version']}` |",
        "",
        f"## {DECISION_ICON[decision]} {t['decision']}: **{decision}**",
        "",
        *[f"- {_cell(r)}" for r in report["decision"]["reasons"]],
        "",
        f"## 🎯 {t['goals']}",
        "",
        f"> {t['unverified_note']}",
        ">",
        f"> {t['proposed_note']}",
        "",
        f"| | ID | {t['goal']} | {t['status']} | {t['gate']} | {t['evidence']} |",
        "|---|---|---|---|---|---|",
    ]
    for g in report["goals"]:
        evidence = "<br>".join(f"{c['kind']}: {_cell(c['detail'])}" for c in g["criteria"])
        title = ("🧪 " if g.get("proposed") else "") + _cell(g['title'])
        out.append(f"| {GOAL_ICON[g['status']]} | `{g['goal_id']}` | {title} | {g['status']} | {t['yes'] if g['hard_gate'] else t['no']} | {evidence} |")
    out += ["", f"## 🧭 {t['coverage']}", "", f"| | {t['dimension']} | {t['status']} | {t['checks']} |", "|---|---|---|---|"]
    for dim, info in report["coverage"].items():
        label = DIMENSION_FA.get(dim, dim) if lang == "fa" else dim
        checks = ", ".join(f"`{c}`" for c in info["checks"]) or "—"
        out.append(f"| {STATUS_ICON.get(info['state'], '')} | {label} | {info['state']} | {checks} |")
    out += ["", f"## 🔬 {t['results']}", "", f"| | Check | {t['status']} | {t['summary']} | {t['time']} |", "|---|---|---|---|---|"]
    for c in report["checks"]:
        out.append(f"| {STATUS_ICON.get(c['status'], '')} | `{c['check_id']}` | {c['status']} | {_cell(c['summary'])} | {c['duration_s']} |")
    findings = [f for c in report["checks"] for f in c["findings"]]
    major = [f for f in findings if Severity(f["severity"]).rank <= Severity.SEV2.rank]
    minor = [f for f in findings if Severity(f["severity"]).rank > Severity.SEV2.rank]
    out += ["", f"## 🐞 {t['findings']} ({len(findings)})", ""]
    if not findings:
        out.append(t["none"])
    for sev in ("SEV0", "SEV1", "SEV2"):
        group = [f for f in major if f["severity"] == sev]
        if not group:
            continue
        out += [f"### {sev} ({len(group)})", "", f"| Check | {t['findings']} | {t['module']} | {t['location']} | {t['lifecycle']} |", "|---|---|---|---|---|"]
        for f in group:
            loc = f["location"]["path"] + (f":{f['location']['line']}" if f["location"] and f["location"].get("line") else "") if f["location"] else "-"
            out.append(f"| `{f['check_id']}` | {_cell(f['title'])} | {f['module'] or '-'} | `{_cell(loc)}` | {f['lifecycle'] or '-'} |")
        out.append("")
    if minor:
        out += [f"<details><summary>{t['minor']} ({len(minor)})</summary>", "", "| Sev | Check | " + t["findings"] + " |", "|---|---|---|"]
        out += [f"| {f['severity']} | `{f['check_id']}` | {_cell(f['title'])} |" for f in minor[:300]]
        out += ["", "</details>", ""]
    out += [f"## ⏭️ {t['next']}", ""]
    if report.get("fix_briefs"):
        out.append(f"- {t['briefs']}")
    uncal = [c for c in report["checks"] if c["status"] == "UNCALIBRATED"]
    if uncal:
        out.append(f"- {t['uncal']} " + ", ".join(f"`{c['check_id']}`" for c in uncal))
    waiting = sorted({cr["reference"] for g in report["goals"] for cr in g["criteria"] if cr["kind"] == "attestation" and cr["status"] != "PASS"})
    if waiting:
        out.append(f"- {t['attest']} " + ", ".join(f"`{w}`" for w in waiting))
    if lang == "fa":
        out.append("\n</div>")
    return "\n".join(out) + "\n"


def render_junit(report: dict[str, Any]) -> str:
    suites = ET.Element("testsuites", name=f"pqe:{report['project']}")
    for check in report["checks"]:
        suite = ET.SubElement(suites, "testsuite", name=check["check_id"], tests="1",
                              failures="1" if check["status"] == "FAIL" else "0",
                              errors="1" if check["status"] == "ERROR" else "0",
                              skipped="1" if check["status"] in ("SKIPPED", "UNCALIBRATED") else "0",
                              time=str(check["duration_s"]))
        case = ET.SubElement(suite, "testcase", classname="pqe", name=check["check_id"], time=str(check["duration_s"]))
        if check["status"] == "FAIL":
            body = "\n".join(f"[{f['severity']}] {f['title']}" for f in check["findings"][:50])
            ET.SubElement(case, "failure", message=check["summary"][:500]).text = body
        elif check["status"] == "ERROR":
            ET.SubElement(case, "error", message=check["summary"][:500])
        elif check["status"] in ("SKIPPED", "UNCALIBRATED"):
            ET.SubElement(case, "skipped", message=f"{check['status']}: {check['summary']}"[:500])
    return ET.tostring(suites, encoding="unicode")


_SARIF_LEVEL = {"SEV0": "error", "SEV1": "error", "SEV2": "warning", "SEV3": "note", "INFO": "note"}


def render_sarif(report: dict[str, Any], root: Path) -> str:
    rules: dict[str, dict[str, Any]] = {}
    results = []
    for check in report["checks"]:
        for f in check["findings"]:
            loc = f.get("location") or {}
            path = loc.get("path") or ""
            if not path or not (root / path).is_file():
                continue  # SARIF locations must be repository files
            rules.setdefault(f["check_id"], {"id": f["check_id"], "name": f["check_id"], "shortDescription": {"text": check["title"]}})
            region = {"startLine": max(1, int(loc.get("line") or 1))}
            results.append({
                "ruleId": f["check_id"],
                "level": _SARIF_LEVEL[f["severity"]],
                "message": {"text": f"[{f['severity']}] {f['title']}" + (f" — {f['suggestion']}" if f.get("suggestion") else "")},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": path}, "region": region}}],
                "partialFingerprints": {"pqe/v1": f["fingerprint"]},
            })
    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "pqe", "informationUri": "https://github.com/hypermindedd/pqe01-project-quality-engine",
                                      "version": report["engine_version"], "rules": list(rules.values())}},
                  "results": results}],
    }
    return json.dumps(sarif, indent=2)
