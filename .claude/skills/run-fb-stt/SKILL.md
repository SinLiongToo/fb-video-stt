---
name: run-fb-stt
description: Launch, test, and extend the FB 影片轉文字 (Facebook video/Reel speech-to-text) tool in this repo — index.html + Flask server.py using yt-dlp + faster-whisper locally. Use whenever asked to run, test, screenshot, debug, or change index.html / server.py / start.bat here.
---

# FB 影片轉文字 (FB video → transcript)

Single page `index.html` (no build, no CDN deps) served by `server.py` (Flask). Opening
`index.html` via `file://` cannot work — browsers can't fetch Facebook video (CORS + page
parsing), so the page refuses on `file:` and tells the user to run the server.

## Architecture

```
index.html ──POST /api/transcribe {url, model, language}──▶ server.py
           ◀── {id} ; then GET /api/job/<id> every 1s (status, progress, segments[])
server.py: yt-dlp (bestaudio, no ffmpeg merge) → temp file in %TEMP%/fb_stt
           → faster-whisper WhisperModel(name, cpu, int8), cached per model name
           → segments streamed into job dict → temp audio deleted
index.html ──POST /api/docx {segments, timestamps, title, url, meta, filename}──▶ .docx (python-docx)
```

TXT / SRT / copy are built client-side; only Word goes through the server.

## Hard rules

- **`URL_RE` in server.py only allows facebook.com / fb.watch / fb.com.** Don't widen it to
  arbitrary URLs — that turns the local server into an open downloader.
- **Default language is `auto`, not `zh`.** Verified with the sample reel (English speech):
  forcing `zh` makes Whisper *translate* into wrong Chinese instead of transcribing. `zh`
  adds `initial_prompt` asking for 繁體中文 + punctuation (also applied on `auto`, harmless for
  English — detection still picked `en`).
- Bind `127.0.0.1` only. Audio never goes to a third-party service — keep it that way.
- Theme: tokens on `:root`, dark set duplicated under
  `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` **and**
  `:root[data-theme="dark"]`. Button `#theme` cycles auto → light → dark, stored in
  `localStorage['fbstt-theme']` inside try/catch. Any new color must be a token defined in
  all three places.
- **Help panel (❓ 說明) is required and must stay current.** `#helpToggle` sits next to
  `#theme` in the header and toggles `#helpPanel` (`hidden` attribute + `aria-expanded`).
  Same convention as the sibling `../project_claude_TTS_SST`. The panel is written for the
  end user, in plain 繁體中文, no jargon: 開始之前 / 基本步驟 / 語言怎麼選 / 模型怎麼選 /
  結果怎麼用 / 常見問題 / 修改日誌. Whenever a user-visible feature, option, button, or
  error message changes, update the matching help section in the same change.
- **Version + changelog live in three places, synced by hand** (no build step):
  `.version-info` at the top of index.html (`版本 vX.Y｜最後更新 YYYY-MM-DD HH:MM（台灣時間）`),
  `#changelogList` inside the help panel, and README「開發紀錄」. Bump minor for each batch of
  user-visible changes; get the real time with `date "+%Y-%m-%d %H:%M"` — never reuse an old
  stamp or make one up. README entries only need the date. Internal-only changes: no bump.
- Word export sets `w:eastAsia` font (Microsoft JhengHei) on Normal style, or Chinese
  renders in a fallback font.

## Environment facts (this machine)

- Windows, Python 3.12, **CPU only** (no CUDA). Installed: yt-dlp, faster-whisper,
  Flask, python-docx, av (PyAV decodes m4a — no system ffmpeg on PATH, and none needed).
- Sample: `https://www.facebook.com/reel/1129966309452158` — 54 s English, offers an
  audio-only m4a DASH format. `small` model: ~94 s first run (incl. model download),
  ~19 s warm. First line: "The best thing I ever did was stop telling everyone what's
  going on in my life."

## Run

