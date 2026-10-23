"""Mobile applications in a simulated environment.

Android (native, Flutter or React Native builds):

* ``android.manifest``      static audit of AndroidManifest.xml (debuggable, backup,
                            cleartext traffic, exported components, dangerous permissions)
* ``android.build``         debug APK builds; size recorded
* ``android.unit``          JVM unit tests (JUnit XML)
* ``android.lint``          Android Lint errors and warnings
* ``android.launch``        installs on the emulator, launches, measures cold start
* ``android.monkey``        thousands of random user events; any crash or ANR is a defect
* ``android.instrumented``  on-device tests (connectedAndroidTest)
* ``android.maestro``       Maestro user-flow files, when present

iOS:

* ``ios.plist``             App Transport Security audit
* ``ios.test``              xcodebuild test on the iOS Simulator

Device checks need an emulator/simulator. Where none exists (a laptop, a Linux
web job) they are SKIPPED with the reason; the CI workflow runs them in the
Android-emulator and macOS jobs.
"""

from __future__ import annotations

import glob
import os
import plistlib
import re
import sys
import xml.etree.ElementTree as ET  # noqa: S405
from pathlib import Path

from ..context import ALWAYS_EXCLUDED_DIRS, RunContext, ToolMissing
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status, TestCase
from .base import Runner, register
from .functional import parse_junit

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
DANGEROUS_PERMISSIONS = {
    "READ_CONTACTS", "WRITE_CONTACTS", "READ_CALENDAR", "WRITE_CALENDAR", "CAMERA", "RECORD_AUDIO",
    "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION", "READ_PHONE_STATE",
    "CALL_PHONE", "READ_CALL_LOG", "WRITE_CALL_LOG", "SEND_SMS", "RECEIVE_SMS", "READ_SMS", "BODY_SENSORS",
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "POST_NOTIFICATIONS",
    "MANAGE_EXTERNAL_STORAGE", "SYSTEM_ALERT_WINDOW", "REQUEST_INSTALL_PACKAGES",
}


# --------------------------------------------------------------------------- #
# Pure parsers (unit-tested without a device)
# --------------------------------------------------------------------------- #


def audit_manifest(text: str, rel: str) -> tuple[list[Finding], dict[str, object]]:
    cid, dim = "android.manifest", Dimension.PLATFORM_SECURITY
    root = ET.fromstring(text)  # noqa: S314  # nosec B314 - project source file
    findings: list[Finding] = []
    app = root.find("application")
    perms = sorted({(p.get(f"{ANDROID_NS}name") or "").rsplit(".", 1)[-1] for p in root.iter("uses-permission")})
    dangerous = [p for p in perms if p in DANGEROUS_PERMISSIONS]

    def add(sev: Severity, title: str, key: str, hint: str) -> None:
        findings.append(Finding(cid, dim, sev, title, location=Location(rel), evidence={"key": key}, suggestion=hint))

    if app is not None:
        if app.get(f"{ANDROID_NS}debuggable") == "true":
            add(Severity.SEV1, "android:debuggable=\"true\" is hard-coded in the manifest", "debuggable",
                "Remove it; the build type sets it. A debuggable release lets anyone attach a debugger and read app memory.")
        if app.get(f"{ANDROID_NS}usesCleartextTraffic") == "true":
            add(Severity.SEV2, "Cleartext (HTTP) traffic is allowed app-wide", "cleartext",
                "Use HTTPS, or scope exceptions with a network security config.")
        if app.get(f"{ANDROID_NS}allowBackup", "true") == "true":
            add(Severity.SEV3, "android:allowBackup is enabled (app data can be extracted via backup)", "allowBackup",
                "Set allowBackup=\"false\" or define dataExtractionRules for sensitive data.")
        for tag in ("activity", "service", "receiver", "provider"):
            for comp in app.iter(tag):
                if comp.get(f"{ANDROID_NS}exported") != "true" or comp.get(f"{ANDROID_NS}permission"):
                    continue
                launcher = any(c.get(f"{ANDROID_NS}name", "").endswith("LAUNCHER") for c in comp.iter("category"))
                if launcher:
                    continue
                name = comp.get(f"{ANDROID_NS}name", "?")
                add(Severity.SEV2, f"Exported {tag} without a permission: {name}", f"exported:{name}",
                    "Any app on the device can start or bind it. Set exported=\"false\" or require a permission.")
    for perm in dangerous:
        add(Severity.INFO, f"Dangerous permission requested: {perm}", f"perm:{perm}",
            "Confirm the product documents need it; request it at runtime with a clear rationale.")
    return findings, {"permissions": perms, "dangerous_permissions": dangerous}


