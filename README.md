# PQE.01 — Project Quality Engine

<div dir="rtl">

موتوری که به ریپوی هر پروژه‌ای اضافه می‌شود و خودش همه‌چیز را انجام می‌دهد: تشخیص می‌دهد پروژه چیست، طراحی می‌کند به چه آزمون‌هایی نیاز دارد، پروژه را در یک محیط ایزوله و شبیه‌سازی‌شده بالا می‌آورد، همهٔ آزمون‌ها را می‌گیرد و یک تصمیم انتشار همراه با گزارش فارسی تحویل می‌دهد.

## چطور کار می‌کند

۱. **تشخیص نوع پروژه** از روی شواهد واقعی: فایل‌های ساخت، وابستگی‌ها و نقطهٔ شروع برنامه. یک ریپو می‌تواند چند بخش داشته باشد، مثلاً اپ موبایل به‌همراه سرویس پشتیبانش.

۲. **طراحی نقشهٔ آزمون** برای هر نوع، با توضیح اینکه هر آزمون چرا لازم است و در کدام محیط اجرا می‌شود.

۳. **ساخت محیط ایزوله:** رمزها و توکن‌ها تازه و تصادفی ساخته می‌شوند، پوشه‌های داده موقت‌اند و کلید سرویس‌های پولی خالی می‌ماند تا هیچ هزینهٔ واقعی‌ای ایجاد نشود.

۴. **کاوش برنامهٔ در حال اجرا:** صفحه‌ها و مستندات سرویس دیده می‌شوند و هوش مصنوعی از روی مستندات پروژه، هدف‌ها و مسیرهای کاربر را طراحی می‌کند.

۵. **اجرای همهٔ آزمون‌ها** در محیط درست: مرورگر واقعی برای سایت، شبیه‌ساز اندروید برای اپ اندروید و شبیه‌ساز آیفون روی مک.

۶. **گزارش و تصمیم انتشار.**

## سه اصل غیرقابل‌مذاکره

۱. **چیزی که اجرا نشده، قبول نیست.** نتیجه یا قبول است، یا رد، یا خطا، یا «ردشده با دلیل»، یا «اندازه‌گیری‌شده بدون آستانهٔ تأییدشده».

۲. **پیشنهاد هوش مصنوعی تا تأیید انسان، جلوی انتشار را نمی‌گیرد.** هدف‌هایی که از مستندات استخراج می‌شوند از اولین اجرا گزارش می‌شوند، اما فقط بعد از تأیید یک انسان به گیت انتشار تبدیل می‌شوند.

۳. **تصمیم انسان مقدم است.** قراردادی که انسان مالکیتش را گرفته، هرگز توسط موتور بازنویسی نمی‌شود. تست‌ها و قرارداد برای اصلاح‌گر خودکار مسیر ممنوعه‌اند.

## اتصال به یک پروژه

یک بار، در تنظیمات همین ریپوی موتور، دسترسی اکشن‌ها را برای ریپوهای خودت باز کن. مسیر تنظیم:

</div>

```text
Settings → Actions → General → Access → Accessible from repositories owned by the user
```

<div dir="rtl">

بعد در هر پروژه، فقط این فایل را اضافه کن:

</div>

```text
.github/workflows/pqe.yml
```

```yaml
name: Project Quality Engine
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
jobs:
  quality:
    uses: hypermindedd/Project-quallity-engine/.github/workflows/pqe.yml@main
    with:
      fail-on: no_go
    secrets: inherit
```

<div dir="rtl">

رازهای اختیاری پروژه: کلید هوش مصنوعی برای طراحی آزمون از روی مستندات و بازبینی تصویری، و توکن نوشن برای گزارش در اتاق فرمان. نام رازها:

</div>

```text
ANTHROPIC_API_KEY
NOTION_TOKEN
```

<div dir="rtl">

## اجرای محلی

</div>

```bash
pip install -e '.[security,ui]'
python -m playwright install chromium
pqe auto /path/to/project
```

<div dir="rtl">

دستورهای دیگر:

</div>

```bash
pqe classify /path/to/project
pqe plan /path/to/project
pqe run /path/to/project
pqe fix /path/to/project --max-iterations 3
pqe baseline /path/to/project /path/to/project/.qa/out/RUN/report.json
```

## What is detected and tested

| Project kind | Detected from | Environment | Suites |
|---|---|---|---|
| Static site | `index.html`, Jekyll, Hugo, MkDocs | Linux + Chromium | crawl & click, responsive, visual, axe, links, SEO, Lighthouse |
| Web app | Next, Vite, Vinext, Nuxt, SvelteKit, Astro, Angular, … | Linux + Chromium | the above + project tests, type-check, build, AI journeys |
| API service | FastAPI (incl. factories), Flask, Django, Express, Fastify, Nest | Linux | OpenAPI contract, hostile input, auth, headers/CORS, latency, load, AI scenarios |
| Android app | Gradle `com.android.application` | Android emulator (KVM) | manifest audit, build, unit, lint, install/launch/cold start, monkey stress, instrumented, Maestro |
| Flutter app | `pubspec.yaml` with Flutter SDK | Android emulator | analyze, test, APK, integration tests + all device checks |
| React Native / Expo | `react-native` / `expo` dependency | Android emulator | jest/tsc, prebuild, APK + all device checks |
| iOS app | `.xcodeproj` / `.xcworkspace` | macOS simulator | ATS/privacy plist audit, `xcodebuild test` |
| CLI tool | `[project.scripts]`, `bin` | Linux | tests + every entrypoint starts |
| Library | `pyproject` / `package.json` exports | Linux | tests, build |
| MQL5 Expert Advisor | `.mq5` / `.mq4` | Windows (planned, attested) | compile + backtest as human attestation |

Universal for every project: own test suite, committed secrets, vulnerable dependencies, SAST, dependency cycles,
documentation references, setup hermeticity.

## Architecture

```
src/pqe/
├── discovery/   classify.py (kinds + boot hints), profile.py (docs, goals, requirement IDs)
├── planning/    generate.py (test plan + contract), ai.py (document-driven goals/journeys), auto.py (autonomous mode)
├── runners/     functional, architecture, integrity, quality, security, api, ui (+journeys), web, mobile
├── analysis/    goal verdicts, traceability, triage, release decision, advisory visual review
├── remediation/ bounded fix loop with protected-path guard
├── report/      Markdown (fa/en), JSON, JUnit, SARIF, Notion
└── orchestrator.py
action.yml                     composite action (install / classify / auto)
.github/workflows/pqe.yml      reusable workflow: classify → web / android / ios jobs
templates/project-workflow.yml the 10-line file a project adds
plugin/                        Claude Code skills and agents
```

New project kinds are one detector in `classify.py` plus one handler in `generate.py`;
new checks are one self-registering runner. The core does not change.
