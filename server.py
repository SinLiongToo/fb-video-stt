#!/usr/bin/env python3
"""
FB 影片語音轉文字：本機伺服器。

瀏覽器沒辦法直接抓 Facebook 影片（CORS + 需要解析頁面），所以由這支腳本在本機：
  1. 用 yt-dlp 下載 FB 影片／Reel 的音軌（audio only，不需要 ffmpeg 合併）
  2. 用 faster-whisper 在本機 CPU 辨識成文字
音檔不會送到任何第三方服務。

用法：
    python server.py [port]
預設 port 8792，啟動後開 http://localhost:8792/
"""
import os
import re
import sys
import tempfile
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(tempfile.gettempdir(), 'fb_stt')
os.makedirs(WORK_DIR, exist_ok=True)

# 只接受 facebook / fb.watch 網址，避免變成任意網址下載器
URL_RE = re.compile(r'^https?://([a-z0-9-]+\.)?(facebook\.com|fb\.watch|fb\.com)/', re.I)
MODELS = {'tiny', 'base', 'small', 'medium', 'large-v3-turbo', 'large-v3'}

# 台語（實驗性）：MediaTek-Research/Breeze-ASR-26 的 CTranslate2 int8 版，給 faster-whisper 用。
# 注意：Breeze 聽台語、但輸出「華語漢字」（官方 model card 明講），不是台語正字。
# 原版 fp32 約 6GB，在 7.7GB RAM 的電腦上直接跑會記憶體不足；int8 版約 1.6GB。
# 下載方式與選這個模型的實測過程見 README「台語模型」。放在 OneDrive 外，避免同步 1.6GB。
TAIGI_MODEL_DIR = os.environ.get(
    'FB_STT_TAIGI_MODEL',
    os.path.join(os.path.expanduser('~'), '.cache', 'fb_stt', 'breeze-asr-26-ct2-int8'))

# GitHub Pages 版的網頁（https://sinliongtoo.github.io/fb-video-stt/）會來呼叫這台本機伺服器。
# CORS 只開給這一個來源；別的網站的 JS 不能操作這台伺服器。
ALLOWED_ORIGINS = {'https://sinliongtoo.github.io'}

app = Flask(__name__)


@app.after_request
def cors(resp):
    origin = request.headers.get('Origin')
    if origin in ALLOWED_ORIGINS:
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        # Chrome 的 Private Network Access：公開網站連 localhost 需要這個
        resp.headers['Access-Control-Allow-Private-Network'] = 'true'
        resp.headers['Vary'] = 'Origin'
    return resp


@app.route('/api/<path:_>', methods=['OPTIONS'])
def preflight(_):
    return '', 204


@app.get('/api/ping')
def ping():
    return jsonify(ok=True)


jobs = {}           # job_id -> dict(status, progress, message, result, error)
_models = {}         # model name -> WhisperModel（只留最近用的一個，RAM 不夠同時放兩個大模型）
_model_lock = threading.Lock()


def get_model(name):
    with _model_lock:
        if name not in _models:
            from faster_whisper import WhisperModel
            _models.clear()
            _models[name] = WhisperModel(name, device='cpu', compute_type='int8')
        return _models[name]


