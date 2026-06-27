"""
로컬 분류 시스템 — 독립 UI 모듈 (web_gallery.py 와 동일한 분리 패턴)

vidsort.py 의 메인 앱(self.db, self._reload)을 공유받아 별도 Toplevel 창으로 동작.
같은 vidsort.db / 같은 DB 인스턴스(락 공유)를 사용 — 별도 SQLite 커넥션을 열지 않는다.
"""

import json
import threading
import hashlib
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

from PIL import Image, ImageTk

from local_llm import LocalLLMClient, CLASSIFY_BATCH_MAX
from classify_util import top_k_similar, cluster_by_similarity

CFG_KEYS = ('local_llm_endpoint', 'local_llm_api_key',
            'local_llm_chat_model', 'local_llm_embed_model',
            'local_llm_custom_prompt')


def open_window(app, db_path, thumb_dir, load_cfg=None, save_cfg=None):
    """app — 메인 VidSort 인스턴스 (app.db, app._reload 재사용)
    load_cfg/save_cfg — vidsort.py 모듈의 vidsort_cfg.json 읽기/쓰기 함수 (공유)"""
    win = ClassifyWindow(app, db_path, thumb_dir, load_cfg, save_cfg)
    return win


class ClassifyWindow(tk.Toplevel):
    THUMB = 140

    def __init__(self, app, db_path, thumb_dir, load_cfg=None, save_cfg=None):
        super().__init__(app)
        self.app = app
        self.db = app.db          # 메인 앱과 동일한 DB 인스턴스 공유 (락 공유)
        self.thumb_dir = thumb_dir
        self._load_cfg_fn = load_cfg or (lambda: {})
        self._save_cfg_fn = save_cfg or (lambda cfg: None)
        self.title('🧠 로컬 분류 시스템')
        self.geometry('1280x820')
        self.configure(bg='#0d0d14')

        self.client = None
        self.cards = []           # CardWidget 리스트 (현재 화면)
        self.retry_queue = []     # 거부된 항목 (path, suggestion) 재시도 대기

        self._build_ui()
        self._load_settings()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ── UI 골격 ──────────────────────────────────
    def _build_ui(self):
        paned = tk.PanedWindow(self, orient='horizontal', bg='#0d0d14',
                                sashwidth=4, sashrelief='flat')
        paned.pack(fill='both', expand=True)

        left = tk.Frame(paned, bg='#13131f', width=320)
        paned.add(left, minsize=260)

        right = tk.Frame(paned, bg='#0d0d14')
        paned.add(right, minsize=600)

        self._build_settings_panel(left)
        self._build_grid_panel(right)

    def _build_settings_panel(self, parent):
        pad = dict(padx=10, pady=4)

        tk.Label(parent, text='로컬 LLM 설정', bg='#13131f', fg='#eee',
                 font=('Consolas', 11, 'bold')).pack(anchor='w', **pad)

        tk.Label(parent, text='엔드포인트', bg='#13131f', fg='#aaa').pack(anchor='w', padx=10)
        self.ep_var = tk.StringVar(value='http://127.0.0.1:8000/v1')
        tk.Entry(parent, textvariable=self.ep_var).pack(fill='x', padx=10, pady=2)

        tk.Label(parent, text='API 키 (없으면 빈칸)', bg='#13131f', fg='#aaa').pack(anchor='w', padx=10)
        self.key_var = tk.StringVar()
        tk.Entry(parent, textvariable=self.key_var, show='*').pack(fill='x', padx=10, pady=2)

        ttk.Button(parent, text='🔄 모델 새로고침', command=self._refresh_models)\
            .pack(fill='x', padx=10, pady=(8, 4))

        tk.Label(parent, text='채팅 모델', bg='#13131f', fg='#aaa').pack(anchor='w', padx=10)
        self.chat_model_var = tk.StringVar()
        self.chat_model_cb = ttk.Combobox(parent, textvariable=self.chat_model_var,
                                           values=[], state='readonly')
        self.chat_model_cb.pack(fill='x', padx=10, pady=2)

        tk.Label(parent, text='임베딩 모델', bg='#13131f', fg='#aaa').pack(anchor='w', padx=10)
        self.embed_model_var = tk.StringVar()
        self.embed_model_cb = ttk.Combobox(parent, textvariable=self.embed_model_var,
                                            values=[], state='readonly')
        self.embed_model_cb.pack(fill='x', padx=10, pady=2)

        ttk.Separator(parent).pack(fill='x', padx=10, pady=10)

        tk.Label(parent, text='대상 폴더', bg='#13131f', fg='#aaa').pack(anchor='w', padx=10)
        self.folder_var = tk.StringVar()
        self.folder_cb = ttk.Combobox(parent, textvariable=self.folder_var,
                                       values=self.db.all_folders(), state='readonly')
        self.folder_cb.pack(fill='x', padx=10, pady=2)

        tk.Label(parent, text='커스텀 프롬프트 (지침)', bg='#13131f', fg='#aaa').pack(
            anchor='w', padx=10, pady=(10, 0))
        self.prompt_txt = tk.Text(parent, height=8, bg='#0a0a14', fg='#eee',
                                   insertbackground='#eee', wrap='word')
        self.prompt_txt.pack(fill='x', padx=10, pady=2)

        ttk.Button(parent, text='▶ 분류 시작', style='Acc.TButton',
                   command=self._start_classify).pack(fill='x', padx=10, pady=(12, 4))
        ttk.Button(parent, text='🔁 거부된 항목 재분류',
                   command=self._retry_rejected).pack(fill='x', padx=10, pady=4)

        self.status_lbl = tk.Label(parent, text='', bg='#13131f', fg='#7c6ff7',
                                    wraplength=300, justify='left')
        self.status_lbl.pack(anchor='w', padx=10, pady=(10, 4))

    def _build_grid_panel(self, parent):
        top = tk.Frame(parent, bg='#0d0d14')
        top.pack(fill='x', padx=8, pady=6)
        ttk.Button(top, text='✅ 선택 일괄 적용', command=self._apply_selected)\
            .pack(side='left', padx=4)

        self.canvas = tk.Canvas(parent, bg='#0d0d14', highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True, padx=(8, 0), pady=4)
        vsb.pack(side='right', fill='y')

        self.inner = tk.Frame(self.canvas, bg='#0d0d14')
        self._inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.inner.bind('<Configure>',
                         lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',
                          lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.canvas.bind_all('<MouseWheel>',
                              lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), 'units'))

    # ── 설정 로드/저장 ───────────────────────────
    def _load_settings(self):
        cfg = self._load_cfg_fn()
        self.ep_var.set(cfg.get('local_llm_endpoint', 'http://127.0.0.1:8000/v1'))
        self.key_var.set(cfg.get('local_llm_api_key', ''))
        self.chat_model_var.set(cfg.get('local_llm_chat_model', ''))
        self.embed_model_var.set(cfg.get('local_llm_embed_model', ''))
        self.prompt_txt.insert('1.0', cfg.get('local_llm_custom_prompt', ''))

    def _save_settings(self):
        cfg = self._load_cfg_fn()
        cfg['local_llm_endpoint']     = self.ep_var.get().strip()
        cfg['local_llm_api_key']      = self.key_var.get().strip()
        cfg['local_llm_chat_model']   = self.chat_model_var.get().strip()
        cfg['local_llm_embed_model']  = self.embed_model_var.get().strip()
        cfg['local_llm_custom_prompt'] = self.prompt_txt.get('1.0', 'end').strip()
        self._save_cfg_fn(cfg)

    def _make_client(self):
        return LocalLLMClient(
            endpoint=self.ep_var.get().strip(),
            api_key=self.key_var.get().strip(),
            chat_model=self.chat_model_var.get().strip(),
            embed_model=self.embed_model_var.get().strip(),
        )

    def _refresh_models(self):
        client = self._make_client()

        def worker():
            try:
                models = client.list_models()
            except Exception as e:
                self.after(0, lambda: messagebox.showerror('모델 조회 실패', str(e)))
                return
            def apply():
                self.chat_model_cb['values'] = models
                self.embed_model_cb['values'] = models
                if models and not self.chat_model_var.get():
                    self.chat_model_var.set(models[0])
                if models and not self.embed_model_var.get():
                    self.embed_model_var.set(models[0])
            self.after(0, apply)
        threading.Thread(target=worker, daemon=True).start()

    # ── 분류 워크플로우 ──────────────────────────
    def _start_classify(self):
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning('알림', '대상 폴더를 선택하세요.')
            return
        self._save_settings()
        self.client = self._make_client()
        self._clear_grid()
        self.status_lbl.config(text='미태그 영상 조회 중...')

        custom_prompt = self.prompt_txt.get('1.0', 'end').strip()
        threading.Thread(target=self._classify_worker,
                          args=(folder, custom_prompt), daemon=True).start()

    def _classify_worker(self, folder, custom_prompt):
        items = self.db.get_untagged_in_folder(folder, limit=2000)
        if not items:
            self.after(0, lambda: self.status_lbl.config(text='미태그 영상이 없습니다.'))
            return

        names = [Path(it['path']).stem for it in items]
        try:
            vecs = self.client.embed_texts(names)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('임베딩 실패', str(e)))
            return
        if len(vecs) != len(items):
            vecs = [[] for _ in items]

        path_vec = list(zip([it['path'] for it in items], vecs))
        chunks = cluster_by_similarity(path_vec, max_group=CLASSIFY_BATCH_MAX, threshold=0.55)
        by_path = {it['path']: it for it in items}

        for ci, chunk_paths in enumerate(chunks):
            self.after(0, lambda ci=ci, n=len(chunks): self.status_lbl.config(
                text=f'분류 중... ({ci+1}/{n} 그룹)'))
            chunk_items = [by_path[p] for p in chunk_paths]
            self._classify_chunk(chunk_items, folder, custom_prompt)

        self.after(0, lambda: self.status_lbl.config(text='분류 완료.'))

    def _classify_chunk(self, chunk_items, folder, custom_prompt):
        filenames = [Path(it['path']).name for it in chunk_items]
        rejection_notes = {}
        for it in chunk_items:
            hist = self.db.get_rejections(it['path'])
            if hist:
                rejection_notes[Path(it['path']).name] = [
                    {'suggested': h['suggested'], 'comment': h['comment']} for h in hist]

        try:
            results = self.client.classify_videos(
                filenames, folder_ctx=folder,
                rejection_notes=rejection_notes, custom_prompt=custom_prompt)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror('분류 실패', str(e)))
            return

        raw_tags = sorted({t for r in results for t in r.get('tags', [])})
        final_map = self._normalize_raw_tags(raw_tags)

        for it, res in zip(chunk_items, results):
            final_tags = [final_map.get(t, t) for t in res.get('tags', [])]
            suggestion = {'category': res.get('category', ''), 'tags': final_tags}
            self.after(0, lambda it=it, s=suggestion: self._add_card(it, s))

    def _normalize_raw_tags(self, raw_tags):
        if not raw_tags:
            return {}
        existing = self.db.get_tag_embeddings()
        try:
            new_vecs = self.client.embed_texts(raw_tags)
        except Exception:
            new_vecs = [[] for _ in raw_tags]

        candidates = {}
        for rt, vec in zip(raw_tags, new_vecs):
            candidates[rt] = top_k_similar(vec, existing, k=5, threshold=0.75)

        final_map = self.client.normalize_tags(raw_tags, candidates)

        # 새로 채택된(후보 없이 그대로 쓰인) 태그는 임베딩 캐시에 즉시 저장
        for rt, vec in zip(raw_tags, new_vecs):
            if final_map.get(rt) == rt and vec:
                self.db.save_tag_embedding(rt, vec)
        return final_map

    # ── 카드 그리드 ──────────────────────────────
    def _clear_grid(self):
        for c in self.cards:
            c.frame.destroy()
        self.cards = []

    def _add_card(self, item, suggestion):
        card = ClassifyCard(self.inner, item, suggestion, self.thumb_dir, self)
        card.frame.grid(row=len(self.cards) // 4, column=len(self.cards) % 4,
                         padx=6, pady=6, sticky='n')
        self.cards.append(card)

    def apply_card(self, card):
        path = card.item['path']
        existing = self.db.get_file_categories(path)
        cur_tags = {t for t, in self.db.conn.execute(
            "SELECT tag FROM tags WHERE path=?", (path,)).fetchall()}
        if cur_tags:
            messagebox.showinfo('알림', '이미 태그가 있는 영상입니다 — 메인 화면에서 동시에 편집된 것 같습니다. 건너뜁니다.')
            card.frame.destroy()
            if card in self.cards:
                self.cards.remove(card)
            return
        category = card.cat_var.get().strip()
        tags = [t.strip() for t in card.tag_var.get().split(',') if t.strip()]
        with self.db.lock:
            if category:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO categories(name,description) VALUES(?,'')", (category,))
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO file_categories(path, category) VALUES(?,?)",
                    (path, category))
            for t in tags:
                self.db.conn.execute(
                    "INSERT OR IGNORE INTO tags(path, tag) VALUES(?,?)", (path, t))
            self.db.conn.commit()
        card.frame.destroy()
        if card in self.cards:
            self.cards.remove(card)

    def _apply_selected(self):
        for card in list(self.cards):
            if card.sel_var.get():
                self.apply_card(card)

    def reject_card(self, card, comment):
        path = card.item['path']
        suggestion = {'category': card.cat_var.get().strip(),
                       'tags': [t.strip() for t in card.tag_var.get().split(',') if t.strip()]}
        self.db.add_rejection(path, suggestion, comment)
        card.frame.destroy()
        if card in self.cards:
            self.cards.remove(card)

    def _retry_rejected(self):
        folder = self.folder_var.get().strip()
        if not folder:
            return
        self._save_settings()
        self.client = self._make_client()
        custom_prompt = self.prompt_txt.get('1.0', 'end').strip()
        self._clear_grid()
        threading.Thread(target=self._classify_worker,
                          args=(folder, custom_prompt), daemon=True).start()

    def _on_close(self):
        if hasattr(self.app, '_reload'):
            self.app._reload()
        self.destroy()