def parse_monkey(output: str) -> dict[str, object]:
    crashes = re.findall(r"// CRASH: (.+)", output)
    anrs = re.findall(r"// NOT RESPONDING: (.+)", output)
    long_msg = re.findall(r"// Long Msg: (.+)", output)
    completed = re.search(r"Events injected: (\d+)", output)
    return {
        "crashes": crashes,
        "anrs": anrs,
        "messages": long_msg[:5],
        "events_injected": int(completed.group(1)) if completed else 0,
        "aborted": "Monkey aborted" in output,
    }


def parse_start_time(output: str) -> int | None:
    match = re.search(r"TotalTime: (\d+)", output)
    return int(match.group(1)) if match else None


def parse_xcodebuild(output: str) -> list[TestCase]:
    cases = []
    for match in re.finditer(r"Test [Cc]ase '-?\[?([\w.]+)[ .](\w+)\]?' (passed|failed|skipped)[^\n]*?(?:\(([\d.]+) seconds\))?", output):
        suite, name, verdict, seconds = match.groups()
        status = {"passed": Status.PASS, "failed": Status.FAIL, "skipped": Status.SKIPPED}[verdict]
        cases.append(TestCase(f"{suite}::{name}", suite, name, status, float(seconds or 0)))
    return cases


def audit_plist(data: dict, rel: str) -> list[Finding]:
    findings = []
    ats = data.get("NSAppTransportSecurity") or {}
    if ats.get("NSAllowsArbitraryLoads") is True:
        findings.append(Finding("ios.plist", Dimension.PLATFORM_SECURITY, Severity.SEV2,
                                "App Transport Security disabled (NSAllowsArbitraryLoads = true)", location=Location(rel),
                                evidence={"key": f"{rel}:ats"}, suggestion="Use HTTPS and add narrowly scoped exception domains only."))
    for key in ("NSCameraUsageDescription", "NSLocationWhenInUseUsageDescription", "NSMicrophoneUsageDescription", "NSPhotoLibraryUsageDescription"):
        if key in data and not str(data[key]).strip():
            findings.append(Finding("ios.plist", Dimension.PLATFORM_SECURITY, Severity.SEV2, f"{key} is empty (App Store rejection)",
                                    location=Location(rel), evidence={"key": f"{rel}:{key}"}))
    return findings


def _files(ctx: RunContext, base: str, pattern: str) -> list[Path]:
    return sorted(p for p in (ctx.root / base).glob(pattern)
                  if p.is_file() and not any(part in ALWAYS_EXCLUDED_DIRS - {"build"} for part in p.parts))


# --------------------------------------------------------------------------- #
# Android
# --------------------------------------------------------------------------- #