def download_audio(url, job):
    import yt_dlp
    out_tmpl = os.path.join(WORK_DIR, f'{job["id"]}.%(ext)s')

    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if total:
                job['progress'] = int(d.get('downloaded_bytes', 0) * 100 / total)

    opts = {
        # 優先只抓音軌；沒有獨立音軌時退回含音訊的單一 mp4（不需 ffmpeg 合併）
        'format': 'bestaudio/best[acodec!=none]/best',
        'outtmpl': out_tmpl,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    return path, info


def translate_segments(model, path, out, src_lang, job):
    """並列模式第二階段：把每段原文對應的那一小段聲音丟去翻成英文，
    英文自然跟原文一句對一句（分開跑兩次辨識的話，兩邊的分段長短對不起來）。"""
    from faster_whisper import decode_audio
    audio = decode_audio(path, sampling_rate=16000)
    for i, seg in enumerate(out):
        a, b = int(seg['start'] * 16000), int(seg['end'] * 16000)
        clip = audio[a:b]
        if len(clip) < 16000 * 0.3:   # 太短的片段翻不出東西，還容易冒出 "Thank you."
            seg['en'] = ''
        else:
            parts, _ = model.transcribe(clip, task='translate', language=src_lang, beam_size=5,
                                        vad_filter=False, without_timestamps=True,
                                        condition_on_previous_text=False)
            seg['en'] = ' '.join(p.text.strip() for p in parts).strip()
        job['progress'] = 50 + int((i + 1) * 49 / len(out))


def run_job(job, url, model_name, language, output='orig'):
    path = None
    try:
        job.update(status='downloading', message='下載影片音軌中…', progress=0)
        path, info = download_audio(url, job)
        # FB 的 title 前面會黏「49 reactions | 」之類的計數，去掉
        title = info.get('title') or info.get('description') or ''
        job['title'] = re.sub(r'^[\d.,KkMm萬]+\s*(reactions?|views?|個心情|次觀看)\s*[|·]\s*', '', title)
        job['duration'] = info.get('duration') or 0

        taigi = language == 'nan'
        if taigi:
            model_name = TAIGI_MODEL_DIR
            job['model'] = 'Breeze-ASR-26 (台語→華語)'
            if not os.path.isfile(os.path.join(TAIGI_MODEL_DIR, 'model.bin')):
                raise RuntimeError('找不到台語模型，請先照 README「台語模型」下載 Breeze-ASR-26 int8')
        job.update(status='loading', message=f'載入模型 {job["model"]}（第一次使用需下載）…', progress=0)
        model = get_model(model_name)

        job.update(status='transcribing', message='語音辨識中…', progress=0)
        kwargs = dict(beam_size=5, vad_filter=True)
        if taigi:
            # Breeze 用 Whisper 的 zh token；實測加不加台語 prompt 結果幾乎一樣，所以不加
            kwargs['language'] = 'zh'
        elif language != 'auto':
            kwargs['language'] = language
        if taigi:
            output = 'orig'           # Breeze 不支援翻譯
        if output == 'en':
            # Whisper 內建只能翻成英文；language 是「原音」語言
            kwargs['task'] = 'translate'
        elif language in ('zh', 'auto'):
            # 引導 Whisper 輸出繁體中文＋標點（翻譯時不加，免得中文 prompt 干擾英文輸出）
            kwargs['initial_prompt'] = '以下是繁體中文的逐字稿，包含標點符號。'
        segments, info2 = model.transcribe(path, **kwargs)
        job['language'] = '台語（輸出華語）' if taigi else info2.language
        if output == 'en':
            job['language'] += ' → 英文'
        both = output == 'both'
        if both and info2.language == 'en':
            both = False              # 原文就是英文，不用再翻
            job['note'] = '原文已經是英文，所以沒有另外翻譯'
        # 並列模式：辨識佔進度 0–50%，翻譯佔 50–100%
        span = 50 if both else 99
        total = info2.duration or job['duration'] or 1
        out = []
        for s in segments:
            out.append({'start': round(s.start, 2), 'end': round(s.end, 2), 'text': s.text.strip()})
            job['segments'] = out
            job['progress'] = min(span, int(s.end * span / total))
        if both and out:
            job.update(message='翻譯成英文中…', progress=50)
            job['language'] += ' ＋ 英文'
            translate_segments(model, path, out, info2.language, job)
        job.update(status='done', message='完成', progress=100)
    except Exception as e:
        job.update(status='error', message=f'失敗：{e}', error=str(e))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        job['elapsed'] = round(time.time() - job['started'], 1)


@app.get('/')
def index():
    return send_from_directory(HERE, 'index.html')


@app.post('/api/transcribe')
def transcribe():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    model_name = data.get('model') or 'small'
    # 預設自動偵測：強制 zh 時，英文影片會被 Whisper 直接「翻譯」成中文
    language = data.get('language') or 'auto'
    if not URL_RE.match(url):
        return jsonify(error='請輸入 Facebook 影片／Reel 網址'), 400
    if model_name not in MODELS:
        return jsonify(error='未知的模型'), 400
    if language not in ('auto', 'zh', 'en', 'ja', 'fr', 'nan'):
        return jsonify(error='未知的語言'), 400
    # output: orig 只要原文 / en 只要英文 / both 原文＋英文並列（舊版的 translate:true 視同 en）
    output = data.get('output') or ('en' if data.get('translate') else 'orig')
    if output not in ('orig', 'en', 'both'):
        return jsonify(error='未知的輸出方式'), 400
    job_id = uuid.uuid4().hex[:12]
    job = {'id': job_id, 'status': 'queued', 'message': '排隊中…', 'progress': 0,
           'segments': [], 'started': time.time(), 'url': url, 'model': model_name}
    jobs[job_id] = job
    threading.Thread(target=run_job, args=(job, url, model_name, language, output), daemon=True).start()
    return jsonify(id=job_id)


@app.post('/api/docx')
def export_docx():
    """把逐字稿輸出成 Word (.docx)。內容由前端送來（使用者可能已在頁面上看過）。"""
    import io
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    from flask import send_file

    data = request.get_json(silent=True) or {}
    segments = data.get('segments') or []
    show_ts = bool(data.get('timestamps'))

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(12)
    # 中文字型要另外設 eastAsia，不然 Word 會用預設的新細明體以外字型亂套
    style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft JhengHei')

    doc.add_heading(data.get('title') or 'FB 影片逐字稿', level=1)
    for line in (data.get('url'), data.get('meta')):
        if line:
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x65, 0x67, 0x6B)

    for s in segments:
        p = doc.add_paragraph()
        if show_ts:
            t = float(s.get('start') or 0)
            ts = p.add_run(f'[{int(t // 60):02d}:{int(t % 60):02d}] ')
            ts.font.color.rgb = RGBColor(0x8A, 0x8D, 0x91)
        p.add_run(str(s.get('text') or ''))
        if s.get('en'):
            p.add_run().add_break()
            en = p.add_run(str(s['en']))
            en.italic = True
            en.font.size = Pt(10.5)
            en.font.color.rgb = RGBColor(0x65, 0x67, 0x6B)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    name = re.sub(r'[^\w.-]', '_', data.get('filename') or 'transcript') + '.docx'
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


@app.get('/api/job/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error='找不到工作'), 404
    return jsonify({k: v for k, v in job.items() if k != 'started'})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8792
    print(f'FB 影片語音轉文字 -> http://localhost:{port}/   （Ctrl+C 結束）')
    app.run(host='127.0.0.1', port=port, threaded=True)


if __name__ == '__main__':
    main()
