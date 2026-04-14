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
    uid:      str
    url:      str
    save_dir: str
    quality:  str          # 'best' / '1080' / '720' / '480' / 'audio'
    status:   str = 'pending'   # pending / downloading / done / error / cancelled
    title:    str = ''
    filename: str = ''
    progress: float = 0.0  # 0~100
    speed:    str = ''
    eta:      str = ''
    size:     str = ''
    error:    str = ''


# ──────────────────────────────────────────────
# yt-dlp 래퍼
# ──────────────────────────────────────────────
class Downloader:
    """단일 다운로드 실행기. 별도 스레드에서 실행."""

    def __init__(self, item: DownloadItem,
                 on_progress,   # (item) -> None
                 on_done,       # (item) -> None
                 on_error):     # (item) -> None
        self.item = item
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
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

        # 포맷 선택
        fmt = _quality_to_format(item.quality)

        ydl_opts = {
            'format':            fmt,
            'outtmpl':           os.path.join(item.save_dir, '%(title)s.%(ext)s'),
            'progress_hooks':    [self._hook],
            'postprocessor_hooks': [self._postproc_hook],
            'quiet':             True,
            'no_warnings':       True,
            'noprogress':        True,
            'ignoreerrors':      False,
            'nocheckcertificate': True,
            'retries':           3,
            'fragment_retries':  5,
            'concurrent_fragment_downloads': 4,
            'merge_output_format': 'mp4',
            # m3u8 / HLS 전용
            'hls_use_mpegts':    False,
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


def _quality_to_format(quality: str) -> str:
    mapping = {
        'best':  'bestvideo+bestaudio/best',
        '1080':  'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        '720':   'bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        '480':   'bestvideo[height<=480]+bestaudio/best[height<=480]/best',
        'audio': 'bestaudio/best',
    }
    return mapping.get(quality, 'bestvideo+bestaudio/best')


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

    # ── UI 빌드 ─────────────────────────────────
    def _build_ui(self):
        # ── 상단 입력 패널 ──
        top = ttk.Frame(self, padding=(12, 10, 12, 8))
        top.pack(fill='x')

        # URL 입력
        url_row = ttk.Frame(top)
        url_row.pack(fill='x', pady=(0, 6))

        ttk.Label(url_row, text='URL', width=8, anchor='w').pack(side='left')
        self._url_var = tk.StringVar()
        url_entry = ttk.Entry(url_row, textvariable=self._url_var)
        url_entry.pack(side='left', fill='x', expand=True, padx=(4, 4))
        url_entry.bind('<Return>', lambda e: self._add_to_queue())

        paste_btn = ttk.Button(url_row, text='붙여넣기',
                               command=self._paste_url)
        paste_btn.pack(side='left', padx=(0, 4))

        # 저장 경로
        dir_row = ttk.Frame(top)
        dir_row.pack(fill='x', pady=(0, 6))

        ttk.Label(dir_row, text='저장 위치', width=8, anchor='w').pack(side='left')
        self._dir_var = tk.StringVar()
        ttk.Entry(dir_row, textvariable=self._dir_var).pack(
            side='left', fill='x', expand=True, padx=(4, 4))
        ttk.Button(dir_row, text='폴더 선택',
                   command=self._pick_dir).pack(side='left', padx=(0, 4))

        # 품질 + 추가 버튼
        opt_row = ttk.Frame(top)
        opt_row.pack(fill='x')

        ttk.Label(opt_row, text='품질', width=8, anchor='w').pack(side='left')
        self._qual_var = tk.StringVar(value='best')
        qual_cb = ttk.Combobox(opt_row, textvariable=self._qual_var, width=16,
                               state='readonly',
                               values=['best', '1080', '720', '480', 'audio only'])
        qual_cb.pack(side='left', padx=(4, 12))

        add_btn = ttk.Button(opt_row, text='+ 대기열 추가',
                             style='Accent.TButton',
                             command=self._add_to_queue)
        add_btn.pack(side='left')

        # yt-dlp 미설치 경고
        if not HAS_YTDLP:
            warn = tk.Label(top,
                text='⚠  yt-dlp 가 설치되어 있지 않습니다. pip install yt-dlp',
                bg=BG, fg=YELLOW, font=('Segoe UI', 9))
            warn.pack(fill='x', pady=(4, 0))

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

    def _add_to_queue(self):
        url = self._url_var.get().strip()
        if not url:
            self._log_msg('URL을 입력하세요.', YELLOW)
            return
        if not HAS_YTDLP:
            messagebox.showerror('오류', 'yt-dlp 를 먼저 설치하세요.\npip install yt-dlp')
            return

        save_dir = self._dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning('경고', '저장 위치를 선택하세요.')
            return
        os.makedirs(save_dir, exist_ok=True)

        qual_str = self._qual_var.get()
        quality  = 'audio' if qual_str == 'audio only' else qual_str

        uid  = str(uuid.uuid4())[:8]
        item = DownloadItem(uid=uid, url=url, save_dir=save_dir,
                            quality=qual_str, title=url)
        self._items[uid] = item
        self._tree.insert('', 'end', iid=uid,
                          values=(url, qual_str, '0%', '', '', '', '대기'),
                          tags=('pending',))
        self._url_var.set('')
        self._log_msg(f'추가: {url}')

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

        qual_str = item.quality
        quality  = 'audio' if qual_str == 'audio only' else qual_str
        item.quality = quality  # normalize

        worker = Downloader(
            item,
            on_progress=lambda i: self._ui_queue.put(('progress', i)),
            on_done    =lambda i: self._ui_queue.put(('done',     i)),
            on_error   =lambda i: self._ui_queue.put(('error',    i)),
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
                event, item = self._ui_queue.get_nowait()
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

        display_title = item.title if item.title != item.url else _shorten_url(item.url)

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


def _shorten_url(url: str, maxlen: int = 80) -> str:
    return url if len(url) <= maxlen else url[:maxlen - 3] + '...'


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────
if __name__ == '__main__':
    app = DownloaderApp()
    app.mainloop()