@register
class AndroidRunner(Runner):
    id = "android"
    title = "Android application"
    dimension = Dimension.MOBILE_STABILITY
    phase = 46

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.android.enabled else "android checks not enabled in contract"

    @staticmethod
    def _step(cid: str, title: str, dim: Dimension, fn, *args):  # noqa: ANN001, ANN205 - small local adapter
        """One missing tool skips only the step that needs it, never the whole suite."""
        try:
            return fn(*args)
        except ToolMissing as exc:
            return CheckResult.skipped(cid, title, dim, f"required tool not installed: {exc}")

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        spec = ctx.config.android
        results = [self._manifest(ctx)]
        apk = None
        if spec.build_command:
            built = self._step("android.build", "Debug APK builds", Dimension.BUILD, self._build, ctx)
            build, apk = built if isinstance(built, tuple) else (built, None)
            results.append(build)
        results.append(self._step("android.unit", "JVM unit tests", Dimension.FUNCTIONAL, self._junit_command, ctx, "android.unit",
                                  "JVM unit tests", spec.unit_test_command, "**/build/test-results/**/*.xml", Dimension.FUNCTIONAL))
        results.append(self._step("android.lint", "Android Lint", Dimension.CODE_QUALITY, self._lint, ctx))
        device = self._device(ctx)
        reason = "no Android emulator or device attached (runs in the Android CI job)"
        if not device:
            for cid, title in (("android.launch", "Install, launch and cold-start time"), ("android.monkey", "Random-event stress (crashes, ANRs)"),
                               ("android.instrumented", "On-device tests"), ("android.maestro", "Maestro user flows")):
                results.append(CheckResult.skipped(cid, title, Dimension.MOBILE_STABILITY, reason))
            return results
        package = spec.package
        launch = self._launch(ctx, apk, package)
        results.append(launch)
        if launch.status in (Status.PASS, Status.UNCALIBRATED):
            results.append(self._monkey(ctx, package))
        else:
            results.append(CheckResult.skipped("android.monkey", "Random-event stress", Dimension.MOBILE_STABILITY, "app could not be launched"))
        results.append(self._step("android.instrumented", "On-device tests", Dimension.FUNCTIONAL, self._junit_command, ctx,
                                  "android.instrumented", "On-device tests", spec.instrumented_command,
                                  "**/build/outputs/androidTest-results/**/*.xml", Dimension.FUNCTIONAL))
        results.append(self._step("android.maestro", "Maestro user flows", Dimension.E2E_FLOW, self._maestro, ctx))
        return results

    # ---------------------------------------------------------------- static
    def _manifest(self, ctx: RunContext) -> CheckResult:
        spec = ctx.config.android
        title = "Manifest security and permissions"
        candidates = [ctx.root / spec.manifest] if spec.manifest else _files(ctx, spec.project_dir, "**/src/main/AndroidManifest.xml")
        candidates = [c for c in candidates if c.exists() and "/build/" not in c.as_posix()]
        if not candidates:
            return CheckResult.skipped("android.manifest", title, Dimension.PLATFORM_SECURITY, "no AndroidManifest.xml found")
        result = CheckResult("android.manifest", title, Dimension.PLATFORM_SECURITY, Status.PASS)
        for path in candidates[:5]:
            try:
                findings, metrics = audit_manifest(path.read_text(encoding="utf-8"), ctx.rel(path))
            except ET.ParseError as exc:
                findings, metrics = [Finding("android.manifest", Dimension.PLATFORM_SECURITY, Severity.SEV1, "Manifest is not valid XML",
                                             detail=str(exc), location=Location(ctx.rel(path)))], {}
            result.findings += findings
            result.metrics[ctx.rel(path)] = metrics
        blocking = [f for f in result.findings if f.severity != Severity.INFO]
        result.status = Status.FAIL if blocking else Status.PASS
        result.summary = f"{len(blocking)} issue(s), {len(result.findings) - len(blocking)} permission observation(s)"
        return result

    def _build(self, ctx: RunContext) -> tuple[CheckResult, Path | None]:
        spec = ctx.config.android
        proc = ctx.run(spec.build_command, cwd=spec.project_dir, timeout_s=spec.timeout_s)
        (ctx.out_dir / "android-build.log").write_text(ctx.redact(proc.stdout + proc.stderr), encoding="utf-8")
        apks = sorted(Path(p) for p in glob.glob(str(ctx.root / spec.project_dir / spec.apk_glob), recursive=True))
        result = CheckResult("android.build", "Debug APK builds", Dimension.BUILD, Status.PASS, artifacts=["android-build.log"])
        if not proc.ok or not apks:
            result.status = Status.FAIL
            result.summary = "build failed" if not proc.ok else f"build succeeded but no APK matches {spec.apk_glob}"
            result.findings.append(Finding("android.build", Dimension.BUILD, Severity.SEV1, "Android build failed", detail=proc.tail(30)))
            return result, None
        apk = apks[-1]
        result.metrics = {"apk": ctx.rel(apk), "apk_mb": round(apk.stat().st_size / 1_048_576, 2)}
        result.summary = f"{ctx.rel(apk)} ({result.metrics['apk_mb']} MB)"
        return result, apk

    def _junit_command(self, ctx: RunContext, cid: str, title: str, cmd: list[str], pattern: str, dim: Dimension) -> CheckResult:
        spec = ctx.config.android
        if not cmd:
            return CheckResult.skipped(cid, title, dim, "no command declared")
        proc = ctx.run(cmd, cwd=spec.project_dir, timeout_s=spec.timeout_s)
        log = f"{cid.replace('.', '-')}.log"
        (ctx.out_dir / log).write_text(ctx.redact(proc.stdout + proc.stderr), encoding="utf-8")
        result = CheckResult(cid, title, dim, Status.PASS, artifacts=[log])
        for xml in _files(ctx, spec.project_dir, pattern):
            try:
                result.tests += parse_junit(xml)
            except ET.ParseError:
                continue
        failed = [t for t in result.tests if t.status == Status.FAIL]
        for t in failed:
            result.findings.append(Finding(cid, dim, Severity.SEV1, f"Test failed: {t.id}", detail=t.message, evidence={"key": t.id}))
        if not proc.ok and not failed:
            result.findings.append(Finding(cid, dim, Severity.SEV1, f"{title}: command failed", detail=proc.tail(25)))
        result.status = Status.FAIL if result.findings else (Status.PASS if result.tests else Status.ERROR)
        result.summary = f"{len(result.tests)} test(s), {len(failed)} failed" if result.tests else (proc.tail(3) or "no test results produced")
        return result

    def _lint(self, ctx: RunContext) -> CheckResult:
        spec = ctx.config.android
        if not spec.lint_command:
            return CheckResult.skipped("android.lint", "Android Lint", Dimension.CODE_QUALITY, "no lint command declared")
        proc = ctx.run(spec.lint_command, cwd=spec.project_dir, timeout_s=spec.timeout_s)
        result = CheckResult("android.lint", "Android Lint", Dimension.CODE_QUALITY, Status.PASS)
        reports = _files(ctx, spec.project_dir, "**/build/reports/lint-results*.xml")
        if not reports and proc.ok:
            return CheckResult.errored(result.check_id, result.title, result.dimension,
                                       "lint finished but produced no lint-results XML; nothing was verified")
        for xml in reports:
            try:
                tree = ET.parse(xml)  # noqa: S314  # nosec B314 - generated by our own build
            except ET.ParseError:
                continue
            for issue in tree.iter("issue"):
                sev = {"Fatal": Severity.SEV1, "Error": Severity.SEV2, "Warning": Severity.SEV3}.get(issue.get("severity", ""), Severity.INFO)
                loc = issue.find("location")
                path = ctx.rel(Path(loc.get("file", ""))) if loc is not None else ""
                line = int(loc.get("line")) if loc is not None and (loc.get("line") or "").isdigit() else None
                result.findings.append(Finding("android.lint", Dimension.CODE_QUALITY, sev, f"{issue.get('id')}: {issue.get('message', '')[:160]}",
                                               location=Location(path, line), evidence={"key": f"{issue.get('id')}:{path}:{line}"}))
        if not proc.ok and not result.findings:
            result.findings.append(Finding("android.lint", Dimension.CODE_QUALITY, Severity.SEV2, "Lint command failed", detail=proc.tail(20)))
        result.status = Status.FAIL if any(f.severity.rank <= Severity.SEV2.rank for f in result.findings) else Status.PASS
        result.summary = f"{len(result.findings)} lint issue(s)"
        return result

    # ---------------------------------------------------------------- device
    @staticmethod
    def _device(ctx: RunContext) -> str:
        if not ctx.which("adb"):
            return ""
        proc = ctx.run(["adb", "devices"], timeout_s=30)
        for line in proc.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                return parts[0]
        return ""

    def _launch(self, ctx: RunContext, apk: Path | None, package: str) -> CheckResult:
        spec = ctx.config.android
        result = CheckResult("android.launch", "Install, launch and cold-start time", Dimension.MOBILE_STABILITY, Status.PASS)
        if not package:
            return CheckResult.errored(result.check_id, result.title, result.dimension, "android.package (applicationId) is unknown")
        if apk is not None:
            install = ctx.run(["adb", "install", "-r", "-t", str(apk)], timeout_s=600)
            if "Success" not in install.stdout:
                result.findings.append(Finding(result.check_id, result.dimension, Severity.SEV1, "APK failed to install", detail=install.tail(10)))
                result.status = Status.FAIL
                return result
        resolve = ctx.run(["adb", "shell", "cmd", "package", "resolve-activity", "--brief", package], timeout_s=60)
        component = next((ln.strip() for ln in reversed(resolve.stdout.splitlines()) if "/" in ln), "")
        if not component:
            result.findings.append(Finding(result.check_id, result.dimension, Severity.SEV1, f"No launchable activity for {package}", detail=resolve.tail(5)))
            result.status = Status.FAIL
            return result
        times = []
        ctx.run(["adb", "logcat", "-c"], timeout_s=30)
        for _ in range(max(1, spec.startup_samples)):
            ctx.run(["adb", "shell", "am", "force-stop", package], timeout_s=30)
            start = ctx.run(["adb", "shell", "am", "start", "-W", "-n", component], timeout_s=120)
            value = parse_start_time(start.stdout)
            if value is not None:
                times.append(value)
        crash = ctx.run(["adb", "logcat", "-d", "-b", "crash"], timeout_s=60).stdout
        if "FATAL EXCEPTION" in crash:
            result.findings.append(Finding(result.check_id, result.dimension, Severity.SEV1, "App crashes on launch",
                                           detail=crash[-2500:], evidence={"key": "launch-crash"}))
        result.metrics = {"component": component, "cold_start_ms": times, "cold_start_ms_max": max(times) if times else None}
        result.tests.append(TestCase("android.launch::cold-start", package, "cold start", Status.PASS if times and not result.findings else Status.FAIL))
        if result.findings or not times:
            result.status = Status.FAIL
            result.summary = "launch failed" if not times else "crash during launch"
        else:
            result.status = Status.UNCALIBRATED
            result.summary = f"cold start {max(times)} ms (worst of {len(times)}); no approved threshold"
        return result

    def _monkey(self, ctx: RunContext, package: str) -> CheckResult:
        spec = ctx.config.android
        events = int(os.environ.get("PQE_MONKEY_EVENTS") or spec.monkey_events)
        result = CheckResult("android.monkey", f"Random-event stress ({events} events)", Dimension.MOBILE_STABILITY, Status.PASS)
        ctx.run(["adb", "logcat", "-c"], timeout_s=30)
        proc = ctx.run(["adb", "shell", "monkey", "-p", package, "-s", str(spec.monkey_seed), "--throttle", str(spec.monkey_throttle_ms),
                        "--pct-syskeys", "0", "--ignore-security-exceptions", "-v", "-v", str(events)], timeout_s=spec.timeout_s)
        (ctx.out_dir / "android-monkey.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        crash_log = ctx.run(["adb", "logcat", "-d", "-b", "crash"], timeout_s=60).stdout
        (ctx.out_dir / "android-crash.log").write_text(crash_log, encoding="utf-8")
        data = parse_monkey(proc.stdout)
        result.metrics = {k: v for k, v in data.items() if k != "messages"} | {"seed": spec.monkey_seed}
        result.artifacts = ["android-monkey.log", "android-crash.log"]
        for crash in data["crashes"]:  # type: ignore[union-attr]
            result.findings.append(Finding(result.check_id, result.dimension, Severity.SEV1, f"Crash under random input: {crash[:120]}",
                                           detail=crash_log[-2500:] or "\n".join(data["messages"]),  # type: ignore[arg-type]
                                           evidence={"key": f"monkey-crash:{crash[:80]}", "seed": spec.monkey_seed},
                                           suggestion=f"Reproduce with the same seed: adb shell monkey -p {package} -s {spec.monkey_seed} -v {events}"))
        for anr in data["anrs"]:  # type: ignore[union-attr]
            result.findings.append(Finding(result.check_id, result.dimension, Severity.SEV1, f"App not responding (ANR): {anr[:120]}",
                                           evidence={"key": f"monkey-anr:{anr[:80]}", "seed": spec.monkey_seed},
                                           suggestion="Move blocking work (disk, network, heavy computation) off the main thread."))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{data['events_injected']} events injected, {len(data['crashes'])} crash(es), {len(data['anrs'])} ANR(s)"  # type: ignore[arg-type]
        return result

    def _maestro(self, ctx: RunContext) -> CheckResult:
        spec = ctx.config.android
        title = "Maestro user flows"
        flows = ctx.root / spec.project_dir / spec.maestro_dir
        if not flows.is_dir():
            return CheckResult.skipped("android.maestro", title, Dimension.E2E_FLOW, f"no {spec.maestro_dir}/ flows in the project")
        if not ctx.which("maestro"):
            return CheckResult.skipped("android.maestro", title, Dimension.E2E_FLOW, "maestro CLI not installed")
        junit = ctx.out_dir / "maestro-junit.xml"
        proc = ctx.run(["maestro", "test", str(flows), "--format", "junit", "--output", str(junit)], timeout_s=spec.timeout_s)
        result = CheckResult("android.maestro", title, Dimension.E2E_FLOW, Status.PASS, artifacts=["maestro-junit.xml"])
        result.tests = parse_junit(junit) if junit.exists() else []
        for t in result.tests:
            if t.status == Status.FAIL:
                result.findings.append(Finding("android.maestro", Dimension.E2E_FLOW, Severity.SEV1, f"Flow failed: {t.name}", detail=t.message, evidence={"key": t.id}))
        if not proc.ok and not result.findings:
            result.findings.append(Finding("android.maestro", Dimension.E2E_FLOW, Severity.SEV1, "Maestro run failed", detail=proc.tail(20)))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.tests)} flow(s), {len(result.findings)} failed"
        return result


