# PQE.01 — Project Quality Engine

<div dir="rtl">

موتوری که هر پروژه را از روی **مستنداتش** می‌فهمد، همهٔ ابعاد کیفیت را با ابزارهای قطعی می‌سنجد، تیک می‌زند که **هدف پروژه محقق شده یا نه**، و ایرادها را روی یک شاخهٔ جدا و پشت گیت انسانی اصلاح می‌کند.

## سه اصل غیرقابل‌مذاکره

۱. **پیشگو مستندات است، نه کد.** انتظارها از سند پروژه می‌آیند و در قرارداد کیفیت پروژه ثبت می‌شوند. موتور این قرارداد را هرگز تغییر نمی‌دهد.

۲. **چیزی که اجرا نشده، قبول نیست.** هر چک یا قبول است، یا رد، یا خطا، یا «ردشده با دلیل»، یا «بدون آستانهٔ تأییدشده». هدفِ بی‌شاهد «تأییدنشده» می‌ماند.

۳. **معماری قانون است.** موتور تخطی و رانش را پیدا می‌کند و اصلاح پیشنهاد می‌دهد؛ تست‌ها، قرارداد و خط مبنا مسیرهای محافظت‌شده‌اند و اصلاح‌گر حق دست زدن به آن‌ها را ندارد.

## چه چیزهایی سنجیده می‌شود

</div>

| Dimension | Checks | Tools |
|---|---|---|
| Goal & traceability | `goals.traceability`, goal verdicts | contract + documents |
| Functional | `functional.pytest`, `command.*` | pytest, any declared command |
| Architecture | `architecture.boundaries`, `architecture.mirrors` | Python AST, Tarjan SCC, SHA-256 |
| Build hygiene | `setup.hermetic` | git |
| Code quality | `quality.python.defects` | ruff (defect classes) |
| API contract / robustness | `api.contract`, `api.robustness` | built-in HTTP prober on an isolated instance |
| End-to-end flows | `api.scenario.*` | contract scenarios with saved variables |
| Security | `security.secrets`, `security.sast.*`, `security.dependencies.*`, `api.security.*` | built-in scanner, bandit, semgrep, pip-audit, npm/pnpm audit |
| Speed & load | `performance.latency`, `load.http` | built-in sampler and concurrent load |
| UI | `ui.interaction`, `ui.responsive`, `ui.visual`, `ui.accessibility`, `ui.design_review` | Playwright, axe-core, Pillow, optional vision review |
| Integrity & docs | `integrity.checksums`, `docs.references` | SHA-256, document path index |

<div dir="rtl">

## نصب

</div>

```bash
pip install -e '.[security,ui]'
```

```bash
python -m playwright install chromium
```

<div dir="rtl">

## استفاده

۱. شناخت پروژه و ساخت پیش‌نویس قرارداد:

</div>

```bash
pqe init /path/to/project
```

<div dir="rtl">

۲. تکمیل قرارداد از روی مستندات (در Claude Code با مهارت آنبوردینگ همین پلاگین).

۳. اجرای کامل:

</div>

```bash
pqe run /path/to/project --setup
```

<div dir="rtl">

۴. حلقهٔ اصلاح روی شاخهٔ جدا (حداکثر سه دور، ادغام با تو):

</div>

```bash
pqe fix /path/to/project --max-iterations 3
```

<div dir="rtl">

۵. پذیرش یک اجرای بازبینی‌شده به‌عنوان خط مبنا (برای تشخیص پسرفت):

</div>

```bash
pqe baseline /path/to/project /path/to/project/.qa/out/RUN/report.json
```

<div dir="rtl">

## خروجی‌ها

هر اجرا یک پوشه در مسیر خروجی پروژه می‌سازد شامل: گزارش فارسی، گزارش ماشینی، خروجی تست برای CI، یافته‌ها برای اسکن کد گیت‌هاب، و برای هر ماژول یک برگهٔ اصلاح. در صورت فعال بودن، خلاصه با اهداف تیک‌خورده در نوشن هم منتشر می‌شود.

## اتصال به Claude Code

</div>

```bash
claude plugin marketplace add hypermindedd/pqe01-project-quality-engine
```

```bash
claude plugin install pqe@pqe-marketplace
```

<div dir="rtl">

## اتصال به CI هر پروژه

فایل نمونهٔ گردش‌کار را در پوشهٔ گردش‌کار پروژه کپی کن و یک توکن فقط‌خواندنی برای همین مخزن به‌عنوان راز مخزن پروژه اضافه کن. نام راز:

</div>

```text
PQE_READ_TOKEN
```

<div dir="rtl">

## معماری

</div>

```
src/pqe/
├── model.py            domain types (Status, Severity, Dimension, Finding, CheckResult)
├── config.py           strict contract parser (.qa/pqe.toml)
├── context.py          safe process execution, secrets, file walking
├── discovery/          read-only project profiling + draft contract
├── runners/            one concern per runner, self-registering (Open/Closed)
├── analysis/           goal verdicts, traceability, triage, release decision, advisory LLM review
├── remediation/        bounded fix loop with protected-path guard
├── report/             Markdown (fa/en), JSON, JUnit, SARIF, Notion
└── orchestrator.py     the pipeline
plugin/                 Claude Code plugin: onboard / run / fix skills, auditor + test-architect agents
templates/              CI workflow for projects
```

<div dir="rtl">

افزودن یک بُعد تازه یعنی یک فایل اجراکنندهٔ جدید با ثبت خودکار؛ هستهٔ موتور تغییر نمی‌کند.

</div>
