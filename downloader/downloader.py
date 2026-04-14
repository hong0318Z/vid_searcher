"""
스트리밍 영상 다운로더
m3u8 / HLS / DASH / YouTube / 기타 1000+ 사이트 지원 (yt-dlp 기반)

실행: python downloader.py
의존성: pip install yt-dlp
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import os
import sys
import time
import re
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import uuid

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    import curl_cffi as _curl_cffi_mod
    _ver = tuple(int(x) for x in _curl_cffi_mod.__version__.split('.')[:2])
    # yt-dlp는 curl_cffi 0.5.x ~ 0.9.x 만 지원. 0.10+ 는 API 변경으로 unsupported.
    HAS_CURL_CFFI = _ver < (0, 10)
    CURL_CFFI_VERSION = _curl_cffi_mod.__version__
    CURL_CFFI_UNSUPPORTED = not HAS_CURL_CFFI
except ImportError:
    HAS_CURL_CFFI = False
    CURL_CFFI_VERSION = None
    CURL_CFFI_UNSUPPORTED = False


def _impersonate_opts() -> dict:
    """curl_cffi 설치 시 generic 추출기에 Cloudflare 우회 impersonation 적용.
    'impersonate' 최상위 옵션은 ImpersonateTarget 객체가 필요하므로 사용 안 함."""
    if not HAS_CURL_CFFI:
        return {}
    # 빈 문자열 = yt-dlp가 지원되는 타겟 자동 선택
    return {'extractor_args': {'generic': {'impersonate': ['']}}}


class _YtdlpLogger:
    """yt-dlp 경고·오류를 GUI 로그 패널로 전달하는 어댑터."""
    def __init__(self, on_warning, on_error):
        self._warn = on_warning
        self._err  = on_error

    def debug(self, msg):
        pass  # 디버그 메시지는 무시

    def warning(self, msg):
        self._warn(msg)

    def error(self, msg):
        self._err(msg)

# ──────────────────────────────────────────────
# 다크 테마 색상
# ──────────────────────────────────────────────
BG       = '#1e1e2e'
BG2      = '#2a2a3e'
BG3      = '#313145'
FG       = '#cdd6f4'
FG2      = '#a6adc8'
ACCENT   = '#7c6ff7'
ACCENT2  = '#9d98f9'
GREEN    = '#a6e3a1'
RED      = '#f38ba8'
YELLOW   = '#f9e2af'
BLUE     = '#89b4fa'
BORDER   = '#45475a'

CFG_PATH = Path(__file__).parent / 'downloader_cfg.json'


def load_cfg():
    try:
        return json.loads(CFG_PATH.read_text('utf-8'))
    except Exception:
        return {}


def save_cfg(cfg: dict):
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')


# ──────────────────────────────────────────────
# 다운로드 항목 데이터
# ──────────────────────────────────────────────
@dataclass
class DownloadItem:
    uid:          str
    url:          str
    save_dir:     str
    quality:      str          # 표시용 ('최고화질' / '1080p' / '오디오만' 등)
    format_str:   str = ''     # yt-dlp format string (예: 'bestvideo+bestaudio/best')
    custom_title: str = ''     # 사용자 지정 파일명 (빈 문자열이면 사이트 제목 사용)
    referer:      str = ''     # Referer 헤더 (CDN 403 우회용)
    cookie_browser: str = ''  # 브라우저 쿠키 소스 ('chrome'/'firefox'/'edge' 등)
    status:       str = 'pending'   # pending / downloading / merging / done / error / cancelled
    title:        str = ''
    filename:     str = ''
    progress:     float = 0.0  # 0~100
    speed:        str = ''
    eta:          str = ''
    size:         str = ''
    error:        str = ''


# ──────────────────────────────────────────────
# yt-dlp 래퍼
# ──────────────────────────────────────────────
class Downloader:
    """단일 다운로드 실행기. 별도 스레드에서 실행."""

    def __init__(self, item: DownloadItem,
                 on_progress,        # (item) -> None
                 on_done,            # (item) -> None
                 on_error,           # (item) -> None
                 on_log=None):       # (msg, color) -> None
        self.item = item
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
        self.on_log   = on_log or (lambda msg, color=None: None)
        self._ydl: Optional[yt_dlp.YoutubeDL] = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        # yt-dlp 는 abort_download 플래그로 중단
        if self._ydl:
            try:
                self._ydl.params['abort_download'] = True
            except Exception:
                pass

    def _hook(self, d):
        if self._cancelled:
            raise yt_dlp.utils.DownloadCancelled()

        status = d.get('status')
        item = self.item

        if status == 'downloading':
            total   = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            down    = d.get('downloaded_bytes', 0)
            item.progress = (down / total * 100) if total else 0
            spd = d.get('speed')
            if spd:
                item.speed = _fmt_speed(spd)
            eta = d.get('eta')
            if eta is not None:
                item.eta = _fmt_eta(eta)
            if total:
                item.size = _fmt_size(total)
            self.on_progress(item)

        elif status == 'finished':
            item.progress = 100
            item.speed = ''
            item.eta = ''
            self.on_progress(item)

        elif status == 'error':
            item.error = str(d.get('error', '알 수 없는 오류'))

    def _postproc_hook(self, d):
        if d.get('status') == 'started':
            self.item.status = 'merging'
            self.on_progress(self.item)

    def run(self):
        item = self.item
        item.status = 'downloading'
        self.on_progress(item)

        # format_str 이 있으면 우선 사용, 없으면 best fallback
        fmt = item.format_str if item.format_str else 'bestvideo+bestaudio/best'

        # 파일명: 사용자가 지정한 제목이 있으면 그것을, 없으면 사이트 제목 사용
        if item.custom_title:
            outtmpl = os.path.join(item.save_dir, item.custom_title + '.%(ext)s')
        else:
            outtmpl = os.path.join(item.save_dir, '%(title)s.%(ext)s')

        logger = _YtdlpLogger(
            on_warning=lambda m: self.on_log(f'⚠ {m}', YELLOW),
            on_error  =lambda m: self.on_log(f'✕ {m}', RED),
        )
        # Referer 헤더 — CDN 403 우회
        http_headers = {}
        if item.referer:
            http_headers['Referer'] = item.referer

        ydl_opts = {
            'format':            fmt,
            'outtmpl':           outtmpl,
            'progress_hooks':    [self._hook],
            'postprocessor_hooks': [self._postproc_hook],
            'logger':            logger,
            'quiet':             True,
            'no_warnings':       False,
            'noprogress':        True,
            'ignoreerrors':      False,
            'nocheckcertificate': True,
            'retries':           3,
            'fragment_retries':  5,
            'concurrent_fragment_downloads': 4,
            'merge_output_format': 'mp4',
            'hls_use_mpegts':    False,
            **({'http_headers': http_headers} if http_headers else {}),
            **({'cookiesfrombrowser': (item.cookie_browser, None, None, None)}
               if item.cookie_browser else {}),
            **_impersonate_opts(),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._ydl = ydl
                info = ydl.extract_info(item.url, download=False)
                if info:
                    item.title    = info.get('title', item.url)
                    item.filename = ydl.prepare_filename(info)
                self.on_progress(item)

                ydl.download([item.url])

        except yt_dlp.utils.DownloadCancelled:
            item.status = 'cancelled'
            item.speed  = ''
            item.eta    = ''
            self.on_done(item)
            return
        except Exception as e:
            item.status = 'error'
            item.error  = str(e)
            self.on_error(item)
            return

        item.status   = 'done'
        item.progress = 100
        item.speed    = ''
        item.eta      = ''
        self.on_done(item)


# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────
def _fmt_speed(bps: float) -> str:
    if bps >= 1_000_000:
        return f'{bps/1_000_000:.1f} MB/s'
    elif bps >= 1_000:
        return f'{bps/1_000:.0f} KB/s'
    return f'{bps:.0f} B/s'


def _fmt_eta(sec: int) -> str:
    if sec >= 3600:
        return f'{sec//3600}h {(sec%3600)//60}m'
    elif sec >= 60:
        return f'{sec//60}m {sec%60}s'
    return f'{sec}s'


def _fmt_size(b: int) -> str:
    if b >= 1_000_000_000:
        return f'{b/1_000_000_000:.2f} GB'
    elif b >= 1_000_000:
        return f'{b/1_000_000:.1f} MB'
    elif b >= 1_000:
        return f'{b/1_000:.0f} KB'
    return f'{b} B'



# ──────────────────────────────────────────────
# 포맷 선택 다이얼로그
# ──────────────────────────────────────────────
class FormatPickerDialog(tk.Toplevel):
    """URL 분석 결과 표시 — 포맷·파일명 선택 후 대기열에 추가"""

    def __init__(self, parent, url: str, info: dict, on_confirm):
        """
        on_confirm(fmt_str, custom_title, quality_label) 콜백을 호출한 뒤 닫힘.
        """
        super().__init__(parent)
        self._url = url
        self._info = info
        self._on_confirm = on_confirm
        self._fmt_map: dict[str, str] = {}   # tree iid -> yt-dlp format string

        self.title('스트림 선택')
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry('700x480')
        self.transient(parent)
        self.grab_set()

        self._build()
        self.bind('<Return>', lambda e: self._confirm())

    # ── UI 구성 ─────────────────────────────────
    def _build(self):
        info      = self._info
        site_title = info.get('title', self._url)
        duration   = info.get('duration')
        uploader   = (info.get('uploader') or info.get('channel')
                      or info.get('extractor_key', ''))

        # 파일명 편집 행
        hdr = ttk.Frame(self, padding=(14, 12, 14, 6))
        hdr.pack(fill='x')

        ttk.Label(hdr, text='파일명  (수정 가능)',
                  foreground=FG2, font=('Segoe UI', 8)).pack(anchor='w')
        self._title_var = tk.StringVar(value=_sanitize_filename(site_title))
        title_e = ttk.Entry(hdr, textvariable=self._title_var,
                            font=('Segoe UI', 10))
        title_e.pack(fill='x', pady=(2, 8))
        title_e.select_range(0, 'end')
        title_e.focus_set()

        # 메타 정보 (재생시간 / 채널)
        meta = ttk.Frame(hdr)
        meta.pack(fill='x', pady=(0, 4))
        if duration:
            ttk.Label(meta,
                      text=f'재생시간: {_fmt_duration(int(duration))}',
                      foreground=FG2, font=('Segoe UI', 8)
                      ).pack(side='left', padx=(0, 16))
        if uploader:
            ttk.Label(meta, text=f'채널/업로더: {uploader}',
                      foreground=FG2, font=('Segoe UI', 8)
                      ).pack(side='left')

        ttk.Separator(self).pack(fill='x')

        # 포맷 목록
        body = ttk.Frame(self, padding=(14, 8, 14, 6))
        body.pack(fill='both', expand=True)

        ttk.Label(body, text='포맷 선택  (더블클릭 또는 Enter 로 추가)',
                  foreground=FG2, font=('Segoe UI', 8)).pack(anchor='w', pady=(0, 4))

        cols = ('resolution', 'ext', 'size', 'codec', 'duration', 'note')
        self._tree = ttk.Treeview(body, columns=cols, show='headings',
                                   selectmode='browse', style='Picker.Treeview')

        for cid, head, w, anc in [
            ('resolution', '해상도',   110, 'center'),
            ('ext',        '확장자',    55, 'center'),
            ('size',       '크기',      90, 'center'),
            ('codec',      '코덱',      90, 'center'),
            ('duration',   '재생시간',  90, 'center'),
            ('note',       '비고',     175, 'w'),
        ]:
            self._tree.heading(cid, text=head)
            self._tree.column(cid, width=w, anchor=anc, stretch=(cid == 'note'))

        vsb = ttk.Scrollbar(body, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        self._tree.bind('<Double-1>', lambda e: self._confirm())

        self._fill_formats(info)

        # 버튼 행
        ttk.Separator(self).pack(fill='x')
        btn = ttk.Frame(self, padding=(14, 8))
        btn.pack(fill='x')
        ttk.Button(btn, text='취소',
                   command=self.destroy).pack(side='right', padx=(6, 0))
        ttk.Button(btn, text='대기열에 추가', style='Accent.TButton',
                   command=self._confirm).pack(side='right')

    # ── 포맷 목록 채우기 ─────────────────────────
    def _fill_formats(self, info: dict):
        formats  = info.get('formats', [])
        duration = info.get('duration')
        dur_str  = _fmt_duration(int(duration)) if duration else '-'

        # 자동 최고화질 (항상 첫 줄)
        self._tree.insert('', 'end', iid='auto_best',
                          values=('자동 최고화질', 'mp4', '-', '-',
                                  dur_str, '최적 비디오+오디오 자동 병합'))
        self._fmt_map['auto_best'] = 'bestvideo+bestaudio/best'

        # 복합 포맷 (비디오+오디오 동시 포함)
        seen_h: set[int] = set()
        combined = []
        for f in reversed(formats):
            h = f.get('height')
            if (f.get('vcodec', 'none') != 'none'
                    and f.get('acodec', 'none') != 'none'
                    and h and h not in seen_h):
                seen_h.add(h)
                combined.append(f)
        combined.sort(key=lambda f: f.get('height', 0), reverse=True)

        for f in combined:
            iid = f'c_{f["format_id"]}'
            self._insert_fmt_row(iid, f, dur_str)
            self._fmt_map[iid] = f['format_id']

        # DASH/HLS 분리 스트림 → 높이별 자동 병합 옵션
        seen_h2: set[int] = set()
        video_only = []
        for f in reversed(formats):
            h = f.get('height')
            if (f.get('vcodec', 'none') != 'none'
                    and f.get('acodec', 'none') == 'none'
                    and h and h not in seen_h and h not in seen_h2):
                seen_h2.add(h)
                video_only.append(f)
        video_only.sort(key=lambda f: f.get('height', 0), reverse=True)

        for f in video_only:
            h   = f.get('height', 0)
            iid = f'd_{h}'
            self._insert_fmt_row(iid, f, dur_str, note_extra='(자동 병합)')
            self._fmt_map[iid] = (
                f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
            )

        # 오디오 전용 (최고 품질 1개만)
        audio_fmts = [f for f in formats
                      if f.get('vcodec', 'none') == 'none'
                      and f.get('acodec', 'none') != 'none']
        if audio_fmts:
            best_a  = max(audio_fmts, key=lambda f: f.get('abr') or 0)
            abr     = best_a.get('abr', 0)
            acodec  = (best_a.get('acodec') or '').split('.')[0]
            size    = best_a.get('filesize') or best_a.get('filesize_approx')
            note    = f'{abr:.0f} kbps' if abr else ''
            self._tree.insert('', 'end', iid='audio_only',
                              values=('오디오만',
                                      best_a.get('ext', 'm4a'),
                                      _fmt_size(size) if size else '알 수 없음',
                                      acodec or '-',
                                      dur_str, note))
            self._fmt_map['audio_only'] = 'bestaudio/best'

        # 첫 번째 항목 선택
        children = self._tree.get_children()
        if children:
            self._tree.selection_set(children[0])
            self._tree.focus(children[0])

    def _insert_fmt_row(self, iid: str, f: dict,
                        dur_str: str, note_extra: str = ''):
        h      = f.get('height', 0)
        w      = f.get('width', 0)
        res    = f'{w}x{h}' if w and h else (f'{h}p' if h else '?')
        ext    = f.get('ext', '?')
        size   = f.get('filesize') or f.get('filesize_approx')
        vcodec = (f.get('vcodec') or '').split('.')[0] or '?'
        note   = (f.get('format_note') or '')
        if note_extra:
            note = f'{note} {note_extra}'.strip()
        self._tree.insert('', 'end', iid=iid,
                          values=(res, ext,
                                  _fmt_size(size) if size else '알 수 없음',
                                  vcodec, dur_str, note))

    # ── 확인 ────────────────────────────────────
    def _confirm(self):
        sel = self._tree.selection()
        if not sel:
            messagebox.showwarning('선택 없음', '포맷을 선택하세요.', parent=self)
            return
        iid       = sel[0]
        fmt_str   = self._fmt_map.get(iid, 'bestvideo+bestaudio/best')
        label     = self._tree.set(iid, 'resolution')   # 표시용 품질 레이블
        custom    = self._title_var.get().strip()
        self._on_confirm(fmt_str, custom, label)
        self.destroy()


# ──────────────────────────────────────────────
# 메인 앱
# ──────────────────────────────────────────────
class DownloaderApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title('스트리밍 다운로더')
        self.geometry('820x660')
        self.minsize(640, 480)
        self.configure(bg=BG)

        self._cfg = load_cfg()
        self._items: dict[str, DownloadItem] = {}   # uid -> item
        self._workers: dict[str, Downloader] = {}   # uid -> Downloader
        self._ui_queue: queue.Queue = queue.Queue()
        self._analyzing = False

        self._style()
        self._build_ui()
        self._poll_ui()

        # 저장된 경로 복원
        saved_dir = self._cfg.get('save_dir', str(Path.home() / 'Downloads'))
        self._dir_var.set(saved_dir)

    # ── TTK 스타일 ──────────────────────────────
    def _style(self):
        s = ttk.Style(self)
        s.theme_use('clam')

        s.configure('.', background=BG, foreground=FG,
                    fieldbackground=BG2, bordercolor=BORDER,
                    troughcolor=BG2, selectbackground=ACCENT,
                    selectforeground=FG, font=('Segoe UI', 9))

        s.configure('TFrame',  background=BG)
        s.configure('TLabel',  background=BG,  foreground=FG)
        s.configure('TEntry',  fieldbackground=BG2, foreground=FG,
                    insertcolor=FG, bordercolor=BORDER)
        s.configure('TCombobox', fieldbackground=BG2, foreground=FG,
                    selectbackground=ACCENT, arrowcolor=FG2)
        s.map('TCombobox', fieldbackground=[('readonly', BG2)])

        s.configure('Accent.TButton', background=ACCENT, foreground='white',
                    bordercolor=ACCENT, focuscolor=ACCENT2, padding=(8, 4))
        s.map('Accent.TButton',
              background=[('active', ACCENT2), ('disabled', BG3)],
              foreground=[('disabled', FG2)])

        s.configure('TButton', background=BG3, foreground=FG,
                    bordercolor=BORDER, padding=(8, 4))
        s.map('TButton',
              background=[('active', BORDER)],
              foreground=[('disabled', FG2)])

        s.configure('TProgressbar', troughcolor=BG3,
                    background=ACCENT, bordercolor=BG3, thickness=6)

        s.configure('Treeview', background=BG2, foreground=FG,
                    fieldbackground=BG2, bordercolor=BORDER,
                    rowheight=52)
        s.configure('Treeview.Heading', background=BG3, foreground=FG2,
                    bordercolor=BORDER)
        s.map('Treeview',
              background=[('selected', ACCENT)],
              foreground=[('selected', 'white')])

        # 포맷 선택 다이얼로그 전용 (행 높이 작게)
        s.configure('Picker.Treeview', background=BG2, foreground=FG,
                    fieldbackground=BG2, bordercolor=BORDER,
                    rowheight=28)
        s.configure('Picker.Treeview.Heading', background=BG3, foreground=FG2,
                    bordercolor=BORDER)
        s.map('Picker.Treeview',
              background=[('selected', ACCENT)],
              foreground=[('selected', 'white')])

    # ── UI 빌드 ─────────────────────────────────
    def _build_ui(self):
        # ── 상단 입력 패널 ──
        top = ttk.Frame(self, padding=(12, 10, 12, 8))
        top.pack(fill='x')

        # URL 입력
        url_row = ttk.Frame(top)
        url_row.pack(fill='x', pady=(0, 4))

        ttk.Label(url_row, text='URL', width=8, anchor='w').pack(side='left')
        self._url_var = tk.StringVar()
        url_entry = ttk.Entry(url_row, textvariable=self._url_var)
        url_entry.pack(side='left', fill='x', expand=True, padx=(4, 4))
        url_entry.bind('<Return>', lambda e: self._analyze_url())

        ttk.Button(url_row, text='붙여넣기',
                   command=self._paste_url).pack(side='left', padx=(0, 4))

        # Referer (CDN 403 우회용)
        ref_row = ttk.Frame(top)
        ref_row.pack(fill='x', pady=(0, 4))

        ttk.Label(ref_row, text='Referer', width=8, anchor='w',
                  foreground=FG2).pack(side='left')
        self._referer_var = tk.StringVar()
        ref_entry = ttk.Entry(ref_row, textvariable=self._referer_var,
                              foreground=FG2)
        ref_entry.pack(side='left', fill='x', expand=True, padx=(4, 4))
        ref_entry.bind('<Return>', lambda e: self._analyze_url())

        ttk.Label(ref_row,
                  text='m3u8 직접 입력 시 원본 페이지 URL (403 오류 시)',
                  foreground=FG2, font=('Segoe UI', 7)).pack(side='left')

        # 브라우저 쿠키 (Cloudflare 로그인 사이트 우회)
        cookie_row = ttk.Frame(top)
        cookie_row.pack(fill='x', pady=(0, 6))

        ttk.Label(cookie_row, text='쿠키', width=8, anchor='w',
                  foreground=FG2).pack(side='left')
        self._browser_var = tk.StringVar(value='없음')
        browser_cb = ttk.Combobox(cookie_row, textvariable=self._browser_var,
                                  state='readonly', width=12,
                                  values=['없음', 'chrome', 'firefox', 'edge', 'brave'])
        browser_cb.pack(side='left', padx=(4, 4))
        ttk.Label(cookie_row,
                  text='Cloudflare 차단 사이트 — 로그인된 브라우저 쿠키 사용 (없음 = 미사용)',
                  foreground=FG2, font=('Segoe UI', 7)).pack(side='left')

        # 저장 경로
        dir_row = ttk.Frame(top)
        dir_row.pack(fill='x', pady=(0, 6))

        ttk.Label(dir_row, text='저장 위치', width=8, anchor='w').pack(side='left')
        self._dir_var = tk.StringVar()
        ttk.Entry(dir_row, textvariable=self._dir_var).pack(
            side='left', fill='x', expand=True, padx=(4, 4))
        ttk.Button(dir_row, text='폴더 선택',
                   command=self._pick_dir).pack(side='left', padx=(0, 4))

        # 스트림 분석 버튼 + 상태 레이블
        opt_row = ttk.Frame(top)
        opt_row.pack(fill='x')

        self._analyze_btn = ttk.Button(opt_row, text='🔍 스트림 분석',
                                        style='Accent.TButton',
                                        command=self._analyze_url)
        self._analyze_btn.pack(side='left')

        self._analyze_lbl = ttk.Label(opt_row, text='',
                                       foreground=FG2, font=('Segoe UI', 8))
        self._analyze_lbl.pack(side='left', padx=(10, 0))

        # yt-dlp 미설치 경고
        if not HAS_YTDLP:
            tk.Label(top,
                     text='⚠  yt-dlp 가 설치되어 있지 않습니다.  →  pip install yt-dlp',
                     bg=BG, fg=YELLOW, font=('Segoe UI', 9)).pack(fill='x', pady=(4, 0))

        # curl_cffi 버전 경고
        if CURL_CFFI_UNSUPPORTED:
            tk.Label(top,
                     text=f'⚠  curl_cffi {CURL_CFFI_VERSION} 은 yt-dlp와 호환 안 됨 (Cloudflare 우회 불가)'
                          '  →  pip install "curl_cffi>=0.5.10,<0.10"',
                     bg=BG, fg=YELLOW, font=('Segoe UI', 8)).pack(fill='x', pady=(2, 0))
        elif not HAS_CURL_CFFI:
            tk.Label(top,
                     text='ℹ  Cloudflare 사이트 403 오류 시: pip install "curl_cffi>=0.5.10,<0.10"',
                     bg=BG, fg=FG2, font=('Segoe UI', 8)).pack(fill='x', pady=(2, 0))

        ttk.Separator(self).pack(fill='x')

        # ── 다운로드 목록 ──
        list_frame = ttk.Frame(self)
        list_frame.pack(fill='both', expand=True, padx=12, pady=(8, 0))

        # Treeview  (uid 는 iid 로 사용)
        cols = ('title', 'quality', 'progress', 'speed', 'eta', 'size', 'status')
        self._tree = ttk.Treeview(list_frame, columns=cols,
                                  show='headings', selectmode='extended')

        col_cfg = [
            ('title',    '제목 / URL',   300, 'w'),
            ('quality',  '품질',          60, 'center'),
            ('progress', '진행',          90, 'center'),
            ('speed',    '속도',          80, 'center'),
            ('eta',      '남은 시간',     80, 'center'),
            ('size',     '크기',          70, 'center'),
            ('status',   '상태',          80, 'center'),
        ]
        for cid, head, w, anc in col_cfg:
            self._tree.heading(cid, text=head)
            self._tree.column(cid, width=w, anchor=anc, stretch=(cid == 'title'))

        vsb = ttk.Scrollbar(list_frame, orient='vertical',
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        self._tree.tag_configure('done',       foreground=GREEN)
        self._tree.tag_configure('error',      foreground=RED)
        self._tree.tag_configure('cancelled',  foreground=FG2)
        self._tree.tag_configure('downloading',foreground=BLUE)
        self._tree.tag_configure('merging',    foreground=YELLOW)

        # ── 하단 버튼 바 ──
        btn_bar = ttk.Frame(self, padding=(12, 6))
        btn_bar.pack(fill='x')

        self._start_btn = ttk.Button(btn_bar, text='▶ 시작',
                                     command=self._start_selected)
        self._start_btn.pack(side='left', padx=(0, 4))

        self._cancel_btn = ttk.Button(btn_bar, text='■ 취소',
                                      command=self._cancel_selected)
        self._cancel_btn.pack(side='left', padx=(0, 4))

        ttk.Button(btn_bar, text='🗑 완료 제거',
                   command=self._remove_done).pack(side='left', padx=(0, 4))

        ttk.Button(btn_bar, text='✕ 선택 제거',
                   command=self._remove_selected).pack(side='left', padx=(0, 4))

        ttk.Button(btn_bar, text='📂 폴더 열기',
                   command=self._open_folder).pack(side='right')

        ttk.Separator(self).pack(fill='x')

        # ── 로그 ──
        log_frame = ttk.Frame(self)
        log_frame.pack(fill='x', padx=12, pady=(4, 8))

        log_hdr = ttk.Frame(log_frame)
        log_hdr.pack(fill='x')
        ttk.Label(log_hdr, text='로그', foreground=FG2).pack(side='left')
        ttk.Button(log_hdr, text='지우기',
                   command=self._clear_log).pack(side='right')

        self._log = tk.Text(log_frame, height=6, bg=BG2, fg=FG2,
                            font=('Consolas', 8), insertbackground=FG,
                            relief='flat', state='disabled',
                            wrap='word', borderwidth=0)
        log_sb = ttk.Scrollbar(log_frame, orient='vertical',
                               command=self._log.yview)
        self._log.configure(yscrollcommand=log_sb.set)
        self._log.pack(side='left', fill='x', expand=True)
        log_sb.pack(side='right', fill='y')

        # 우클릭 메뉴
        self._ctx_menu = tk.Menu(self, tearoff=0,
                                 bg=BG2, fg=FG, activebackground=ACCENT,
                                 activeforeground='white')
        self._ctx_menu.add_command(label='▶ 시작',    command=self._start_selected)
        self._ctx_menu.add_command(label='■ 취소',    command=self._cancel_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label='✕ 제거',   command=self._remove_selected)
        self._ctx_menu.add_command(label='📂 폴더 열기', command=self._open_folder)
        self._tree.bind('<Button-3>', self._show_ctx)

    # ── URL 분석 ────────────────────────────────
    def _analyze_url(self):
        url = self._url_var.get().strip()
        if not url:
            self._log_msg('URL을 입력하세요.', YELLOW)
            return
        if not HAS_YTDLP:
            messagebox.showerror('오류', 'yt-dlp 를 먼저 설치하세요.\npip install yt-dlp')
            return
        if self._analyzing:
            return

        save_dir = self._dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning('경고', '저장 위치를 선택하세요.')
            return

        referer = self._referer_var.get().strip()
        browser = self._browser_var.get().strip()
        if browser == '없음':
            browser = ''

        self._analyzing = True
        self._analyze_btn.configure(state='disabled')
        self._analyze_lbl.configure(text='분석 중...')
        log_extra = ''
        if referer:
            log_extra += f'  [Referer: {referer}]'
        if browser:
            log_extra += f'  [쿠키: {browser}]'
        self._log_msg(f'분석: {url}' + log_extra)

        def _worker():
            # thread-safe 로그 헬퍼 (after로 메인 스레드에 전달)
            def _tlog(msg, color=FG2):
                self.after(0, lambda m=msg, c=color: self._log_msg(m, c))

            logger = _YtdlpLogger(
                on_warning=lambda m: _tlog(f'⚠ {m}', YELLOW),
                on_error  =lambda m: _tlog(f'✕ {m}', RED),
            )
            try:
                http_headers = {}
                if referer:
                    http_headers['Referer'] = referer
                opts = {
                    'quiet': True,
                    'no_warnings': False,
                    'nocheckcertificate': True,
                    'logger': logger,
                    **({'http_headers': http_headers} if http_headers else {}),
                    **({'cookiesfrombrowser': (browser, None, None, None)} if browser else {}),
                    **_impersonate_opts(),
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                self.after(0, lambda: self._on_analyze_done(url, info, save_dir, referer, browser))
            except Exception as e:
                err = str(e)
                self.after(0, lambda: self._on_analyze_error(err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_analyze_done(self, url: str, info: dict, save_dir: str,
                         referer: str = '', browser: str = ''):
        self._analyzing = False
        self._analyze_btn.configure(state='normal')
        self._analyze_lbl.configure(text='')
        if not info:
            self._log_msg('영상 정보를 가져올 수 없습니다.', RED)
            return
        self._log_msg(f'분석 완료: {info.get("title", url)}')
        FormatPickerDialog(
            self, url, info,
            on_confirm=lambda fmt, custom, label:
                self._enqueue(url, save_dir, fmt, custom, label, referer, browser)
        )

    def _on_analyze_error(self, err: str):
        self._analyzing = False
        self._analyze_btn.configure(state='normal')
        self._analyze_lbl.configure(text='')
        self._log_msg(f'분석 오류: {err}', RED)

    def _enqueue(self, url: str, save_dir: str,
                 fmt_str: str, custom_title: str, quality_label: str,
                 referer: str = '', cookie_browser: str = ''):
        """FormatPickerDialog 확인 후 대기열에 항목 추가"""
        os.makedirs(save_dir, exist_ok=True)
        uid  = str(uuid.uuid4())[:8]
        item = DownloadItem(
            uid=uid, url=url, save_dir=save_dir,
            quality=quality_label,
            format_str=fmt_str,
            custom_title=custom_title,
            title=custom_title or url,
            referer=referer,
            cookie_browser=cookie_browser,
        )
        self._items[uid] = item
        display = custom_title or _shorten_url(url)
        self._tree.insert('', 'end', iid=uid,
                          values=(display, quality_label, '0%', '', '', '', '대기'),
                          tags=('pending',))
        self._url_var.set('')
        self._log_msg(f'추가: {display}')

    # ── 이벤트 핸들러 ────────────────────────────
    def _paste_url(self):
        try:
            self._url_var.set(self.clipboard_get())
        except Exception:
            pass

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self._dir_var.get() or str(Path.home()))
        if d:
            self._dir_var.set(d)
            cfg = load_cfg()
            cfg['save_dir'] = d
            save_cfg(cfg)

    def _start_selected(self):
        sel = self._tree.selection()
        if not sel:
            sel = list(self._items.keys())
        for uid in sel:
            item = self._items.get(uid)
            if item and item.status == 'pending':
                self._run_download(item)

    def _run_download(self, item: DownloadItem):
        if item.uid in self._workers:
            return  # 이미 실행 중

        worker = Downloader(
            item,
            on_progress=lambda i:          self._ui_queue.put(('progress', i)),
            on_done    =lambda i:          self._ui_queue.put(('done',     i)),
            on_error   =lambda i:          self._ui_queue.put(('error',    i)),
            on_log     =lambda m, c=FG2:   self._ui_queue.put(('log', m, c)),
        )
        self._workers[item.uid] = worker
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        self._log_msg(f'시작: {item.url}')

    def _cancel_selected(self):
        sel = self._tree.selection()
        if not sel:
            return
        for uid in sel:
            w = self._workers.get(uid)
            if w:
                w.cancel()
                self._log_msg(f'취소 요청: {self._items[uid].url}')

    def _remove_done(self):
        for uid in list(self._items.keys()):
            item = self._items[uid]
            if item.status in ('done', 'cancelled', 'error'):
                if self._tree.exists(uid):
                    self._tree.delete(uid)
                del self._items[uid]
                self._workers.pop(uid, None)

    def _remove_selected(self):
        sel = self._tree.selection()
        for uid in sel:
            item = self._items.get(uid)
            if item:
                w = self._workers.get(uid)
                if w:
                    w.cancel()
                if self._tree.exists(uid):
                    self._tree.delete(uid)
                del self._items[uid]
                self._workers.pop(uid, None)

    def _open_folder(self):
        sel = self._tree.selection()
        uid = sel[0] if sel else None
        item = self._items.get(uid) if uid else None
        folder = item.save_dir if item else self._dir_var.get()
        if folder and os.path.isdir(folder):
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')

    def _show_ctx(self, e):
        row = self._tree.identify_row(e.y)
        if row:
            if row not in self._tree.selection():
                self._tree.selection_set(row)
        self._ctx_menu.post(e.x_root, e.y_root)

    def _clear_log(self):
        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')

    # ── UI 갱신 (메인 스레드) ─────────────────────
    def _poll_ui(self):
        try:
            while True:
                ev = self._ui_queue.get_nowait()
                event = ev[0]
                if event == 'log':
                    # ('log', msg, color)
                    self._log_msg(ev[1], ev[2])
                else:
                    # ('progress'|'done'|'error', item)
                    item = ev[1]
                    self._update_row(item)
                    if event == 'done':
                        self._log_msg(f'완료: {item.title or item.url}', GREEN)
                    elif event == 'error':
                        self._log_msg(f'오류: {item.error}', RED)
        except queue.Empty:
            pass
        self.after(200, self._poll_ui)

    def _update_row(self, item: DownloadItem):
        if not self._tree.exists(item.uid):
            return

        status_map = {
            'pending':     ('대기',     'pending'),
            'downloading': ('다운로드', 'downloading'),
            'merging':     ('병합 중',  'merging'),
            'done':        ('완료',     'done'),
            'error':       ('오류',     'error'),
            'cancelled':   ('취소됨',   'cancelled'),
        }
        status_label, tag = status_map.get(item.status, (item.status, ''))
        progress_str = f'{item.progress:.0f}%' if item.progress else '0%'

        display_title = (item.custom_title
                         or (item.title if item.title != item.url
                             else _shorten_url(item.url)))

        self._tree.item(item.uid,
                        values=(display_title, item.quality,
                                progress_str, item.speed, item.eta,
                                item.size, status_label),
                        tags=(tag,))

    def _log_msg(self, msg: str, color: str = FG2):
        t = time.strftime('%H:%M:%S')
        self._log.configure(state='normal')
        self._log.insert('end', f'[{t}] {msg}\n')
        self._log.see('end')
        self._log.configure(state='disabled')


def _fmt_duration(sec: int) -> str:
    """초 → H:MM:SS 또는 M:SS 문자열"""
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def _sanitize_filename(name: str) -> str:
    """파일명에 사용 불가한 문자 제거 후 최대 200자"""
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:200]


def _shorten_url(url: str, maxlen: int = 80) -> str:
    return url if len(url) <= maxlen else url[:maxlen - 3] + '...'


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────
if __name__ == '__main__':
    app = DownloaderApp()
    app.mainloop()