class ClassifyCard:
    THUMB = 140

    def __init__(self, parent, item, suggestion, thumb_dir, win):
        self.item = item
        self.win = win
        self.frame = tk.Frame(parent, bg='#13131f', bd=1, relief='solid')

        self.sel_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self.frame, variable=self.sel_var, bg='#13131f').pack(anchor='w')

        thumb_img = self._load_thumb(item['path'], thumb_dir)
        lbl = tk.Label(self.frame, image=thumb_img, bg='#0a0a14',
                        width=self.THUMB, height=self.THUMB)
        lbl.image = thumb_img  # GC 방지
        lbl.pack()

        name = Path(item['path']).name
        tk.Label(self.frame, text=name[:30], bg='#13131f', fg='#ddd',
                 font=('Consolas', 8), wraplength=self.THUMB).pack()

        self.cat_var = tk.StringVar(value=suggestion.get('category', ''))
        tk.Entry(self.frame, textvariable=self.cat_var, width=18).pack(pady=2)

        self.tag_var = tk.StringVar(value=', '.join(suggestion.get('tags', [])))
        tk.Entry(self.frame, textvariable=self.tag_var, width=18).pack(pady=2)

        btn_row = tk.Frame(self.frame, bg='#13131f')
        btn_row.pack(pady=4)
        ttk.Button(btn_row, text='적용', width=6,
                   command=lambda: win.apply_card(self)).pack(side='left', padx=2)
        ttk.Button(btn_row, text='제외', width=6,
                   command=self._on_reject).pack(side='left', padx=2)

    def _on_reject(self):
        comment = CommentDialog(self.frame).result
        self.win.reject_card(self, comment or '')

    def _load_thumb(self, path, thumb_dir):
        h = hashlib.md5(path.encode()).hexdigest()
        tf = Path(thumb_dir) / (h + '.jpg')
        try:
            img = Image.open(tf)
            img.thumbnail((self.THUMB, self.THUMB))
            return ImageTk.PhotoImage(img)
        except Exception:
            blank = Image.new('RGB', (self.THUMB, self.THUMB), '#0a0a14')
            return ImageTk.PhotoImage(blank)


class CommentDialog:
    """제외 코멘트 입력 (선택) — 작은 모달."""

    def __init__(self, parent):
        self.result = ''
        dlg = tk.Toplevel(parent)
        dlg.title('제외 사유 (선택)')
        dlg.geometry('360x140')
        tk.Label(dlg, text='이 제안을 거부하는 이유를 적으면 다음 시도에 참고합니다.').pack(
            anchor='w', padx=10, pady=(10, 4))
        txt = tk.Text(dlg, height=4)
        txt.pack(fill='x', padx=10, pady=4)

        def ok():
            self.result = txt.get('1.0', 'end').strip()
            dlg.destroy()
        ttk.Button(dlg, text='확인', command=ok).pack(pady=6)
        dlg.transient(parent)
        dlg.grab_set()
        dlg.wait_window()