```bash
cd "<project dir>" && python server.py 8792     # run_in_background; or user double-clicks start.bat
for i in $(seq 1 20); do curl -sf http://localhost:8792/ -o /dev/null && break; sleep 1; done
```

Stop — use the PowerShell tool directly (not via Bash; `$` gets mangled). **Killing the
port owner is not enough on Windows**: a second `python.exe server.py 8792` can survive and
keep answering with old routes (seen: new `/api/docx` returned 404). Kill by command line:

```powershell
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like '*server.py 8792*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Leave other ports alone (e.g. `server.py 8790` belongs to a different project).

**The user also runs the server themselves from the VS Code terminal** — command line
`python.exe c:/Users/.../project_claub_fb_stt_/server.py` (no port arg → default 8792),
parent `powershell.exe` with VS Code shell integration. That copy runs whatever code existed
when they started it, holds 8792, and silently wins over a server you start (seen: new
`translate` flag ignored, info line missing 「→ 英文」). Before testing, check who owns 8792:

```powershell
Get-NetTCPConnection -LocalPort 8792 -State Listen | ForEach-Object {
  (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)").CommandLine }
```

If it's the user's copy, don't kill it — test on another port (`python server.py 8793`) and
tell them to restart theirs (Ctrl+C, rerun) to pick up server.py changes. index.html changes
need only a browser refresh (it is read from disk per request).

## Test

API smoke (write JSON to a file; inline Chinese through Git Bash quoting is unreliable):

```bash
id=$(curl -s -X POST http://localhost:8792/api/transcribe -H "Content-Type: application/json" \
  -d '{"url":"https://www.facebook.com/reel/1129966309452158","model":"small","language":"auto"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
# poll /api/job/$id every 5s until status is done|error; expect language "en", ~20 segments
curl -s -o t.docx -w "%{http_code}\n" -X POST http://localhost:8792/api/docx \
  -H "Content-Type: application/json" --data-binary @req.json   # expect 200; verify with python-docx
```

Print Chinese from Python with `sys.stdout.reconfigure(encoding='utf-8')` (cp950 console).

Browser: Playwright in the scratchpad (not the project dir) — `npm install playwright@1.63.0`,
`npx playwright install chromium`, write a real `.js` file, run with `node`. Theme check:
`browser.newPage({ colorScheme: 'dark' | 'light' })`, read
`getComputedStyle(document.body).backgroundColor` — light `rgb(246, 247, 249)`, dark
`rgb(24, 25, 26)`; click `#theme` three times and confirm it cycles back; reload and
confirm the saved choice persists. Help check: `#helpPanel` hidden on load; click
`#helpToggle` → visible and `aria-expanded="true"`; click again → hidden; screenshot it in
both themes; confirm `.version-info` version equals the top `#changelogList` entry.

## GitHub / Pages

- Repo `https://github.com/SinLiongToo/fb-video-stt` (public, branch `main`, Pages from
  root): **https://sinliongtoo.github.io/fb-video-stt/**. Commit + `git push` deploys; poll
  `gh api repos/SinLiongToo/fb-video-stt/pages/builds/latest --jq .status` until `built`.
- Pages serves index.html only. `ON_PAGES` (hostname ends with `github.io`) switches
  `API` to `http://localhost:8792`; `checkServer()` hits `/api/ping` and shows `#serverNote`.
  Every new fetch must use `API + '/api/...'`.
- server.py CORS allows only `ALLOWED_ORIGINS = {'https://sinliongtoo.github.io'}` and sends
  `Access-Control-Allow-Private-Network: true`. Don't widen to `*`. JSON endpoints use
  `get_json(silent=True)` (no `force=True`) so cross-site text/plain posts are rejected.
- **Chrome 153 Local Network Access**: a public https page reaching localhost needs the
  user's permission (one-time prompt). Headless Playwright denies it → "Permission was
  denied … `loopback` address". Test with
  `browser.newContext({ permissions: ['local-network-access'] })`. To test the live Pages
  site against a server on another port, `page.route('http://localhost:8792/**', r =>
  r.continue({ url: r.request().url().replace(':8792', ':8793') }))`.
- Committed files: index.html, server.py, start.bat, requirements.txt, README.md,
  .gitignore, .nojekyll, this skill. Keep personal paths out of README (use `%USERPROFILE%`).
- Commits end with the `Co-Authored-By` line from the session's attribution reminder.

## Languages and translate

- `language` whitelist in server.py: `auto, zh, en, ja, fr, nan` — add new ones there **and**
  in `#lang`, plus the help panel「語言怎麼選」.
- `output: orig | en | both` (legacy `translate: true` = `en`); UI `#output` select.
  `en` → faster-whisper `task="translate"` (**English only** — Whisper can't translate to
  Chinese). No Chinese `initial_prompt` when translating. Job `language` gets suffix
  ` → 英文` (en) or ` ＋ 英文` (both) — use that to verify the flag reached the server.
- `both` = `translate_segments()`: transcribe normally, then translate **each segment's own
  audio slice** (`decode_audio` + slice by start/end, `without_timestamps`,
  `condition_on_previous_text=False`) into `seg['en']`. Never replace this with a second
  full-file translate pass — segment boundaries differ and lines won't align. Skips clips
  < 0.3 s (hallucinated "Thank you."); skips entirely when detected language is `en`
  (`job['note']`). Costs ~3× (each clip is padded to a 30 s window). Progress: 0–50 %
  transcribe, 50–99 % translate, status 「翻譯成英文中…」.
- Every renderer/export must handle `seg.en`: page (`.seg .en`), copy, .txt, .srt
  (bilingual), .md (`mdText()`, client-side), Word (`/api/docx` adds italic line).
- Verified: Mandarin → English via `both` is good (Windows Hanhan TTS clip; generate with
  `System.Speech` `SelectVoice('Microsoft Hanhan Desktop')` → wav, then call
  `server.translate_segments` directly with `PYTHONPATH` = project dir).
- `large-v3-turbo` was trained without translation data and often returns the source
  language; UI shows `#turboNote` when translate + turbo. Translate is disabled in 台語 mode.
- No French audio on this machine (Windows voices: zh-TW Hanhan, en-US Zira only); French
  is untested end to end — ask the user for a French FB link if you need to verify.

## Taiwanese (台語) mode — `language: "nan"`

Full test log and decision table live in README「台語模型」; read it before changing this.

- Uses `TAIGI_MODEL_DIR` = `~/.cache/fb_stt/breeze-asr-26-ct2-int8` (env `FB_STT_TAIGI_MODEL`),
  downloaded from `phate334/Breeze-ASR-26-int8-CT2` (source revision `7b992682…` matches the
  official cache). Ignores the model dropdown (UI disables it). Missing `model.bin` → job error
  pointing at README.
- **Breeze outputs Mandarin characters by design** (model card), not 台語正字. UI label is
  「台語 → 華語」. Don't add taibun Tâi-lô romanization on top — romanizing Mandarin text
  invents syllables nobody said (was added, then removed for this reason).
- Do not try to run Breeze fp32 via transformers or convert it locally: 7.7GB RAM →
  silent OOM kill / converter segfault. NUTN v0.5 (converted at
  `~/.cache/fb_stt/nutn-tv05-ct2-int8`, unused) fails on drama audio with music.
- `get_model` keeps only one model loaded (clears cache on switch) for the same RAM reason.
- Taigi test reel: `https://www.facebook.com/reel/29026250636981104` (61 s; post text holds
  the Taigi-hanji dialogue as a reference). Expect ~155 s end to end, 2 long segments,
  text containing 「五千塊能否分成幾次給你」. Playwright: `selectOption('#lang','nan')`,
  `#model` disabled, `#taigiNote` visible; wait with a 600000 ms timeout.