# --------------------------------------------------------------------------- #
# iOS
# --------------------------------------------------------------------------- #


@register
class IosRunner(Runner):
    id = "ios"
    title = "iOS application"
    dimension = Dimension.MOBILE_STABILITY
    phase = 46

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.ios.enabled else "ios checks not enabled in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        spec = ctx.config.ios
        results = [self._plist(ctx)]
        if sys.platform != "darwin" or not ctx.which("xcodebuild"):
            results.append(CheckResult.skipped("ios.test", "Build and test on the iOS Simulator", Dimension.FUNCTIONAL,
                                               "needs macOS with Xcode (runs in the macOS CI job)"))
            return results
        action = "test" if spec.run_tests else "build"
        bundle = ctx.out_dir / "ios.xcresult"
        proc = ctx.run(["xcodebuild", *spec.xcodebuild_args, "-destination", spec.destination, "-resultBundlePath", str(bundle), action],
                       cwd=spec.project_dir, timeout_s=spec.timeout_s)
        (ctx.out_dir / "ios-xcodebuild.log").write_text(proc.stdout[-2_000_000:] + proc.stderr[-200_000:], encoding="utf-8")
        result = CheckResult("ios.test", f"xcodebuild {action} on the iOS Simulator", Dimension.FUNCTIONAL, Status.PASS,
                             artifacts=["ios-xcodebuild.log"], tests=parse_xcodebuild(proc.stdout))
        for t in result.tests:
            if t.status == Status.FAIL:
                result.findings.append(Finding("ios.test", Dimension.FUNCTIONAL, Severity.SEV1, f"Test failed: {t.id}", evidence={"key": t.id}))
        if not proc.ok and not result.findings:
            result.findings.append(Finding("ios.test", Dimension.BUILD, Severity.SEV1, f"xcodebuild {action} failed", detail=proc.tail(30)))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.tests)} test(s), {len(result.findings)} failure(s)" if result.tests else ("ok" if proc.ok else "failed")
        results.append(result)
        return results

    @staticmethod
    def _plist(ctx: RunContext) -> CheckResult:
        spec = ctx.config.ios
        title = "Info.plist transport security and privacy strings"
        plists = [p for p in _files(ctx, spec.project_dir, spec.plist_glob) if "/Pods/" not in p.as_posix() and "Tests" not in p.as_posix()]
        if not plists:
            return CheckResult.skipped("ios.plist", title, Dimension.PLATFORM_SECURITY, "no Info.plist found")
        result = CheckResult("ios.plist", title, Dimension.PLATFORM_SECURITY, Status.PASS)
        for path in plists[:10]:
            try:
                data = plistlib.loads(path.read_bytes())
            except (plistlib.InvalidFileException, ValueError, ET.ParseError):
                continue
            if isinstance(data, dict):
                result.findings += audit_plist(data, ctx.rel(path))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(plists)} plist(s), {len(result.findings)} issue(s)"
        return result
