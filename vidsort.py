"""
VidSort v5
★ 필터/정렬/페이징 전부 SQL (Python sorted 제거 → 누락 버그 완전 해결)
★ 포맷 체크박스 (사이드바 하단, 적용 버튼)
★ Canvas 기반 렌더링
★ 썸네일 이미지 전역 캐시 (128GB RAM 활용)
★ 기존 vidsort.db / .thumbs 완전 호환
"""

import os, sys, json, shutil, hashlib, threading, subprocess
import sqlite3, time
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

# ─────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────
_BASE     = Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
THUMB_DIR = _BASE / ".thumbs"
DB_PATH   = _BASE / "vidsort.db"
CFG_PATH  = _BASE / "vidsort_cfg.json"

VIDEO_EXTS = {'.mp4','.mkv','.avi','.mov','.wmv','.webm',
              '.flv','.m4v','.3gp','.ts','.mts','.vob','.rmvb','.divx'}

# 포맷 체크박스 목록 (표시명, 확장자)
FORMAT_LIST = [
    ('MP4',   '.mp4'),  ('MKV',  '.mkv'),  ('AVI',  '.avi'),
    ('MOV',   '.mov'),  ('WMV',  '.wmv'),  ('WEBM', '.webm'),
    ('FLV',   '.flv'),  ('M4V',  '.m4v'),  ('TS',   '.ts'),
    ('MTS',   '.mts'),  ('VOB',  '.vob'),  ('RMVB', '.rmvb'),
    ('3GP',   '.3gp'),  ('DIVX', '.divx'),
]

MIN_NAME_LEN = 2
PAGE_SIZE    = 500
PREFETCH_WORKERS = 4   # 다음 페이지 미리 캐싱 스레드 수

THUMB_SIZES = [
    (120, 68), (160, 90), (224, 140), (320, 180), (480, 270),
]
DEFAULT_THUMB_STEP = 2

CARD_PAD = 6

# ─────────────────────────────────────────────────────
#  LONG PATH  (Windows MAX_PATH 우회)
# ─────────────────────────────────────────────────────
def longpath(p: str) -> str:
    """Windows에서 260자 이상 경로를 \\\\?\\ 접두어로 처리
    NAS/UNC 경로(// 또는 \\\\로 시작)는 그대로 반환"""
    if sys.platform == 'win32':
        # UNC 경로 (NAS) 는 건드리지 않음
        if p.startswith('//') or p.startswith('\\\\'):
            return p
        if not p.startswith('\\\\?\\'):
            return '\\\\?\\' + str(Path(p).resolve())
    return p

# ─────────────────────────────────────────────────────
#  CONFIG  (포맷 체크 상태 저장)
# ─────────────────────────────────────────────────────
def load_cfg():
    try:
        return json.loads(CFG_PATH.read_text(encoding='utf-8'))
    except:
        return {}

def save_cfg(cfg):
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    except:
        pass

# ─────────────────────────────────────────────────────
#  FFMPEG
# ─────────────────────────────────────────────────────
def _find(name):
    for cmd in [name, name+'.exe']:
        try:
            if subprocess.run([cmd,'-version'],capture_output=True,timeout=5).returncode==0:
                return cmd
        except: pass
    p = _BASE/(name+'.exe')
    return str(p) if p.exists() else None

FFMPEG  = _find('ffmpeg')
FFPROBE = _find('ffprobe')
_NW = subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0

def make_thumb(src, dst):
    if not FFMPEG or not FFPROBE: return False,0,0,0.0
    try:
        r = subprocess.run(
            [FFPROBE,'-v','error',
             '-show_entries','format=duration:stream=width,height',
             '-of','json', src],
            capture_output=True, text=True, timeout=15, creationflags=_NW)
        d   = json.loads(r.stdout)
        fmt = d.get('format',{})
        ss  = d.get('streams',[{}])
        vs  = next((s for s in ss if s.get('width')), ss[0] if ss else {})
        dur = float(fmt.get('duration') or 0)
        vw  = int(vs.get('width')  or 0)
        vh  = int(vs.get('height') or 0)
        if vw and vh:
            ratio=vw/vh; tw=min(480,vw); th=int(tw/ratio)
            if th>270: tw=int(vw*(270/vh)); th=270
        else: tw,th=224,140
        seek=max(1.0,dur*0.4) if dur>5 else 1.0
        r2=subprocess.run(
            [FFMPEG,'-ss',str(seek),'-i',src,
             '-vframes','1','-vf',f'scale={tw}:{th}','-y',dst],
            capture_output=True,timeout=30,creationflags=_NW)
        return r2.returncode==0 and Path(dst).exists(), vw, vh, dur
    except: return False,0,0,0.0

# ─────────────────────────────────────────────────────
#  DATABASE  — 모든 필터/정렬/페이징을 SQL로
# ─────────────────────────────────────────────────────
class DB:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA cache_size=-65536")   # 64MB
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.lock = threading.Lock()
        self._init()

    def _init(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY, name TEXT NOT NULL, alias TEXT DEFAULT '',
            size INTEGER DEFAULT 0, mtime REAL DEFAULT 0,
            duration REAL DEFAULT 0, width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0, thumb_ok INTEGER DEFAULT 0,
            folder TEXT DEFAULT '', added_at REAL DEFAULT 0,
            ext TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS tags(
            path TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(path,tag)
        );
        CREATE INDEX IF NOT EXISTS ix_folder   ON files(folder);
        CREATE INDEX IF NOT EXISTS ix_name     ON files(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS ix_size     ON files(size);
        CREATE INDEX IF NOT EXISTS ix_added    ON files(added_at);
        CREATE INDEX IF NOT EXISTS ix_duration ON files(duration);
        CREATE INDEX IF NOT EXISTS ix_tag      ON tags(path);
        CREATE INDEX IF NOT EXISTS ix_tag_tag  ON tags(tag);
        """)
        self.conn.commit()

        # ── ext 컬럼 마이그레이션 ──────────────────
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(files)").fetchall()]
        if 'ext' not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN ext TEXT DEFAULT ''")
            self.conn.commit()

        # ext 인덱스 (컬럼 확보 후 생성)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_ext ON files(ext)")
        self.conn.commit()

        # 기존 데이터 ext 채우기
        empty = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE ext='' OR ext IS NULL").fetchone()[0]
        if empty > 0:
            self.conn.execute("""
                UPDATE files
                SET ext = LOWER(SUBSTR(name, INSTR(name, '.')))
                WHERE ext='' OR ext IS NULL
            """)
            self.conn.commit()

        self._cols = [c[0] for c in
                      self.conn.execute("SELECT * FROM files LIMIT 0").description]

    def _dicts(self, rows):
        return [dict(zip(self._cols, r)) for r in rows]

    # ── 핵심: SQL 기반 페이지 쿼리 ─────────────
    def query_page(self, active_exts, folder, tag, sort, short_filter,
                   search, offset, limit, min_dur=0):
        import sys
        print(f"[query_page] active_exts={active_exts} folder={folder} "
              f"tag={tag} sort={sort} short={short_filter} "
              f"search={search} offset={offset} limit={limit} min_dur={min_dur}",
              flush=True, file=sys.stderr)
        """
        모든 필터/정렬/페이징을 SQL에서 처리.
        active_exts: 표시할 확장자 리스트 e.g. ['.mp4','.mkv']
        반환: (rows, total_count)
        """
        params = []
        where  = []

        # 1) 포맷 필터 — ext IN (?,?,?) 로 정확하게
        if active_exts:
            ext_list = sorted(active_exts)
            ph = ','.join('?' * len(ext_list))
            where.append(f"f.ext IN ({ph})")
            params.extend(ext_list)
        else:
            return [], 0

        # 2) 2글자 이하 무시 — ext 앞부분(stem)만 체크
        if short_filter:
            where.append("LENGTH(SUBSTR(f.name, 1, INSTR(f.name,'.')-1)) > 2")

        # 2-b) N초 이하 제외
        if min_dur and min_dur > 0:
            where.append("f.duration >= ?")
            params.append(float(min_dur))

        # 3) 폴더 필터
        if folder:
            where.append("f.folder = ?")
            params.append(folder)

        # 4) 태그/검색 JOIN
        use_tag_join = bool(tag or search)
        if search:
            lq = f'%{search.lower()}%'
            where.append(
                "(LOWER(f.name) LIKE ? OR LOWER(f.alias) LIKE ? OR LOWER(t.tag) LIKE ?)")
            params += [lq, lq, lq]
        if tag:
            where.append("t.tag = ?")
            params.append(tag)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        join_sql  = "LEFT JOIN tags t ON f.path=t.path" if use_tag_join else ""

        # 5) 정렬
        sort_map = {
            '이름':    'f.name COLLATE NOCASE ASC',
            '크기':    'f.size DESC',
            '날짜추가': 'f.added_at DESC',
            '재생시간': 'f.duration DESC',
        }
        order_sql = sort_map.get(sort, 'f.name COLLATE NOCASE ASC')

        # tag JOIN 있을 때 중복 방지 — 서브쿼리로 처리 (DISTINCT + ORDER BY 충돌 완전 회피)
        if use_tag_join:
            base_sql = f"""
                SELECT DISTINCT f.path {join_sql and f'FROM files f {join_sql}' or 'FROM files f'}
                {where_sql}
            """
            count_sql = f"SELECT COUNT(*) FROM ({base_sql})"
            data_sql  = f"""
                SELECT f.* FROM files f
                WHERE f.path IN ({base_sql})
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
            """
        else:
            base      = f"FROM files f {where_sql}"
            count_sql = f"SELECT COUNT(*) {base}"
            data_sql  = f"""
                SELECT f.* {base}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
            """

        total = self.conn.execute(count_sql, params).fetchone()[0]
        rows  = self.conn.execute(data_sql, params + [limit, offset]).fetchall()
        print(f"[query_page] total={total} rows={len(rows)} sql={data_sql.strip()[:120]}", flush=True, file=sys.stderr)
        return self._dicts(rows), total

    def get_tags_for_paths(self, paths):
        if not paths: return {}
        ph = ','.join('?'*len(paths))
        res = defaultdict(list)
        for p,t in self.conn.execute(
                f"SELECT path,tag FROM tags WHERE path IN ({ph})", paths).fetchall():
            res[p].append(t)
        return dict(res)

    def all_tags_map(self):
        res = defaultdict(list)
        for p,t in self.conn.execute("SELECT path,tag FROM tags").fetchall():
            res[p].append(t)
        return dict(res)

    def upsert(self, row):
        with self.lock:
            # ext 자동 계산
            row['ext'] = Path(row['name']).suffix.lower()
            self.conn.execute("""
            INSERT INTO files(path,name,alias,size,mtime,duration,width,height,
                              thumb_ok,folder,added_at,ext)
            VALUES(:path,:name,:alias,:size,:mtime,:duration,:width,:height,
                   :thumb_ok,:folder,:added_at,:ext)
            ON CONFLICT(path) DO UPDATE SET
              name=excluded.name, size=excluded.size, mtime=excluded.mtime,
              folder=excluded.folder, ext=excluded.ext,
              duration=CASE WHEN excluded.duration>0 THEN excluded.duration ELSE duration END,
              width   =CASE WHEN excluded.width>0    THEN excluded.width    ELSE width    END,
              height  =CASE WHEN excluded.height>0   THEN excluded.height   ELSE height   END,
              thumb_ok=excluded.thumb_ok
            """, row); self.conn.commit()

    def update_thumb(self,path,w,h,dur):
        with self.lock:
            self.conn.execute(
                "UPDATE files SET thumb_ok=1,width=?,height=?,duration=? WHERE path=?",
                (w,h,dur,path)); self.conn.commit()

    def set_alias(self,path,alias):
        with self.lock:
            self.conn.execute("UPDATE files SET alias=? WHERE path=?",(alias,path))
            self.conn.commit()

    def remove(self,path):
        with self.lock:
            self.conn.execute("DELETE FROM files WHERE path=?",(path,))
            self.conn.execute("DELETE FROM tags  WHERE path=?",(path,))
            self.conn.commit()

    def exists_folder(self,folder):
        return bool(self.conn.execute(
            "SELECT 1 FROM files WHERE folder=? LIMIT 1",(folder,)).fetchone())

    def random_untagged(self, active_exts, limit=100, min_dur=0):
        """태그/별칭 없는 파일 중 랜덤 N개 — 오늘의 추천용"""
        if not active_exts: return []
        ext_list = sorted(active_exts)
        ph = ','.join('?'*len(ext_list))
        dur_clause = "AND f.duration >= ?" if min_dur and min_dur > 0 else ""
        dur_params = [float(min_dur)] if min_dur and min_dur > 0 else []
        rows = self.conn.execute(f"""
            SELECT f.* FROM files f
            WHERE f.ext IN ({ph})
              {dur_clause}
              AND (f.alias IS NULL OR f.alias='')
              AND f.path NOT IN (SELECT DISTINCT path FROM tags)
            ORDER BY RANDOM()
            LIMIT ?
        """, ext_list + dur_params + [limit]).fetchall()
        return self._dicts(rows)

    def all_folders(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT folder FROM files ORDER BY folder").fetchall()]

    def all_tags(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()]

    def get_tags(self,path):
        return [r[0] for r in self.conn.execute(
            "SELECT tag FROM tags WHERE path=? ORDER BY tag",(path,)).fetchall()]

    def add_tag(self,path,tag):
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO tags VALUES(?,?)",(path,tag))
            self.conn.commit()

    def remove_tag(self,path,tag):
        with self.lock:
            self.conn.execute("DELETE FROM tags WHERE path=? AND tag=?",(path,tag))
            self.conn.commit()

    def get_all_for_thumbs(self, folder=None):
        if folder:
            rows = self.conn.execute(
                "SELECT path,name,folder,thumb_ok FROM files WHERE folder=?",
                (folder,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT path,name,folder,thumb_ok FROM files").fetchall()
        return [{'path':r[0],'name':r[1],'folder':r[2],'thumb_ok':r[3]} for r in rows]

    def folder_stats(self):
        rows = self.conn.execute("""
            SELECT folder,
                   COUNT(*) as cnt,
                   SUM(size) as total_size,
                   SUM(thumb_ok) as thumbed
            FROM files GROUP BY folder ORDER BY folder
        """).fetchall()
        return [{'folder':r[0],'count':r[1],'size':r[2],'thumbed':r[3]} for r in rows]

    def close(self): self.conn.close()

# ─────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────
def thumb_file(path):
    THUMB_DIR.mkdir(exist_ok=True)
    return THUMB_DIR/(hashlib.md5(path.encode()).hexdigest()+'.jpg')

# 썸네일 존재 세트 — 앱 시작 시 한 번만 스캔, 이후 세트 조회만
_thumb_exists: set = set()

def build_thumb_cache():
    """THUMB_DIR 스캔해서 존재하는 해시 세트 구축"""
    global _thumb_exists
    if THUMB_DIR.exists():
        _thumb_exists = {f.stem for f in THUMB_DIR.iterdir() if f.suffix=='.jpg'}

def thumb_cached(path: str) -> bool:
    return hashlib.md5(path.encode()).hexdigest() in _thumb_exists

def register_thumb(path: str):
    _thumb_exists.add(hashlib.md5(path.encode()).hexdigest())

def fmt_size(n):
    if not n: return '0B'
    if n>=1e12: return f'{n/1e12:.1f}TB'
    if n>=1e9:  return f'{n/1e9:.1f}GB'
    if n>=1e6:  return f'{n/1e6:.1f}MB'
    return f'{n/1e3:.0f}KB'

def fmt_dur(s):
    if not s: return ''
    s=int(s); h,m,sec=s//3600,(s%3600)//60,s%60
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'

# ─────────────────────────────────────────────────────
#  CANVAS GRID
# ─────────────────────────────────────────────────────
class CanvasGrid(tk.Frame):
    FONT_NAME  = ('Consolas', 9)
    FONT_ALIAS = ('Consolas', 9, 'bold')
    FONT_META  = ('Consolas', 7)
    FONT_TAG   = ('Consolas', 7)
    TAG_BG     = '#7c6ff7'
    CARD_BG    = '#13131f'
    CARD_SEL   = '#7c6ff7'
    CARD_NORM  = '#2a2a3d'
    THUMB_BG   = '#0a0a14'

    def __init__(self, parent, on_open, on_ctx, on_click, **kw):
        super().__init__(parent, bg='#0d0d14', **kw)
        self.on_open  = on_open
        self.on_ctx   = on_ctx
        self.on_click = on_click

        self.cv  = tk.Canvas(self, bg='#0d0d14', highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient='vertical', command=self.cv.yview)
        self.cv.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side='right', fill='y')
        self.cv.pack(fill='both', expand=True)

        self._videos    = []
        self._tags_map  = {}
        self._sel       = set()
        self._cols      = 4
        self._tw        = 224
        self._th        = 140
        self._card_w    = 240
        self._card_h    = 210
        self._img_cache = {}
        self._phs       = {}
        self._items     = {}
        self._path_map  = {}
        self._draw_gen  = 0
        self._tip_win   = None

        self.cv.bind('<Configure>',       self._on_resize)
        self.cv.bind('<MouseWheel>',      self._on_wheel)
        self.cv.bind('<Button-4>',        self._on_wheel)
        self.cv.bind('<Button-5>',        self._on_wheel)
        self.cv.bind('<Button-1>',        self._on_click)
        self.cv.bind('<Double-Button-1>', self._on_dbl)
        self.cv.bind('<Button-3>',        self._on_rclick)
        self.cv.bind('<Motion>',          self._on_motion)
        self.cv.bind('<Leave>',           self._hide_tip)

    def load(self, videos, tags_map, sel, tw, th, img_cache,
             debug=False, page_offset=0):
        self._videos     = videos
        self._tags_map   = tags_map
        self._sel        = sel
        self._tw         = tw
        self._th         = th
        self._card_w     = tw + 20
        self._card_h     = th + 72
        self._img_cache  = img_cache
        self._debug      = debug
        self._page_offset= page_offset
        self._draw()

    def hard_load(self, videos, tags_map, sel, tw, th, img_cache,
                  debug=False, page_offset=0):
        """동기 렌더 — after() 없이 전부 한 번에 그림. 새로고침용."""
        self._videos     = videos
        self._tags_map   = tags_map
        self._sel        = sel
        self._tw         = tw
        self._th         = th
        self._card_w     = tw + 20
        self._card_h     = th + 72
        self._img_cache  = img_cache
        self._debug      = debug
        self._page_offset= page_offset

        # gen 올려서 혹시 남아있는 after 콜백 무효화
        self._draw_gen += 1

        self.cv.delete('all')
        self._phs.clear()
        self._items.clear()
        self._path_map.clear()

        n = len(videos)
        if not n:
            self.cv.create_text(400, 200, text='표시할 파일이 없습니다',
                                fill='#444', font=('Consolas', 14))
            return

        self._cols  = max(1, self.cv.winfo_width() // (self._card_w + CARD_PAD))
        rows_n      = (n + self._cols - 1) // self._cols
        total_h     = CARD_PAD + rows_n * (self._card_h + CARD_PAD)
        self.cv.configure(scrollregion=(0, 0,
            self._cols * (self._card_w + CARD_PAD), total_h))

        # 100개마다 update_idletasks로 UI 숨쉬게 하면서 전부 그리기
        for i, v in enumerate(videos):
            self._draw_card(i, v)
            if i % 100 == 99:
                self.cv.update_idletasks()
        """디버그 오버레이만 토글 — 카드 전체 재그리기 없음"""
        self._debug = on
        if on:
            self.cv.itemconfig('dbg_overlay', state='normal')
        else:
            self.cv.itemconfig('dbg_overlay', state='hidden')

    def refresh_thumb(self, path):
        tf = thumb_file(path)
        if not tf.exists(): return
        tw, th = self._tw, self._th
        cache_key = f'{path}_{tw}_{th}'
        try:
            img = Image.open(tf)
            iw,ih = img.size
            scale = min(tw/iw, th/ih) if iw and ih else 1
            nw,nh = max(1,int(iw*scale)), max(1,int(ih*scale))
            img = img.resize((nw,nh), Image.LANCZOS)
            ph  = ImageTk.PhotoImage(img)
            self._img_cache[cache_key] = ph
            self._phs[path] = ph
        except: return

        idx = next((i for i,v in enumerate(self._videos) if v['path']==path), None)
        if idx is None: return
        x,y    = self._card_xy(idx)
        ph_key = hashlib.md5(path.encode()).hexdigest()[:16]
        th_tag = f't{ph_key}'
        c_tag  = f'c{ph_key}'
        self.cv.delete(th_tag)
        cx = x+self._card_w//2; cy = y+th//2+4
        iid = self.cv.create_image(cx,cy,image=ph,anchor='center',
                                   tags=(th_tag, c_tag))
        self._path_map[iid] = path

    def update_sel(self, sel):
        old = self._sel; self._sel = sel
        for path in old | sel:
            if path in self._items and self._items[path]:
                self.cv.itemconfig(
                    self._items[path][0],
                    outline=self.CARD_SEL if path in sel else self.CARD_NORM)

    def _card_xy(self, idx):
        col = idx % max(self._cols,1)
        row = idx // max(self._cols,1)
        return CARD_PAD + col*(self._card_w+CARD_PAD), \
               CARD_PAD + row*(self._card_h+CARD_PAD)

    def _on_resize(self, e):
        self._cols = max(1, e.width // (self._card_w+CARD_PAD))
        self._draw()

    def _on_wheel(self, e):
        delta = -3 if (e.num==4 or (e.num not in(4,5) and e.delta>0)) else 3
        if e.num not in (4,5): delta = -1*(e.delta//120)*3
        self.cv.yview_scroll(delta,'units')

    def _draw(self):
        self._draw_gen += 1          # 먼저 증가 → 이전 after 콜백 전부 무효화
        gen = self._draw_gen         # 이 배치의 고유 세대 번호
        self.cv.delete('all')
        self._phs.clear()
        self._items.clear()
        self._path_map.clear()

        n = len(self._videos)
        if not n:
            self.cv.create_text(400,200,text='표시할 파일이 없습니다',
                                fill='#444',font=('Consolas',14))
            return

        self._cols = max(1, self.cv.winfo_width() // (self._card_w+CARD_PAD))
        rows    = (n+self._cols-1)//self._cols
        total_h = CARD_PAD + rows*(self._card_h+CARD_PAD)
        self.cv.configure(scrollregion=(0,0,
            self._cols*(self._card_w+CARD_PAD), total_h))
        self._draw_batch(0, gen, list(self._videos))

    def _draw_batch(self, start, gen, videos):
        if gen != self._draw_gen: return
        end = min(start+80, len(videos))
        for i in range(start, end):
            self._draw_card(i, videos[i])
        self.cv.update_idletasks()
        if end < len(videos):
            self.cv.after(0, lambda: self._draw_batch(end, gen, videos))

    def _draw_card(self, idx, v):
        path     = v['path']
        x,y      = self._card_xy(idx)
        tags     = self._tags_map.get(path, [])
        alias    = v.get('alias','')
        sel      = path in self._sel
        # 경로의 특수문자(/ . () 등)가 tkinter 태그에서 오류 유발 → md5 해시 사용
        ph_key   = hashlib.md5(path.encode()).hexdigest()[:16]
        card_tag = f'c{ph_key}'
        tw,th    = self._tw, self._th
        cw,ch    = self._card_w, self._card_h
        abs_idx  = self._page_offset + idx

        # 카드 배경
        bid = self.cv.create_rectangle(
            x,y,x+cw,y+ch,
            fill=self.CARD_BG,
            outline=self.CARD_SEL if sel else self.CARD_NORM,
            width=2,tags=(card_tag,))
        self._path_map[bid] = path

        self.cv.create_rectangle(
            x+2,y+2,x+cw-2,y+th+4,
            fill=self.THUMB_BG,outline='',tags=(card_tag,))

        # 썸네일
        th_tag    = f't{ph_key}'
        cache_key = f'{path}_{tw}_{th}'
        if cache_key not in self._img_cache and thumb_cached(path):
            tf = thumb_file(path)
            try:
                img = Image.open(tf)
                iw,ih = img.size
                scale = min(tw/iw,th/ih) if iw and ih else 1
                nw,nh = max(1,int(iw*scale)),max(1,int(ih*scale))
                img = img.resize((nw,nh),Image.LANCZOS)
                ph  = ImageTk.PhotoImage(img)
                self._img_cache[cache_key] = ph
                self._phs[path] = ph
            except: pass

        cx=x+cw//2; cy=y+th//2+4
        if cache_key in self._img_cache:
            iid=self.cv.create_image(cx,cy,image=self._img_cache[cache_key],
                                     anchor='center',tags=(th_tag,card_tag))
        else:
            txt='⏳' if not v.get('thumb_ok') else '🎞️'
            iid=self.cv.create_text(cx,cy,text=txt,fill='#444',
                                    font=('Consolas',20),tags=(th_tag,card_tag))
        self._path_map[iid] = path

        # 재생시간 배지
        if v.get('duration'):
            d=fmt_dur(v['duration']); bw=len(d)*6+8
            self.cv.create_rectangle(x+cw-bw-2,y+th-14,x+cw-2,y+th+2,
                                     fill='#000',outline='',tags=(card_tag,))
            self.cv.create_text(x+cw-bw//2-2,y+th-6,text=d,fill='#ccc',
                                font=self.FONT_META,tags=(card_tag,))

        # 디버그 오버레이 — 항상 그리되 state로 show/hide
        folder_name = Path(path).parent.name
        dbg_txt  = f'#{abs_idx+1}  {folder_name}'
        dbg_state= 'normal' if getattr(self,'_debug',False) else 'hidden'
        self.cv.create_rectangle(x+2,y+2,x+2+len(dbg_txt)*6+8,y+16,
                                 fill='#000000',outline='',
                                 state=dbg_state,
                                 tags=(card_tag,'dbg_overlay'))
        self.cv.create_text(x+6,y+9,text=dbg_txt,fill='#4dffb4',
                            font=('Consolas',7),anchor='w',
                            state=dbg_state,
                            tags=(card_tag,'dbg_overlay'))

        # 태그
        ty=y+th+8; tx=x+6
        for t in tags[:5]:
            tw2=len(t)*7+10
            self.cv.create_rectangle(tx,ty,tx+tw2,ty+14,
                                     fill=self.TAG_BG,outline='',tags=(card_tag,))
            self.cv.create_text(tx+tw2//2,ty+7,text=t,fill='#fff',
                                font=self.FONT_TAG,tags=(card_tag,))
            tx+=tw2+3

        # 텍스트
        ty2=y+th+26
        if alias:
            aid=self.cv.create_text(x+6,ty2,text=alias[:30],
                                    fill='#e0e0ff',font=self.FONT_ALIAS,
                                    anchor='nw',tags=(card_tag,))
            self._path_map[aid]=path; ty2+=14

        short=v['name'][:30]+'…' if len(v['name'])>30 else v['name']
        nid=self.cv.create_text(x+6,ty2,text=short,
                                fill='#777' if alias else '#bbb',
                                font=self.FONT_NAME,anchor='nw',tags=(card_tag,))
        self._path_map[nid]=path; ty2+=13

        self.cv.create_text(x+6,ty2,text=Path(path).parent.name,
                            fill='#383858',font=self.FONT_META,
                            anchor='nw',tags=(card_tag,))
        ty2+=12

        res  = f"{v.get('width',0)}×{v.get('height',0)}" if v.get('width') else ''
        meta = ' '.join(filter(None,[fmt_size(v['size']),res]))
        self.cv.create_text(x+6,ty2,text=meta,fill='#2a2a48',
                            font=self.FONT_META,anchor='nw',tags=(card_tag,))

        self._items[path] = list(self.cv.find_withtag(card_tag))

    def set_debug(self, on: bool):
        """디버그 오버레이만 토글 — 카드 재그리기 없음"""
        self._debug = on
        state = 'normal' if on else 'hidden'
        self.cv.itemconfig('dbg_overlay', state=state)

    def _path_at(self, x, y):
        # 뷰포트 좌표 → 캔버스 전체 좌표로 변환 (스크롤 오프셋 반영)
        cx = self.cv.canvasx(x)
        cy = self.cv.canvasy(y)
        for iid in reversed(self.cv.find_overlapping(cx, cy, cx, cy)):
            p = self._path_map.get(iid)
            if p: return p
        return None

    def _on_click(self,e):
        p=self._path_at(e.x,e.y)
        if p: self.on_click(e,p)

    def _on_dbl(self,e):
        p=self._path_at(e.x,e.y)
        if p: self.on_open(p)

    def _on_rclick(self,e):
        p=self._path_at(e.x,e.y)
        if p: self.on_ctx(e,p)

    def _on_motion(self,e):
        p=self._path_at(e.x,e.y)
        if p:
            v=next((x for x in self._videos if x['path']==p),None)
            if v: self._show_tip(e.x_root,e.y_root,v['name']); return
        self._hide_tip()

    def _show_tip(self,rx,ry,text):
        if self._tip_win:
            try: self._tip_win.wm_geometry(f'+{rx+12}+{ry+16}'); return
            except: pass
        self._tip_win=tk.Toplevel(self)
        self._tip_win.wm_overrideredirect(True)
        self._tip_win.wm_geometry(f'+{rx+12}+{ry+16}')
        tk.Label(self._tip_win,text=text,bg='#1a1a28',fg='#ddd',
                 font=('Consolas',8),padx=8,pady=4,relief='solid',bd=1).pack()

    def _hide_tip(self,e=None):
        if self._tip_win:
            try: self._tip_win.destroy()
            except: pass
            self._tip_win=None

# ─────────────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────────────
class VidSort(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('VidSort v5 — 동영상 아카이브')
        self.geometry('1400x900'); self.minsize(900,600)
        self.configure(bg='#0d0d14')
        self._style()
        THUMB_DIR.mkdir(exist_ok=True)

        self.db        = DB()
        self._scan_stop  = threading.Event()  # 스캔 전용
        self._thumb_stop = threading.Event()  # 썸네일 전용
        self._stop       = self._scan_stop    # 하위 호환용 alias
        self._clipboard= []
        self._sel      = set()
        self._videos   = []       # 현재 페이지 데이터
        self._total    = 0        # 현재 필터 전체 카운트
        self._offset   = 0
        self._tags_map = {}       # 현재 페이지 태그맵
        self._img_cache= {}       # path_tw_th -> PhotoImage (전역)
        self._nav_bar  = None
        self._folder_paths = []

        # 설정 로드
        cfg = load_cfg()

        # ── LLM 설정 ──────────────────────────────
        self._llm_token    = cfg.get('llm_token', '')
        self._llm_model    = cfg.get('llm_model', 'claude-sonnet-4.5')
        self._llm_endpoint = cfg.get('llm_endpoint',
                                     'https://api.githubcopilot.com')
        self._llm_tag_pool = cfg.get('llm_tag_pool',
                                     ['애니', '영화', '드라마', '예능', '다큐', '기타'])
        self._llm_prompt   = cfg.get('llm_prompt', '')  # 비어 있으면 llm_api 기본값 사용
        self._llm_stop     = threading.Event()

        # 포맷 체크박스 변수 (설정에서 복원)
        self._fmt_vars = {}
        saved_fmts = cfg.get('formats', {})
        for label, ext in FORMAT_LIST:
            # 저장된 값 있으면 복원, 없으면 기본 ON
            # webm/ts는 기본 OFF
            default = ext not in ('.webm','.ts')
            val = saved_fmts.get(ext, default)
            self._fmt_vars[ext] = tk.BooleanVar(value=val)

        self.search_var       = tk.StringVar()
        self.folder_var       = tk.StringVar(value='')
        self.tag_var          = tk.StringVar(value='')
        self.sort_var         = tk.StringVar(value='이름')
        self.short_filter_var = tk.BooleanVar(value=False)
        self.thumb_step_var   = tk.IntVar(value=DEFAULT_THUMB_STEP)
        self.debug_var        = tk.BooleanVar(value=False)
        self.min_dur_var      = tk.IntVar(value=0)
        self.debug_var.trace_add('write',
            lambda *_: self.grid_widget.set_debug(self.debug_var.get())
            if hasattr(self,'grid_widget') else None)

        # 프리페치
        self._prefetch_stop   = threading.Event()
        self._prefetch_thread = None
        self._daily_pick_mode = False

        # 썸네일 큐/스레드/팝업
        self._thumb_queue  = []
        self._thumb_thread = None
        self._thumb_popup  = None
        self._thumb_pb     = None
        self._thumb_lbl_p  = None
        self._thumb_lbl_n  = None

        self.search_var.trace_add('write', lambda *_: self.after(350, self._reload))
        for v in (self.sort_var, self.short_filter_var):
            v.trace_add('write', lambda *_: self.after(50, self._reload))

        self._build_ui()
        self._check_ffmpeg()

        # 썸네일 존재 세트 백그라운드 빌드 (존재 확인을 세트 조회로 대체)
        threading.Thread(target=build_thumb_cache, daemon=True).start()

        self._reload_sidebar()
        self._reload()

    # ── STYLE ───────────────────────────────────
    def _style(self):
        s=ttk.Style(self); s.theme_use('clam')
        bg,fg,ac='#0d0d14','#dcdcf0','#7c6ff7'
        s.configure('.',background=bg,foreground=fg,font=('Consolas',10))
        s.configure('TFrame',background=bg)
        s.configure('TLabel',background=bg,foreground=fg)
        s.configure('TButton',background='#1a1a28',foreground=fg,
                    borderwidth=0,focusthickness=0,padding=6)
        s.map('TButton',background=[('active','#2a2a3d'),('pressed',ac)],
              foreground=[('active','#fff')])
        s.configure('Acc.TButton',background=ac,foreground='#fff')
        s.map('Acc.TButton',background=[('active','#5e52d0')])
        s.configure('TEntry',fieldbackground='#1a1a28',foreground=fg,
                    insertcolor=fg,borderwidth=0)
        s.configure('TScrollbar',background='#1a1a28',troughcolor='#0d0d14',
                    arrowcolor='#444',borderwidth=0)
        s.configure('TProgressbar',background=ac,troughcolor='#1a1a28',borderwidth=0)
        s.configure('TSeparator',background='#2a2a3d')
        s.configure('Treeview',background='#111120',foreground=fg,
                    fieldbackground='#111120',borderwidth=0,rowheight=24)
        s.configure('Treeview.Heading',background='#1a1a28',foreground='#888',borderwidth=0)
        s.map('Treeview',background=[('selected',ac)])

    # ── UI ──────────────────────────────────────
    def _build_ui(self):
        # TOP
        top=tk.Frame(self,bg='#08080f',height=54)
        top.pack(fill='x'); top.pack_propagate(False)
        tk.Label(top,text='VidSort',bg='#08080f',fg='#7c6ff7',
                 font=('Consolas',20,'bold')).pack(side='left',padx=16,pady=8)
        tk.Label(top,text='v5',bg='#08080f',fg='#333',
                 font=('Consolas',10)).pack(side='left')

        sw=tk.Frame(top,bg='#1a1a28',padx=10)
        sw.pack(side='left',padx=20,pady=10,fill='y')
        tk.Label(sw,text='🔍',bg='#1a1a28',fg='#555').pack(side='left')
        se=ttk.Entry(sw,textvariable=self.search_var,width=42,font=('Consolas',11))
        se.pack(side='left',ipady=3)
        se.bind('<Escape>',lambda e: self.search_var.set(''))
        tk.Label(sw,text='파일명·별칭·태그',bg='#1a1a28',
                 fg='#444',font=('Consolas',8)).pack(side='left',padx=8)

        self.lbl_ff=tk.Label(top,text='',bg='#08080f',font=('Consolas',9))
        self.lbl_ff.pack(side='right',padx=10)
        self.lbl_stats=tk.Label(top,text='',bg='#08080f',fg='#555',font=('Consolas',9))
        self.lbl_stats.pack(side='right',padx=10)

        # TOOLBAR
        tb=tk.Frame(self,bg='#111120',pady=6); tb.pack(fill='x')
        ttk.Button(tb,text='📁 폴더 추가',style='Acc.TButton',
                   command=self._add_folder).pack(side='left',padx=(10,4))
        ttk.Button(tb,text='🔄 업데이트',command=self._ask_update).pack(side='left',padx=4)
        ttk.Separator(tb,orient='vertical').pack(side='left',fill='y',padx=8,pady=4)

        tk.Label(tb,text='태그:',bg='#111120',fg='#666',font=('Consolas',9)).pack(side='left',padx=(4,0))
        self.tag_cb=ttk.Combobox(tb,textvariable=self.tag_var,
                                  values=[''],width=14,state='readonly',font=('Consolas',9))
        self.tag_cb.pack(side='left',padx=4)
        self.tag_cb.bind('<<ComboboxSelected>>',lambda e: self._reload())

        tk.Label(tb,text='정렬:',bg='#111120',fg='#666',font=('Consolas',9)).pack(side='left',padx=(8,0))
        ttk.Combobox(tb,textvariable=self.sort_var,
                     values=['이름','크기','날짜추가','재생시간'],
                     width=10,state='readonly',font=('Consolas',9)).pack(side='left',padx=4)

        tk.Checkbutton(tb,text='2글자↓ 무시',variable=self.short_filter_var,
                       bg='#111120',fg='#888',selectcolor='#111120',
                       activebackground='#111120',font=('Consolas',9),
                       cursor='hand2').pack(side='left',padx=8)

        # 우측
        self.lbl_clip=tk.Label(tb,text='',bg='#111120',fg='#ffd166',font=('Consolas',9))
        self.lbl_clip.pack(side='right',padx=8)
        ttk.Button(tb,text='📂 붙여넣기',command=self._paste).pack(side='right',padx=4)
        ttk.Separator(tb,orient='vertical').pack(side='right',fill='y',padx=6,pady=4)

        # 짧은 영상 제외 필터
        self.min_dur_var = tk.IntVar(value=0)
        tk.Label(tb,text='초↓제외:',bg='#111120',fg='#666',
                 font=('Consolas',9)).pack(side='right',padx=(4,0))
        dur_entry = ttk.Entry(tb, textvariable=self.min_dur_var,
                              width=5, font=('Consolas',9))
        dur_entry.pack(side='right', padx=2)
        dur_entry.bind('<Return>', lambda e: self._reload())
        ttk.Button(tb, text='30초',
                   command=lambda:(self.min_dur_var.set(30), self._reload())
                   ).pack(side='right', padx=2)
        ttk.Button(tb, text='60초',
                   command=lambda:(self.min_dur_var.set(60), self._reload())
                   ).pack(side='right', padx=2)
        ttk.Button(tb, text='끄기',
                   command=lambda:(self.min_dur_var.set(0), self._reload())
                   ).pack(side='right', padx=2)
        ttk.Separator(tb,orient='vertical').pack(side='right',fill='y',padx=6,pady=4)

        tk.Checkbutton(tb,text='🐛 디버그',variable=self.debug_var,
                       bg='#111120',fg='#888',selectcolor='#111120',
                       activebackground='#111120',font=('Consolas',9),
                       cursor='hand2').pack(side='right',padx=4)
        ttk.Separator(tb,orient='vertical').pack(side='right',fill='y',padx=6,pady=4)
        tk.Label(tb,text='🔲',bg='#111120',fg='#888').pack(side='right')
        ttk.Scale(tb,from_=0,to=len(THUMB_SIZES)-1,variable=self.thumb_step_var,
                  orient='horizontal',length=80,
                  command=lambda v: self.after(100,self._rerender)).pack(side='right',padx=2)
        tk.Label(tb,text='🖼',bg='#111120',fg='#888').pack(side='right')

        # MAIN
        main=tk.Frame(self,bg='#0d0d14'); main.pack(fill='both',expand=True)

        # 사이드바
        self.sidebar=tk.Frame(main,bg='#0a0a12',width=220)
        self.sidebar.pack(side='left',fill='y'); self.sidebar.pack_propagate(False)
        self._build_sidebar()

        # 오른쪽
        self._right=tk.Frame(main,bg='#0d0d14')
        self._right.pack(side='left',fill='both',expand=True)
        self.grid_widget=CanvasGrid(self._right,
                                    on_open=self._open,
                                    on_ctx=self._ctx,
                                    on_click=self._click)
        self.grid_widget.pack(fill='both',expand=True)

        # STATUS
        sb=tk.Frame(self,bg='#08080f',height=26)
        sb.pack(fill='x',side='bottom'); sb.pack_propagate(False)
        self.progress=ttk.Progressbar(sb,length=200,mode='determinate')
        self.progress.pack(side='right',padx=10,pady=3)
        self.lbl_status=tk.Label(sb,text='준비',bg='#08080f',fg='#444',font=('Consolas',9))
        self.lbl_status.pack(side='left',padx=10)

    def _build_sidebar(self):
        # 폴더 섹션
        tk.Label(self.sidebar,text='📁  폴더',bg='#0a0a12',fg='#555',
                 font=('Consolas',9,'bold')).pack(anchor='w',padx=10,pady=(12,4))
        self.fl=tk.Listbox(self.sidebar,bg='#0a0a12',fg='#999',
                           selectbackground='#7c6ff7',font=('Consolas',9),
                           borderwidth=0,highlightthickness=0,activestyle='none')
        self.fl.pack(fill='both',expand=True,padx=4)
        self.fl.bind('<<ListboxSelect>>',self._sb_folder)

        bf=tk.Frame(self.sidebar,bg='#0a0a12'); bf.pack(fill='x',padx=6,pady=4)
        ttk.Button(bf,text='➕',command=self._add_folder).pack(side='left',fill='x',expand=True,padx=2)
        ttk.Button(bf,text='✕',command=self._remove_folder).pack(side='left',fill='x',expand=True,padx=2)
        ttk.Button(bf,text='📊',command=self._show_folder_overview).pack(side='left',padx=2)

        # 전체 보기 버튼
        ttk.Button(self.sidebar,text='전체 보기',
                   command=self._show_all).pack(fill='x',padx=6,pady=(0,4))
        ttk.Button(self.sidebar,text='🎲 오늘의 추천',
                   command=self._show_daily_pick).pack(fill='x',padx=6,pady=(0,6))

        ttk.Separator(self.sidebar).pack(fill='x',padx=8,pady=4)

        # 태그 버튼 패널
        tk.Label(self.sidebar,text='🏷  태그',bg='#0a0a12',fg='#555',
                 font=('Consolas',9,'bold')).pack(anchor='w',padx=10,pady=(4,4))

        # 스크롤 가능한 태그 버튼 영역
        tag_outer = tk.Frame(self.sidebar,bg='#0a0a12')
        tag_outer.pack(fill='both',expand=True,padx=4)
        tag_canvas = tk.Canvas(tag_outer,bg='#0a0a12',highlightthickness=0)
        tag_vsb    = ttk.Scrollbar(tag_outer,orient='vertical',command=tag_canvas.yview)
        tag_canvas.configure(yscrollcommand=tag_vsb.set)
        tag_vsb.pack(side='right',fill='y')
        tag_canvas.pack(fill='both',expand=True)
        self._tag_btn_frame = tk.Frame(tag_canvas,bg='#0a0a12')
        tag_canvas.create_window((0,0),window=self._tag_btn_frame,anchor='nw')
        self._tag_btn_frame.bind('<Configure>',
            lambda e: tag_canvas.configure(scrollregion=tag_canvas.bbox('all')))
        tag_canvas.bind('<MouseWheel>',
            lambda e: tag_canvas.yview_scroll(-1*(e.delta//120),'units'))

        ttk.Separator(self.sidebar).pack(fill='x',padx=8,pady=6)

        # ── 포맷 필터 섹션 ──────────────────────
        tk.Label(self.sidebar,text='🎬  포맷 필터',bg='#0a0a12',fg='#555',
                 font=('Consolas',9,'bold')).pack(anchor='w',padx=10,pady=(0,4))

        fmt_frame=tk.Frame(self.sidebar,bg='#0a0a12')
        fmt_frame.pack(fill='x',padx=6)

        # 2열 배치
        for i,(label,ext) in enumerate(FORMAT_LIST):
            row=i//2; col=i%2
            cb=tk.Checkbutton(fmt_frame,text=label,variable=self._fmt_vars[ext],
                              bg='#0a0a12',fg='#999',selectcolor='#0a0a12',
                              activebackground='#0a0a12',activeforeground='#dcdcf0',
                              font=('Consolas',8),cursor='hand2')
            cb.grid(row=row,column=col,sticky='w',padx=4,pady=1)

        # 전체선택/해제
        bf2=tk.Frame(self.sidebar,bg='#0a0a12'); bf2.pack(fill='x',padx=6,pady=(4,2))
        ttk.Button(bf2,text='전체 ON',
                   command=lambda:[v.set(True) for v in self._fmt_vars.values()]
                   ).pack(side='left',fill='x',expand=True,padx=2)
        ttk.Button(bf2,text='전체 OFF',
                   command=lambda:[v.set(False) for v in self._fmt_vars.values()]
                   ).pack(side='left',fill='x',expand=True,padx=2)

        # 적용 버튼
        ttk.Button(self.sidebar,text='✅  포맷 적용',style='Acc.TButton',
                   command=self._apply_format).pack(fill='x',padx=6,pady=(4,8))

        ttk.Separator(self.sidebar).pack(fill='x',padx=8,pady=6)

        # ── AI 태그 섹션 ──────────────────────────
        tk.Label(self.sidebar,text='🤖  AI 태그',bg='#0a0a12',fg='#555',
                 font=('Consolas',9,'bold')).pack(anchor='w',padx=10,pady=(0,4))
        ai_f=tk.Frame(self.sidebar,bg='#0a0a12')
        ai_f.pack(fill='x',padx=6,pady=(0,4))
        ttk.Button(ai_f,text='⚙ 설정 / 토큰',
                   command=self._llm_settings_dlg).pack(fill='x',pady=1)
        ttk.Button(ai_f,text='🔌 연결 테스트',
                   command=self._llm_test_dlg).pack(fill='x',pady=1)
        ttk.Button(ai_f,text='🏷 태그 풀 관리',
                   command=self._llm_tag_pool_dlg).pack(fill='x',pady=1)
        ttk.Button(ai_f,text='▶ AI 자동 태그',style='Acc.TButton',
                   command=self._llm_auto_tag_dlg).pack(fill='x',pady=(4,1))
        ttk.Button(ai_f,text='🔍 패턴 분석 → 태그',
                   command=self._llm_pattern_dlg).pack(fill='x',pady=1)

    # ── SIDEBAR 이벤트 ──────────────────────────
    def _reload_sidebar(self):
        folders=self.db.all_folders()
        self._folder_paths=folders
        self.fl.delete(0,'end')
        for f in folders:
            self.fl.insert('end','  '+(Path(f).name or f))

        # 태그 버튼 패널 갱신
        for w in self._tag_btn_frame.winfo_children():
            w.destroy()
        tags = self.db.all_tags()
        self.tag_cb['values'] = ['']+tags

        # 전체 버튼
        all_btn = tk.Button(
            self._tag_btn_frame, text='전체',
            bg='#1a1a28', fg='#aaa', font=('Consolas',9),
            bd=0, padx=10, pady=5, cursor='hand2', anchor='w',
            command=lambda:(self.tag_var.set(''), self._show_all()))
        all_btn.pack(fill='x', pady=1)

        for tag in tags:
            t = tag
            btn = tk.Button(
                self._tag_btn_frame, text=f'  {t}',
                bg='#1a1a28', fg='#dcdcf0', font=('Consolas',9),
                bd=0, padx=10, pady=5, cursor='hand2', anchor='w',
                activebackground='#7c6ff7', activeforeground='#fff',
                command=lambda tg=t: self._filter_by_tag(tg))
            btn.pack(fill='x', pady=1)
            btn.bind('<Enter>', lambda e,b=btn: b.config(bg='#2a2a3d'))
            btn.bind('<Leave>', lambda e,b=btn: b.config(bg='#1a1a28'))

    def _sb_folder(self,e):
        sel=self.fl.curselection()
        if not sel: return
        i=sel[0]
        if i<len(self._folder_paths):
            # 폴더명이 아닌 전체 경로로 필터 (동일 폴더명 중복 문제 해결)
            self.folder_var.set(self._folder_paths[i])
            self.tag_var.set('')
            self._offset=0
            self._reload()

    def _filter_by_tag(self, tag):
        self.tag_var.set(tag)
        self.folder_var.set('')
        self.fl.selection_clear(0,'end')
        self._offset=0
        self._reload()

    def _show_all(self):
        self.folder_var.set('')
        self.tag_var.set('')
        self.fl.selection_clear(0,'end')
        self._offset=0
        self._reload()

    def _show_daily_pick(self):
        """태그/별칭 없는 파일 중 랜덤 100개 — 오늘의 추천"""
        self._set_status('오늘의 추천 선정 중...')
        min_dur = self.min_dur_var.get()
        rows = self.db.random_untagged(self._active_exts(),
                                       limit=100, min_dur=min_dur)
        if not rows:
            messagebox.showinfo('오늘의 추천','조건에 맞는 파일이 없습니다!'); return
        paths    = [v['path'] for v in rows]
        tags_map = self.db.get_tags_for_paths(paths)
        self._videos        = rows
        self._total         = len(rows)
        self._tags_map      = tags_map
        self._offset        = 0
        self._sel.clear()
        self._daily_pick_mode = True   # 플래그 ON
        self.fl.selection_clear(0,'end')
        self.lbl_stats.config(text=f'🎲 오늘의 추천  {len(rows)}개')
        self._set_status(f'오늘의 추천 — 태그/별칭 없는 파일 중 랜덤 {len(rows)}개')
        self._update_nav()
        step=int(self.thumb_step_var.get())
        tw,th=THUMB_SIZES[step]
        self.grid_widget.load(rows, tags_map, self._sel,
                              tw=tw, th=th, img_cache=self._img_cache,
                              debug=self.debug_var.get(),
                              page_offset=0)

    # ── 포맷 적용 ───────────────────────────────
    def _apply_format(self):
        # 설정 저장
        cfg = load_cfg()
        cfg['formats'] = {ext: var.get() for ext,var in self._fmt_vars.items()}
        save_cfg(cfg)
        self._offset=0
        self._reload()

    def _active_exts(self):
        return sorted(ext for ext,var in self._fmt_vars.items() if var.get())

    # ── 핵심: SQL 쿼리로 데이터 가져오기 ─────────
    def _reload(self):
        active_exts = self._active_exts()
        folder  = self.folder_var.get()
        tag     = self.tag_var.get()
        sort    = self.sort_var.get()
        short   = self.short_filter_var.get()
        search  = self.search_var.get().strip()
        min_dur = self.min_dur_var.get()

        threading.Thread(
            target=self._bg_query,
            args=(active_exts, folder, tag, sort, short, search,
                  self._offset, min_dur),
            daemon=True).start()

    def _bg_query(self, active_exts, folder, tag, sort, short, search,
                  offset, min_dur=0):
        rows, total = self.db.query_page(
            active_exts, folder or None, tag or None,
            sort, short, search or None,
            offset, PAGE_SIZE, min_dur=min_dur)
        paths    = [v['path'] for v in rows]
        tags_map = self.db.get_tags_for_paths(paths)
        self.after(0, lambda: self._on_query_done(rows, total, tags_map))

    def _on_query_done(self, rows, total, tags_map):
        self._videos   = rows
        self._total    = total
        self._tags_map = tags_map
        self._sel.clear()

        self.lbl_stats.config(text=f'표시: {len(rows)}개  전체: {total}개')
        self._set_status(f'{total}개 중 {self._offset+1}~{min(self._offset+PAGE_SIZE,total)}번째')

        self._update_nav()
        step=int(self.thumb_step_var.get())
        tw,th=THUMB_SIZES[step]
        self.grid_widget.load(rows, tags_map, self._sel,
                              tw=tw, th=th, img_cache=self._img_cache,
                              debug=self.debug_var.get(),
                              page_offset=self._offset)

        # 다음 페이지 프리페치 시작
        next_offset = self._offset + PAGE_SIZE
        if next_offset < total:
            self._start_prefetch(next_offset, tw, th)

    def _update_nav(self):
        if self._nav_bar and self._nav_bar.winfo_exists():
            self._nav_bar.destroy()

        nav=tk.Frame(self._right,bg='#0d0d14')
        nav.pack(fill='x',padx=10,pady=(4,0),before=self.grid_widget)
        self._nav_bar=nav

        total=self._total; start=self._offset
        end=min(start+PAGE_SIZE,total)

        tk.Label(nav,text=f'전체 {total}개 중  {start+1 if total else 0}~{end}번째',
                 bg='#0d0d14',fg='#555',font=('Consolas',9)).pack(side='left')

        if start>0:
            ttk.Button(nav,text='◀ 이전',
                       command=lambda:(
                           setattr(self,'_offset',max(0,start-PAGE_SIZE)),
                           self._reload())).pack(side='left',padx=6)
        if end<total:
            ttk.Button(nav,text='다음 ▶',
                       command=lambda:(
                           setattr(self,'_offset',end),
                           self._reload())).pack(side='left',padx=4)
        ttk.Button(nav,text='🔃 새로고침',
                   command=self._hard_refresh).pack(side='left',padx=8)

    def _rerender(self):
        step=int(self.thumb_step_var.get())
        tw,th=THUMB_SIZES[step]
        self.grid_widget.load(self._videos, self._tags_map, self._sel,
                              tw=tw, th=th, img_cache=self._img_cache,
                              debug=self.debug_var.get(),
                              page_offset=self._offset)

    def _hard_refresh(self):
        """강제 새로고침 — 메인 스레드에서 DB 직접 쿼리 후 동기 렌더"""
        self._set_status('강제 새로고침 중...')
        self.update_idletasks()

        active_exts = self._active_exts()
        folder  = self.folder_var.get()
        tag     = self.tag_var.get()
        sort    = self.sort_var.get()
        short   = self.short_filter_var.get()
        search  = self.search_var.get().strip()

        # 메인 스레드에서 직접 쿼리 (스레드 타이밍 문제 없음)
        min_dur = self.min_dur_var.get()
        rows, total = self.db.query_page(
            active_exts, folder or None, tag or None,
            sort, short, search or None,
            self._offset, PAGE_SIZE, min_dur=min_dur)
        paths    = [v['path'] for v in rows]
        tags_map = self.db.get_tags_for_paths(paths)

        self._videos   = rows
        self._total    = total
        self._tags_map = tags_map
        self._sel.clear()

        self.lbl_stats.config(text=f'표시: {len(rows)}개  전체: {total}개')
        self._set_status(f'새로고침 완료 — {total}개 중 {len(rows)}개')

        self._update_nav()
        step = int(self.thumb_step_var.get())
        tw, th = THUMB_SIZES[step]

        # 캔버스 완전 초기화 후 동기 렌더
        self.grid_widget.hard_load(rows, tags_map, self._sel,
                                   tw=tw, th=th, img_cache=self._img_cache,
                                   debug=self.debug_var.get(),
                                   page_offset=self._offset)

    def _start_prefetch(self, next_offset, tw, th):
        """다음 페이지 썸네일을 백그라운드에서 미리 img_cache에 적재"""
        self._prefetch_stop.set()
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=0.5)
        self._prefetch_stop.clear()

        active_exts = self._active_exts()
        folder  = self.folder_var.get()
        tag     = self.tag_var.get()
        sort    = self.sort_var.get()
        short   = self.short_filter_var.get()
        search  = self.search_var.get().strip()

        self._prefetch_thread = threading.Thread(
            target=self._prefetch_worker,
            args=(active_exts, folder, tag, sort, short, search,
                  next_offset, tw, th),
            daemon=True)
        self._prefetch_thread.start()

    def _prefetch_worker(self, active_exts, folder, tag, sort, short,
                         search, offset, tw, th):
        """다음 페이지 데이터 쿼리 후 썸네일 이미지 미리 로드"""
        try:
            rows, _ = self.db.query_page(
                active_exts, folder or None, tag or None,
                sort, short, search or None, offset, PAGE_SIZE)
        except: return

        for v in rows:
            if self._prefetch_stop.is_set(): return
            path      = v['path']
            cache_key = f'{path}_{tw}_{th}'
            if cache_key in self._img_cache: continue
            if not thumb_cached(path): continue
            try:
                img = Image.open(thumb_file(path))
                iw,ih = img.size
                scale = min(tw/iw, th/ih) if iw and ih else 1
                nw,nh = max(1,int(iw*scale)), max(1,int(ih*scale))
                img = img.resize((nw,nh), Image.LANCZOS)
                ph  = ImageTk.PhotoImage(img)
                self._img_cache[cache_key] = ph
            except: pass

    # ── SCAN ────────────────────────────────────
    def _add_folder(self):
        self.lift(); self.focus_force()

        win = tk.Toplevel(self)
        win.title('폴더 추가')
        win.configure(bg='#0d0d14')
        win.geometry('520x165')
        win.resizable(False, False)
        win.grab_set(); win.lift()

        tk.Label(win, text='폴더 경로 입력 또는 브라우저로 선택',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 10, 'bold')).pack(pady=(14,4))
        tk.Label(win, text=r'NAS 예: \\192.168.0.110\share\영상  또는  Z:\영상',
                 bg='#0d0d14', fg='#555', font=('Consolas', 8)).pack()

        path_var = tk.StringVar()
        entry = ttk.Entry(win, textvariable=path_var,
                          font=('Consolas', 10), width=54)
        entry.pack(padx=16, pady=8)
        entry.focus_set()

        def browse():
            f = filedialog.askdirectory(title='폴더 선택', parent=win)
            if f: path_var.set(f)   # 변환 없이 그대로

        def confirm():
            folder = path_var.get().strip()   # 변환 없이 그대로
            if not folder:
                messagebox.showinfo('알림', '경로를 입력하세요.', parent=win)
                return
            win.destroy()
            exists = self.db.exists_folder(folder)
            if exists:
                if not messagebox.askyesno('이미 추가됨',
                    '이 폴더는 이미 기록이 있습니다.\n새로 스캔할까요?',
                    parent=self): return
            self._scan(folder, False)

        entry.bind('<Return>', lambda e: confirm())
        bf = tk.Frame(win, bg='#0d0d14'); bf.pack(pady=4)
        ttk.Button(bf, text='📁 브라우저', command=browse).pack(side='left', padx=4)
        ttk.Button(bf, text='✅ 추가', style='Acc.TButton',
                   command=confirm).pack(side='left', padx=4)
        ttk.Button(bf, text='취소', command=win.destroy).pack(side='left', padx=4)

    def _ask_update(self):
        self.lift(); self.focus_force()
        folder = filedialog.askdirectory(title='업데이트할 폴더', parent=self)
        if not folder: return
        ans = messagebox.askyesnocancel('업데이트',
            '[예]  새 파일만 추가\n[아니오]  전체 재스캔',
            parent=self)
        if ans is None: return
        self._scan(folder, ans)

    def _remove_folder(self):
        sel=self.fl.curselection()
        if not sel: return
        fp=self._folder_paths[sel[0]]
        if not messagebox.askyesno('제거',
            f"'{Path(fp).name}' 폴더 기록 삭제?\n실제 파일은 유지됩니다."): return
        rows=self.db.get_all_for_thumbs(folder=fp)
        for v in rows: self.db.remove(v['path'])
        self._reload_sidebar(); self._reload()

    def _scan(self, folder, incremental):
        # sleep 없이 플래그만 set/clear
        self._scan_stop.set()
        self._scan_stop.clear()
        self.progress['value'] = 0
        self._set_status(f'스캔 중: {folder}')
        threading.Thread(target=self._scan_w,
                         args=(folder, incremental), daemon=True).start()

    def _scan_w(self, folder, incremental):
        print(f"[scan_w] 시작 folder={folder!r} incremental={incremental}", flush=True)
        db_rows  = self.db.get_all_for_thumbs(folder=folder)
        db_paths = {v['path'] for v in db_rows}
        existing = db_paths if incremental else set()
        print(f"[scan_w] DB에 기존 {len(db_paths)}개", flush=True)

        found = set()
        count = 0
        lp_folder = longpath(folder)
        for root,dirs,files in os.walk(lp_folder):
            if self._scan_stop.is_set(): return
            dirs[:]=[d for d in dirs if not d.startswith('.') and d!='__pycache__']
            for fname in files:
                ext=Path(fname).suffix.lower()
                if ext not in VIDEO_EXTS: continue
                fpath = str(Path(root)/fname)
                # longpath 접두어만 제거, 슬래시는 변환 안 함
                clean = fpath.replace('\\\\?\\', '')
                found.add(clean)
                if clean in existing: continue
                try:
                    st=os.stat(fpath)
                    self.db.upsert({
                        'path':clean,'name':fname,'alias':'',
                        'size':st.st_size,'mtime':st.st_mtime,
                        'duration':0,'width':0,'height':0,
                        'thumb_ok':0,'folder':folder,'added_at':time.time()
                    })
                    count+=1
                    if count%200==0:
                        self.after(0,lambda c=count:self._set_status(f'스캔 중... {c}개'))
                except: pass

        deleted = db_paths - found
        for p in deleted:
            self.db.remove(p)

        self.after(0,lambda:(
            self._set_status(f'스캔 완료 — 추가:{count}개  삭제:{len(deleted)}개'),
            self._reload_sidebar(),
            self._reload(),
            self._start_thumbs(folder)
        ))

    # ── THUMBNAILS ──────────────────────────────
    def _start_thumbs(self, folder=None):
        if not FFMPEG: return
        all_v = self.db.get_all_for_thumbs(folder=folder)
        todo  = [v for v in all_v
                 if not v.get('thumb_ok') or not thumb_file(v['path']).exists()]
        if not todo: return

        # 이미 실행 중이면 큐에 추가
        if hasattr(self,'_thumb_thread') and self._thumb_thread and \
           self._thumb_thread.is_alive():
            self._thumb_queue.extend(todo)
            self._set_status(
                f'썸네일 대기 중 — 추가 {len(todo)}개 (전체 큐: {len(self._thumb_queue)}개)')
            return

        # 새로 시작
        self._thumb_queue = list(todo)
        self._thumb_stop.clear()
        w = self._nas_w(folder or (todo[0]['folder'] if todo else ''))

        # 팝업
        if hasattr(self,'_thumb_popup') and self._thumb_popup:
            try: self._thumb_popup.destroy()
            except: pass

        popup = tk.Toplevel(self)
        popup.title('썸네일 생성 중')
        popup.configure(bg='#0d0d14')
        popup.geometry('460x150')
        popup.resizable(False,False)
        popup.attributes('-topmost',True)
        self._thumb_popup = popup

        tk.Label(popup,text='🖼  썸네일 생성 중',bg='#0d0d14',fg='#dcdcf0',
                 font=('Consolas',11,'bold')).pack(pady=(14,4))
        self._thumb_lbl_p = tk.Label(popup,text=f'0 / {len(todo)}',
                                     bg='#0d0d14',fg='#7c6ff7',font=('Consolas',10))
        self._thumb_lbl_p.pack()
        self._thumb_pb = ttk.Progressbar(popup,length=400,mode='determinate',
                                         maximum=max(len(todo),1))
        self._thumb_pb.pack(pady=6,padx=24)
        self._thumb_lbl_n = tk.Label(popup,text='',bg='#0d0d14',
                                     fg='#555',font=('Consolas',8))
        self._thumb_lbl_n.pack()
        ttk.Button(popup,text='백그라운드로',command=popup.withdraw).pack(pady=4)

        self._set_status(f'썸네일 생성 — {len(todo)}개')
        self._thumb_thread = threading.Thread(
            target=self._thumb_w, args=(w,), daemon=True)
        self._thumb_thread.start()

    def _nas_w(self,path):
        if sys.platform=='win32':
            drive=str(Path(path).drive)+'\\'
            try:
                import ctypes
                return 2 if ctypes.windll.kernel32.GetDriveTypeW(drive)==4 else 4
            except: pass
        return 4

    def _thumb_w(self, workers):
        """큐 기반 썸네일 워커 — 큐가 빌 때까지 계속 처리"""
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while self._thumb_queue:
                # 현재 큐 스냅샷
                batch = list(self._thumb_queue)
                self._thumb_queue.clear()
                total = len(batch); done = 0

                # 팝업 최대값 업데이트
                def _upd_max(t=total):
                    try:
                        if self._thumb_pb.winfo_exists():
                            self._thumb_pb['maximum'] = t
                    except: pass
                self.after(0, _upd_max)

                futs = {ex.submit(self._gen, v): v for v in batch}
                for fut in as_completed(futs):
                    if self._thumb_stop.is_set(): break
                    v = futs[fut]; done += 1
                    remaining = total - done

                    def _ui(d=done, t=total, r=remaining, n=v['name'], p=v['path']):
                        pct = int(d/t*100) if t else 100
                        self.progress['value'] = pct
                        self._set_status(f'썸네일 {pct}%  남은:{r}개')
                        try:
                            if self._thumb_pb.winfo_exists():
                                self._thumb_pb['value'] = d
                                self._thumb_lbl_p.config(
                                    text=f'{d} / {t}  (남은 {r}개)')
                                self._thumb_lbl_n.config(text=n[:58])
                        except: pass
                        self.grid_widget.refresh_thumb(p)
                    self.after(0, _ui)

                if self._thumb_stop.is_set(): break

        # 완료
        def _done():
            self.progress['value'] = 100
            self._set_status('썸네일 생성 완료')
            try:
                if self._thumb_popup and self._thumb_popup.winfo_exists():
                    self._thumb_popup.destroy()
                    self._thumb_popup = None
            except: pass
        self.after(0, _done)

    def _gen(self,v):
        src = longpath(v['path'])
        tf  = thumb_file(v['path'])
        ok,w,h,dur=make_thumb(src, str(tf))
        if ok:
            self.db.update_thumb(v['path'],w,h,dur)
            v.update({'thumb_ok':1,'width':w,'height':h,'duration':dur})
            register_thumb(v['path'])   # 존재 세트에 추가

    # ── SELECTION ───────────────────────────────
    def _click(self,e,path):
        ctrl=(e.state&0x0004)!=0; shift=(e.state&0x0001)!=0
        if ctrl:
            self._sel.discard(path) if path in self._sel else self._sel.add(path)
        elif shift: self._sel.add(path)
        else:       self._sel={path}
        self.grid_widget.update_sel(self._sel)

    # ── CONTEXT MENU ────────────────────────────
    def _ctx(self,e,path):
        if path not in self._sel:
            self._sel={path}; self.grid_widget.update_sel(self._sel)
        paths=list(self._sel)
        m=tk.Menu(self,tearoff=0,bg='#1a1a28',fg='#dcdcf0',
                  activebackground='#7c6ff7',activeforeground='#fff',font=('Consolas',9))
        m.add_command(label='▶  재생 (기본 앱)',  command=lambda:self._open(path))
        m.add_command(label='🗂  탐색기에서 열기', command=lambda:self._reveal(path))
        m.add_separator()
        m.add_command(label='✏  별칭 편집',        command=lambda:self._alias_dlg(path))
        m.add_command(label='🏷  태그 편집',        command=lambda:self._tag_dlg(paths))
        m.add_command(label='🤖  AI 자동 태그',    command=lambda:self._llm_auto_tag_paths(paths))
        m.add_separator()
        m.add_command(label='✂  잘라내기',          command=lambda:self._clipop('cut',paths))
        m.add_command(label='📋  복사',             command=lambda:self._clipop('copy',paths))
        m.add_separator()
        m.add_command(label='🗑  DB에서 제거',      command=lambda:self._rm_db(paths))
        m.tk_popup(e.x_root,e.y_root)

    # ── ALIAS ───────────────────────────────────
    def _alias_dlg(self,path):
        v=next((x for x in self._videos if x['path']==path),None)
        if not v: return
        win=tk.Toplevel(self); win.title('별칭 편집')
        win.configure(bg='#0d0d14'); win.geometry('420x155')
        win.resizable(False,False); win.grab_set()
        tk.Label(win,text='파일명:',bg='#0d0d14',fg='#555',font=('Consolas',9)
                 ).pack(anchor='w',padx=16,pady=(14,0))
        tk.Label(win,text=v['name'],bg='#0d0d14',fg='#777',font=('Consolas',9)
                 ).pack(anchor='w',padx=16)
        tk.Label(win,text='별칭:',bg='#0d0d14',fg='#aaa',font=('Consolas',9)
                 ).pack(anchor='w',padx=16,pady=(10,2))
        var=tk.StringVar(value=v.get('alias',''))
        ent=ttk.Entry(win,textvariable=var,font=('Consolas',11),width=36)
        ent.pack(padx=16); ent.focus_set(); ent.select_range(0,'end')
        def save():
            a=var.get().strip(); self.db.set_alias(path,a); v['alias']=a
            win.destroy(); self._reload()
        ent.bind('<Return>',lambda e:save())
        bf=tk.Frame(win,bg='#0d0d14'); bf.pack(pady=8)
        ttk.Button(bf,text='저장',style='Acc.TButton',command=save).pack(side='left',padx=4)
        ttk.Button(bf,text='취소',command=win.destroy).pack(side='left',padx=4)

    # ── TAG EDITOR ──────────────────────────────
    def _tag_dlg(self,paths):
        win=tk.Toplevel(self); win.title('태그 편집')
        win.configure(bg='#0d0d14'); win.geometry('380x420')
        win.resizable(False,False); win.grab_set()
        tk.Label(win,text=f'{len(paths)}개 파일 — 태그 편집',
                 bg='#0d0d14',fg='#dcdcf0',font=('Consolas',11,'bold')).pack(pady=12)
        tk.Label(win,text='현재 태그:',bg='#0d0d14',fg='#666',font=('Consolas',9)
                 ).pack(anchor='w',padx=16)
        cur_f=tk.Frame(win,bg='#0d0d14'); cur_f.pack(fill='x',padx=12,pady=4)

        def refresh():
            for w in cur_f.winfo_children(): w.destroy()
            t_now=self.db.get_tags(paths[0]) if len(paths)==1 else []
            for t in t_now:
                tf2=tk.Frame(cur_f,bg='#7c6ff7'); tf2.pack(side='left',padx=2,pady=2)
                tk.Label(tf2,text=t,bg='#7c6ff7',fg='#fff',
                         font=('Consolas',9),padx=6,pady=2).pack(side='left')
                tk.Button(tf2,text='✕',bg='#7c6ff7',fg='#fff',font=('Consolas',7),
                          bd=0,cursor='hand2',
                          command=lambda tg=t:(
                              [self.db.remove_tag(p,tg) for p in paths],refresh())
                          ).pack(side='left')
        refresh()
        ttk.Separator(win).pack(fill='x',padx=12,pady=6)

        existing=self.db.all_tags()
        if existing:
            tk.Label(win,text='기존 태그:',bg='#0d0d14',fg='#666',font=('Consolas',9)
                     ).pack(anchor='w',padx=16)
            ef=tk.Frame(win,bg='#0d0d14'); ef.pack(fill='x',padx=12,pady=4)
            for t in existing[:16]:
                tk.Button(ef,text=t,bg='#1a1a28',fg='#aaa',font=('Consolas',8),
                          bd=0,padx=8,pady=3,cursor='hand2',
                          command=lambda tg=t:(
                              [self.db.add_tag(p,tg) for p in paths],refresh())
                          ).pack(side='left',padx=2,pady=2)

        ttk.Separator(win).pack(fill='x',padx=12,pady=6)
        tk.Label(win,text='새 태그 입력:',bg='#0d0d14',fg='#666',font=('Consolas',9)
                 ).pack(anchor='w',padx=16)
        nf=tk.Frame(win,bg='#0d0d14'); nf.pack(padx=12,pady=4,fill='x')
        nv=tk.StringVar()
        ne=ttk.Entry(nf,textvariable=nv,font=('Consolas',10),width=22)
        ne.pack(side='left'); ne.focus_set()
        def add_new():
            t=nv.get().strip()
            if not t: return
            [self.db.add_tag(p,t) for p in paths]
            nv.set(''); refresh(); self._reload_sidebar()
        ne.bind('<Return>',lambda e:add_new())
        ttk.Button(nf,text='추가',command=add_new).pack(side='left',padx=6)
        ttk.Button(win,text='완료',style='Acc.TButton',
                   command=lambda:(win.destroy(),self._reload_sidebar(),self._reload())
                   ).pack(pady=10)

    # ── FILE OPS ────────────────────────────────
    def _open(self,path):
        try:
            if sys.platform=='win32':    os.startfile(path)
            elif sys.platform=='darwin': subprocess.Popen(['open',path])
            else:                        subprocess.Popen(['xdg-open',path])
        except Exception as e: messagebox.showerror('오류',str(e))

    def _reveal(self,path):
        try:
            if sys.platform=='win32':
                subprocess.Popen(['explorer','/select,',path],creationflags=_NW)
            elif sys.platform=='darwin': subprocess.Popen(['open','-R',path])
            else: subprocess.Popen(['xdg-open',str(Path(path).parent)])
        except Exception as e: messagebox.showerror('오류',str(e))

    def _clipop(self,mode,paths):
        self._clipboard=[(p,mode) for p in paths]
        icon='✂' if mode=='cut' else '📋'
        self.lbl_clip.config(
            text=f"{icon} {len(paths)}개 {'잘라내기' if mode=='cut' else '복사'} 대기")

    def _paste(self):
        if not self._clipboard:
            messagebox.showinfo('알림','클립보드가 비어 있습니다.'); return
        dest=filedialog.askdirectory(title='붙여넣기 대상 폴더')
        if not dest: return
        dest=Path(dest); errs=[]
        for sp,mode in self._clipboard:
            src=Path(sp); dst=dest/src.name; i=1
            while dst.exists(): dst=dest/f'{src.stem} ({i}){src.suffix}'; i+=1
            try:
                shutil.move(str(src),str(dst)) if mode=='cut' else shutil.copy2(str(src),str(dst))
            except Exception as ex: errs.append(str(ex))
        messagebox.showinfo('완료',
            f"{len(self._clipboard)}개 {'이동' if self._clipboard[0][1]=='cut' else '복사'} 완료"
            +(f'\n오류:\n'+'\n'.join(errs[:3]) if errs else ''))
        if self._clipboard[0][1]=='cut':
            self._clipboard=[]; self.lbl_clip.config(text='')
        self._reload()

    def _rm_db(self,paths):
        if not messagebox.askyesno('DB 제거',
            f'{len(paths)}개를 DB에서 제거합니다.\n실제 파일은 유지됩니다.'): return
        for p in paths: self.db.remove(p)
        self._reload_sidebar(); self._reload()

    # ── 폴더 현황 ───────────────────────────────
    def _show_folder_overview(self):
        win=tk.Toplevel(self); win.title('📊 폴더 현황')
        win.configure(bg='#0d0d14'); win.geometry('720x500')
        cols=('folder','count','size','thumbed','missing')
        tree=ttk.Treeview(win,columns=cols,show='headings')
        for c,w,lbl in [('folder',300,'폴더'),('count',70,'파일수'),
                        ('size',90,'용량'),('thumbed',80,'썸네일'),('missing',80,'미생성')]:
            tree.heading(c,text=lbl); tree.column(c,width=w,
                anchor='center' if c!='folder' else 'w')
        vsb=ttk.Scrollbar(win,orient='vertical',command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right',fill='y'); tree.pack(fill='both',expand=True,padx=8,pady=8)
        tree.bind('<Double-Button-1>',lambda e:(
            tree.selection() and (
                setattr(self,'_offset',0),
                self.folder_var.set(tree.item(tree.selection()[0])['values'][0]),
                self._reload(),
                win.destroy())))

        stats=self.db.folder_stats()
        tc=ts=tt=tm=0
        for r in stats:
            miss=r['count']-r['thumbed']
            tree.insert('','end',values=(
                r['folder'],r['count'],fmt_size(r['size'] or 0),
                f"{r['thumbed']}/{r['count']}",miss))
            tc+=r['count']; ts+=r['size'] or 0
            tt+=r['thumbed']; tm+=miss
        tree.insert('','end',values=(
            f"[전체 {len(stats)}개 폴더]",tc,fmt_size(ts),
            f'{tt}/{tc}',tm),tags=('total',))
        tree.tag_configure('total',foreground='#7c6ff7')
        if tm>0:
            ttk.Button(win,text=f'🖼 미생성 {tm}개 썸네일 생성',
                       style='Acc.TButton',
                       command=lambda:(win.destroy(),self._start_thumbs())
                       ).pack(pady=(8,2))

        # 썸네일 초기화 버튼
        bf=tk.Frame(win,bg='#0d0d14'); bf.pack(pady=4)
        ttk.Button(bf,text='🗑 선택 폴더 썸네일만 초기화',
                   command=lambda:(win.destroy(), self._reset_thumbs(
                       tree.item(tree.selection()[0])['values'][0]
                       if tree.selection() else None))
                   ).pack(side='left',padx=4)
        ttk.Button(bf,text='🗑 전체 썸네일 초기화',
                   command=lambda:(win.destroy(), self._reset_thumbs(None))
                   ).pack(side='left',padx=4)

    def _reset_thumbs(self, folder=None):
        """썸네일 파일 삭제 + DB thumb_ok 리셋"""
        msg = f"'{Path(folder).name}' 폴더" if folder else "전체"
        if not messagebox.askyesno('썸네일 초기화',
            f"{msg} 썸네일을 모두 삭제하고 다시 생성합니다.\n계속할까요?"):
            return

        # DB thumb_ok 리셋
        with self.db.lock:
            if folder:
                self.db.conn.execute(
                    "UPDATE files SET thumb_ok=0 WHERE folder=?", (folder,))
            else:
                self.db.conn.execute("UPDATE files SET thumb_ok=0")
            self.db.conn.commit()

        # 썸네일 파일 삭제
        deleted = 0
        if folder:
            # 해당 폴더 파일들의 썸네일만 삭제
            rows = self.db.get_all_for_thumbs(folder=folder)
            for v in rows:
                tf = thumb_file(v['path'])
                if tf.exists():
                    tf.unlink()
                    deleted += 1
        else:
            # 전체 .thumbs 폴더 삭제 후 재생성
            if THUMB_DIR.exists():
                for f in THUMB_DIR.iterdir():
                    if f.suffix == '.jpg':
                        f.unlink()
                        deleted += 1

        # 메모리 캐시도 초기화
        self._img_cache.clear()
        build_thumb_cache()  # 존재 세트 재빌드

        messagebox.showinfo('완료',
            f'썸네일 {deleted}개 삭제 완료\n다시 썸네일을 생성하려면 📊 → 미생성 썸네일 생성을 누르세요.')
        self._reload()
    def _auto_group_dialog(self):
        self._set_status('키워드 분석 중...')
        threading.Thread(target=self._run_auto_group,daemon=True).start()

    def _run_auto_group(self):
        # DB에서 전체 파일명만 가져옴
        rows=self.db.conn.execute("SELECT path,name FROM files").fetchall()
        freq=defaultdict(set)
        for path,name in rows:
            stem=Path(name).stem
            seen=set()
            for ln in range(3,min(len(stem)+1,20)):
                for st2 in range(len(stem)-ln+1):
                    kw=stem[st2:st2+ln]
                    if kw not in seen:
                        seen.add(kw); freq[kw].add(path)
        candidates=sorted(
            [(kw,paths) for kw,paths in freq.items() if len(paths)>=2],
            key=lambda x:(-len(x[1]),-len(x[0])))[:200]
        self.after(0,lambda:(
            self._set_status('분석 완료'),
            self._show_auto_group(candidates)))

    def _show_auto_group(self,candidates):
        win=tk.Toplevel(self); win.title('🔖 자동 그룹 태그')
        win.configure(bg='#0d0d14'); win.geometry('560x660'); win.grab_set()
        tk.Label(win,text='공통 키워드 → 자동 태그',bg='#0d0d14',fg='#dcdcf0',
                 font=('Consolas',12,'bold')).pack(pady=12)
        sf=tk.Frame(win,bg='#0d0d14'); sf.pack(fill='x',padx=16,pady=4)
        tk.Label(sf,text='필터:',bg='#0d0d14',fg='#666',font=('Consolas',9)).pack(side='left')
        fvar=tk.StringVar()
        ttk.Entry(sf,textvariable=fvar,width=20,font=('Consolas',9)).pack(side='left',padx=6)

        lf=tk.Frame(win,bg='#0d0d14'); lf.pack(fill='both',expand=True,padx=16)
        lc=tk.Canvas(lf,bg='#0d0d14',highlightthickness=0)
        lsb=ttk.Scrollbar(lf,orient='vertical',command=lc.yview)
        lc.configure(yscrollcommand=lsb.set)
        lsb.pack(side='right',fill='y'); lc.pack(fill='both',expand=True)
        inner=tk.Frame(lc,bg='#0d0d14')
        lc.create_window((0,0),window=inner,anchor='nw')
        inner.bind('<Configure>',lambda e:lc.configure(scrollregion=lc.bbox('all')))
        lc.bind('<MouseWheel>',lambda e:lc.yview_scroll(-1*(e.delta//120),'units'))

        check_vars={}
        def refresh_list(q=''):
            for w in inner.winfo_children(): w.destroy()
            check_vars.clear()
            for kw,paths in candidates:
                if q and q.lower() not in kw.lower(): continue
                var=tk.BooleanVar(value=False); check_vars[kw]=(var,paths)
                row=tk.Frame(inner,bg='#0d0d14'); row.pack(fill='x',pady=1)
                tk.Checkbutton(row,variable=var,bg='#0d0d14',selectcolor='#0d0d14',
                               activebackground='#0d0d14',cursor='hand2').pack(side='left')
                tk.Label(row,text=kw,bg='#0d0d14',fg='#dcdcf0',
                         font=('Consolas',10),width=22,anchor='w').pack(side='left')
                tk.Label(row,text=f'{len(paths)}개',bg='#0d0d14',fg='#7c6ff7',
                         font=('Consolas',9)).pack(side='left',padx=8)
        refresh_list()
        fvar.trace_add('write',lambda *_:refresh_list(fvar.get()))

        bf2=tk.Frame(win,bg='#0d0d14'); bf2.pack(fill='x',padx=16,pady=4)
        ttk.Button(bf2,text='전체 선택',
                   command=lambda:[v.set(True) for v,_ in check_vars.values()]
                   ).pack(side='left',padx=4)
        ttk.Button(bf2,text='전체 해제',
                   command=lambda:[v.set(False) for v,_ in check_vars.values()]
                   ).pack(side='left',padx=4)

        def apply():
            sel=[(kw,paths) for kw,(var,paths) in check_vars.items() if var.get()]
            if not sel: messagebox.showinfo('알림','선택된 키워드가 없습니다.'); return
            total_t=0
            for kw,paths in sel:
                for p in paths:
                    self.db.add_tag(p,kw); total_t+=1
            win.destroy()
            self._reload_sidebar(); self._reload()
            messagebox.showinfo('완료',f'{len(sel)}개 키워드, {total_t}건 태그 적용')
        ttk.Button(win,text='✅ 선택 항목 태그 적용',
                   style='Acc.TButton',command=apply).pack(pady=10)

    # ── LLM / AI 태그 ───────────────────────────
    def _save_llm_cfg(self):
        cfg = load_cfg()
        cfg['llm_token']    = self._llm_token
        cfg['llm_model']    = self._llm_model
        cfg['llm_endpoint'] = self._llm_endpoint
        cfg['llm_tag_pool'] = self._llm_tag_pool
        cfg['llm_prompt']   = self._llm_prompt
        save_cfg(cfg)

    def _get_llm_client(self):
        """LLMClient 인스턴스 반환. 설정/패키지 오류 시 None."""
        if not self._llm_token:
            messagebox.showwarning('AI 설정',
                'GitHub 토큰이 설정되지 않았습니다.\n'
                '사이드바 → ⚙ 설정 / 토큰 에서 입력하세요.')
            return None
        try:
            from llm_api import LLMClient
            return LLMClient(self._llm_token, self._llm_model, self._llm_endpoint)
        except ImportError:
            messagebox.showerror('패키지 없음',
                'openai 패키지가 필요합니다.\n터미널에서:\n  pip install openai')
            return None
        except Exception as e:
            messagebox.showerror('LLM 오류', str(e))
            return None

    # ── AI 설정 다이얼로그 ───────────────────────
    def _llm_settings_dlg(self):
        from llm_api import DEFAULT_SYSTEM_PROMPT
        win = tk.Toplevel(self)
        win.title('🤖 AI 설정 — GitHub Copilot')
        win.configure(bg='#0d0d14')
        win.geometry('560x560')
        win.resizable(False, True)
        win.grab_set()

        tk.Label(win, text='GitHub Copilot API 설정',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 12, 'bold')).pack(pady=(16, 8))

        def _row(label, val, hint='', show=''):
            f = tk.Frame(win, bg='#0d0d14'); f.pack(fill='x', padx=20, pady=3)
            tk.Label(f, text=label, bg='#0d0d14', fg='#888',
                     font=('Consolas', 9), width=14, anchor='e').pack(side='left')
            var = tk.StringVar(value=val)
            ttk.Entry(f, textvariable=var, font=('Consolas', 10),
                      width=32, show=show).pack(side='left', padx=6)
            if hint:
                tk.Label(f, text=hint, bg='#0d0d14', fg='#444',
                         font=('Consolas', 7)).pack(side='left')
            return var

        v_token    = _row('GitHub 토큰:', self._llm_token,    '(PAT)', show='*')
        v_model    = _row('모델:',        self._llm_model,    '예) claude-sonnet-4.5')
        v_endpoint = _row('엔드포인트:',  self._llm_endpoint, '')

        tk.Label(win, text='엔드포인트: https://api.githubcopilot.com',
                 bg='#0d0d14', fg='#3a3a5c', font=('Consolas', 7)).pack()

        ttk.Separator(win).pack(fill='x', padx=16, pady=10)

        # ── 태그 분류 프롬프트 편집 ───────────────
        ph = tk.Frame(win, bg='#0d0d14'); ph.pack(fill='x', padx=20)
        tk.Label(ph, text='태그 분류 프롬프트 (비워두면 기본값 사용):',
                 bg='#0d0d14', fg='#888', font=('Consolas', 9)).pack(side='left')
        ttk.Button(ph, text='기본값으로',
                   command=lambda: (prompt_txt.delete('1.0', 'end'),
                                    prompt_txt.insert('1.0', DEFAULT_SYSTEM_PROMPT))
                   ).pack(side='right')

        pf = tk.Frame(win, bg='#0d0d14')
        pf.pack(fill='both', expand=True, padx=20, pady=(4, 0))
        prompt_txt = tk.Text(pf, bg='#1a1a28', fg='#dcdcf0',
                             insertbackground='#dcdcf0',
                             font=('Consolas', 9), height=10,
                             borderwidth=0, wrap='word')
        psb = ttk.Scrollbar(pf, orient='vertical', command=prompt_txt.yview)
        prompt_txt.configure(yscrollcommand=psb.set)
        psb.pack(side='right', fill='y')
        prompt_txt.pack(fill='both', expand=True)
        prompt_txt.insert('1.0', self._llm_prompt or DEFAULT_SYSTEM_PROMPT)

        def save():
            self._llm_token    = v_token.get().strip()
            self._llm_model    = v_model.get().strip() or 'claude-sonnet-4.5'
            self._llm_endpoint = (v_endpoint.get().strip()
                                  or 'https://api.githubcopilot.com')
            entered = prompt_txt.get('1.0', 'end').strip()
            # 기본값과 동일하면 빈 문자열 저장 (항상 최신 기본값 따라가게)
            self._llm_prompt = '' if entered == DEFAULT_SYSTEM_PROMPT else entered
            self._save_llm_cfg()
            win.destroy()
            messagebox.showinfo('저장', 'AI 설정이 저장되었습니다.')

        bf = tk.Frame(win, bg='#0d0d14'); bf.pack(pady=10)
        ttk.Button(bf, text='💾 저장', style='Acc.TButton',
                   command=save).pack(side='left', padx=4)
        ttk.Button(bf, text='취소', command=win.destroy).pack(side='left', padx=4)

    # ── 연결 테스트 ──────────────────────────────
    def _llm_test_dlg(self):
        client = self._get_llm_client()
        if not client: return

        win = tk.Toplevel(self)
        win.title('🔌 AI 연결 테스트')
        win.configure(bg='#0d0d14')
        win.geometry('440x230')
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text='AI 연결 테스트',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 12, 'bold')).pack(pady=(18, 4))
        tk.Label(win, text=f'모델: {self._llm_model}',
                 bg='#0d0d14', fg='#555', font=('Consolas', 9)).pack()
        tk.Label(win, text='질문: "2+2는 뭔가요?"',
                 bg='#0d0d14', fg='#444', font=('Consolas', 9)).pack(pady=(4, 0))

        lbl_res = tk.Label(win, text='요청 중...', bg='#0d0d14', fg='#7c6ff7',
                           font=('Consolas', 10), wraplength=400)
        lbl_res.pack(pady=10, padx=20)

        pb = ttk.Progressbar(win, mode='indeterminate', length=380)
        pb.pack(pady=4); pb.start(10)

        def run():
            try:
                ans = client.test_connection()
                def _ok():
                    pb.stop(); pb.destroy()
                    lbl_res.config(text=f'✅  {ans}', fg='#4dffb4')
                    ttk.Button(win, text='닫기',
                               command=win.destroy).pack(pady=8)
                win.after(0, _ok)
            except Exception as e:
                def _err():
                    pb.stop(); pb.destroy()
                    lbl_res.config(text=f'❌  오류: {e}', fg='#ff6b6b')
                    ttk.Button(win, text='닫기',
                               command=win.destroy).pack(pady=8)
                win.after(0, _err)

        threading.Thread(target=run, daemon=True).start()

    # ── 태그 풀 관리 ─────────────────────────────
    def _llm_tag_pool_dlg(self):
        win = tk.Toplevel(self)
        win.title('🏷 AI 태그 풀 관리')
        win.configure(bg='#0d0d14')
        win.geometry('400x460')
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text='AI 태그 풀',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 12, 'bold')).pack(pady=(16, 4))
        tk.Label(win, text='LLM이 파일을 분류할 때 이 태그들 중에서만 선택합니다.',
                 bg='#0d0d14', fg='#555', font=('Consolas', 8)).pack()

        # 태그 리스트박스
        lf = tk.Frame(win, bg='#0d0d14')
        lf.pack(fill='both', expand=True, padx=16, pady=8)
        lb = tk.Listbox(lf, bg='#1a1a28', fg='#dcdcf0',
                        selectbackground='#7c6ff7',
                        font=('Consolas', 11), borderwidth=0,
                        highlightthickness=0, activestyle='none')
        vsb = ttk.Scrollbar(lf, orient='vertical', command=lb.yview)
        lb.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        lb.pack(fill='both', expand=True)
        for t in self._llm_tag_pool:
            lb.insert('end', t)

        # 추가/삭제
        af = tk.Frame(win, bg='#0d0d14'); af.pack(fill='x', padx=16, pady=(0, 4))
        add_var = tk.StringVar()
        add_e = ttk.Entry(af, textvariable=add_var,
                          font=('Consolas', 10), width=20)
        add_e.pack(side='left', padx=(0, 6))

        def _add():
            t = add_var.get().strip()
            if t and t not in lb.get(0, 'end'):
                lb.insert('end', t)
            add_var.set(''); add_e.focus_set()

        def _del():
            sel = lb.curselection()
            if sel: lb.delete(sel[0])

        add_e.bind('<Return>', lambda e: _add())
        ttk.Button(af, text='추가', command=_add).pack(side='left')
        ttk.Button(af, text='삭제', command=_del).pack(side='left', padx=4)

        def _defaults():
            lb.delete(0, 'end')
            for t in ['애니', '영화', '드라마', '예능', '다큐', '성인', '기타']:
                lb.insert('end', t)

        ttk.Button(win, text='기본값으로 초기화',
                   command=_defaults).pack(pady=(0, 4))

        def save():
            self._llm_tag_pool = list(lb.get(0, 'end'))
            self._save_llm_cfg()
            win.destroy()
            messagebox.showinfo('저장',
                f'태그 풀 {len(self._llm_tag_pool)}개 저장됨')

        bf = tk.Frame(win, bg='#0d0d14'); bf.pack(pady=8)
        ttk.Button(bf, text='💾 저장', style='Acc.TButton',
                   command=save).pack(side='left', padx=4)
        ttk.Button(bf, text='취소', command=win.destroy).pack(side='left', padx=4)

    # ── AI 자동 태그 다이얼로그 ──────────────────
    def _llm_auto_tag_dlg(self):
        """폴더/현재 필터 대상으로 AI 자동 태그 실행 설정"""
        if not self._llm_tag_pool:
            messagebox.showwarning('AI 태그',
                '태그 풀이 비어 있습니다.\n'
                '사이드바 → 🏷 태그 풀 관리 에서 먼저 설정하세요.')
            return

        folders = self.db.all_folders()

        win = tk.Toplevel(self)
        win.title('▶ AI 자동 태그')
        win.configure(bg='#0d0d14')
        win.geometry('500x400')
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win, text='AI 자동 태그',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 12, 'bold')).pack(pady=(16, 4))

        # 대상 선택
        tk.Label(win, text='처리 대상:',
                 bg='#0d0d14', fg='#888',
                 font=('Consolas', 9)).pack(anchor='w', padx=20)

        scope_var  = tk.StringVar(value='current')
        folder_var = tk.StringVar(value=folders[0] if folders else '')

        rb_f = tk.Frame(win, bg='#0d0d14'); rb_f.pack(fill='x', padx=20, pady=4)

        tk.Radiobutton(rb_f,
                       text=f'현재 화면 파일 ({len(self._videos)}개)',
                       variable=scope_var, value='current',
                       bg='#0d0d14', fg='#dcdcf0', selectcolor='#0d0d14',
                       font=('Consolas', 9), cursor='hand2').pack(anchor='w')

        tk.Radiobutton(rb_f, text='특정 폴더 전체:',
                       variable=scope_var, value='folder',
                       bg='#0d0d14', fg='#dcdcf0', selectcolor='#0d0d14',
                       font=('Consolas', 9), cursor='hand2').pack(anchor='w')

        ttk.Combobox(rb_f, textvariable=folder_var, values=folders,
                     state='readonly', font=('Consolas', 8),
                     width=50).pack(anchor='w', padx=20, pady=2)

        ttk.Separator(win).pack(fill='x', padx=16, pady=8)

        # 태그 풀 미리보기
        tk.Label(win, text=f'태그 풀 ({len(self._llm_tag_pool)}개):',
                 bg='#0d0d14', fg='#888',
                 font=('Consolas', 9)).pack(anchor='w', padx=20)
        tk.Label(win, text=' · '.join(self._llm_tag_pool),
                 bg='#0d0d14', fg='#7c6ff7',
                 font=('Consolas', 9), wraplength=460).pack(padx=20, pady=2)

        ttk.Separator(win).pack(fill='x', padx=16, pady=8)

        # 옵션
        skip_var = tk.BooleanVar(value=True)
        tk.Checkbutton(win, text='이미 태그된 파일은 건너뜀 (권장)',
                       variable=skip_var,
                       bg='#0d0d14', fg='#888', selectcolor='#0d0d14',
                       font=('Consolas', 9)).pack(anchor='w', padx=20)

        def start():
            scope = scope_var.get()
            if scope == 'current':
                paths = [v['path'] for v in self._videos]
            else:
                folder = folder_var.get()
                if not folder:
                    messagebox.showwarning('알림', '폴더를 선택해주세요.',
                                           parent=win)
                    return
                rows  = self.db.get_all_for_thumbs(folder=folder)
                paths = [r['path'] for r in rows]

            if skip_var.get():
                tmap = self.db.get_tags_for_paths(paths)
                paths = [p for p in paths if not tmap.get(p)]

            if not paths:
                messagebox.showinfo('알림', '처리할 파일이 없습니다.\n'
                    '(이미 태그된 파일만 있거나 목록이 비어 있습니다.)',
                    parent=win)
                return

            win.destroy()
            self._llm_run_batch(paths, self._llm_tag_pool)

        bf = tk.Frame(win, bg='#0d0d14'); bf.pack(pady=12)
        ttk.Button(bf, text='▶ 시작', style='Acc.TButton',
                   command=start).pack(side='left', padx=4)
        ttk.Button(bf, text='취소',
                   command=win.destroy).pack(side='left', padx=4)

    def _llm_auto_tag_paths(self, paths):
        """우클릭 → 선택 파일 AI 자동 태그"""
        if not self._llm_tag_pool:
            messagebox.showwarning('AI 태그',
                '태그 풀이 비어 있습니다.\n'
                '사이드바 → 🏷 태그 풀 관리 에서 먼저 설정하세요.')
            return
        self._llm_run_batch(paths, self._llm_tag_pool)

    # ── 패턴 분석 → 태그 지정 ───────────────────
    def _llm_pattern_dlg(self):
        """
        파일명에서 공통 패턴(접두어·괄호어)을 추출해 보여주고,
        각 패턴에 태그를 지정하면 해당 파일 전체에 일괄 적용.

        예) 'PPV'로 시작하는 파일 38개 발견 → 태그: [   ]
        """
        self._set_status('패턴 분석 중...')
        threading.Thread(target=self._run_pattern_analysis, daemon=True).start()

    def _run_pattern_analysis(self):
        import re
        rows = self.db.conn.execute(
            "SELECT path, name FROM files").fetchall()

        # ── 패턴 추출 ─────────────────────────────
        # 1) 대괄호/소괄호 안 단어: [PPV], (HD) 등
        # 2) 언더스코어·하이픈·공백 앞 첫 토큰 (3글자 이상)
        pattern_paths = defaultdict(set)

        bracket_re = re.compile(r'[\[\(]([A-Za-z0-9\-_\.]+)[\]\)]')
        prefix_re  = re.compile(r'^([A-Za-z0-9]{3,})')  # 파일명 맨 앞 영숫자

        for path, name in rows:
            stem = Path(name).stem

            # 괄호 안 키워드
            for m in bracket_re.finditer(stem):
                kw = m.group(1).upper()
                if len(kw) >= 2:
                    pattern_paths[kw].add(path)

            # 앞쪽 접두어 (구분자 전까지)
            clean = re.split(r'[-_\s]', stem)[0]
            if prefix_re.match(clean) and 2 <= len(clean) <= 16:
                pattern_paths[clean.upper()].add(path)

        # 3개 이상 파일에 등장한 패턴만
        MIN_FILES = 3
        candidates = sorted(
            [(kw, paths) for kw, paths in pattern_paths.items()
             if len(paths) >= MIN_FILES],
            key=lambda x: -len(x[1]))[:100]

        self.after(0, lambda: (
            self._set_status('패턴 분석 완료'),
            self._show_pattern_dlg(candidates)))

    def _show_pattern_dlg(self, candidates):
        if not candidates:
            messagebox.showinfo('패턴 분석',
                f'공통 패턴(3개 파일 이상)을 찾지 못했습니다.')
            return

        existing_tags = self.db.all_tags()

        win = tk.Toplevel(self)
        win.title('🔍 패턴 분석 → 태그 일괄 지정')
        win.configure(bg='#0d0d14')
        win.geometry('620x640')
        win.grab_set()

        tk.Label(win, text='파일명 패턴 → 태그 일괄 지정',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 12, 'bold')).pack(pady=(14, 2))
        tk.Label(win,
                 text='패턴별로 태그를 입력하세요. 비워두면 해당 패턴은 건너뜁니다.',
                 bg='#0d0d14', fg='#555', font=('Consolas', 8)).pack(pady=(0, 6))

        # ── 검색 필터 ─────────────────────────────
        sf = tk.Frame(win, bg='#0d0d14'); sf.pack(fill='x', padx=16, pady=(0, 4))
        tk.Label(sf, text='필터:', bg='#0d0d14', fg='#666',
                 font=('Consolas', 9)).pack(side='left')
        fvar = tk.StringVar()
        ttk.Entry(sf, textvariable=fvar, width=18,
                  font=('Consolas', 9)).pack(side='left', padx=6)

        # ── 스크롤 패턴 목록 ──────────────────────
        outer = tk.Frame(win, bg='#0d0d14')
        outer.pack(fill='both', expand=True, padx=14)
        cv = tk.Canvas(outer, bg='#0d0d14', highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient='vertical', command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        cv.pack(fill='both', expand=True)
        inner = tk.Frame(cv, bg='#0d0d14')
        cv.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: cv.configure(scrollregion=cv.bbox('all')))
        cv.bind('<MouseWheel>',
                lambda e: cv.yview_scroll(-1*(e.delta//120), 'units'))

        # 패턴당 (체크변수, 태그변수) 저장
        row_vars = {}  # kw → (BooleanVar, StringVar, paths)

        def build_list(q=''):
            for w in inner.winfo_children():
                w.destroy()
            row_vars.clear()

            # 헤더
            hf = tk.Frame(inner, bg='#1a1a28')
            hf.pack(fill='x', pady=(0, 2))
            for text, w in [('✓', 3), ('패턴', 12), ('파일수', 6), ('지정 태그', 22)]:
                tk.Label(hf, text=text, bg='#1a1a28', fg='#555',
                         font=('Consolas', 8), width=w,
                         anchor='w').pack(side='left', padx=3)

            for kw, paths in candidates:
                if q and q.upper() not in kw:
                    continue
                chk_var = tk.BooleanVar(value=True)
                tag_var = tk.StringVar()

                rf = tk.Frame(inner, bg='#0d0d14')
                rf.pack(fill='x', pady=1)

                tk.Checkbutton(rf, variable=chk_var, bg='#0d0d14',
                               selectcolor='#0d0d14', cursor='hand2',
                               activebackground='#0d0d14').pack(side='left')
                tk.Label(rf, text=kw, bg='#0d0d14', fg='#dcdcf0',
                         font=('Consolas', 10), width=12,
                         anchor='w').pack(side='left', padx=4)
                tk.Label(rf, text=f'{len(paths)}개', bg='#0d0d14',
                         fg='#7c6ff7', font=('Consolas', 9),
                         width=5).pack(side='left')

                tag_e = ttk.Entry(rf, textvariable=tag_var,
                                  font=('Consolas', 9), width=18)
                tag_e.pack(side='left', padx=4)

                # 기존 태그 빠른 선택 버튼 (최대 4개)
                for et in existing_tags[:4]:
                    t = et
                    tk.Button(rf, text=t, bg='#1a1a28', fg='#aaa',
                              font=('Consolas', 7), bd=0, padx=4,
                              cursor='hand2',
                              command=lambda tv=tag_var, tg=t: tv.set(tg)
                              ).pack(side='left', padx=1)

                row_vars[kw] = (chk_var, tag_var, paths)

        build_list()
        fvar.trace_add('write', lambda *_: build_list(fvar.get()))

        # ── 하단 버튼 ─────────────────────────────
        bf = tk.Frame(win, bg='#0d0d14'); bf.pack(fill='x', padx=14, pady=8)

        def apply_all():
            applied_files = 0; applied_tags = 0
            for kw, (chk_var, tag_var, paths) in row_vars.items():
                if not chk_var.get():
                    continue
                tag = tag_var.get().strip()
                if not tag:
                    continue
                for p in paths:
                    self.db.add_tag(p, tag)
                    applied_tags += 1
                applied_files += len(paths)
            win.destroy()
            self._reload_sidebar()
            self._reload()
            messagebox.showinfo('적용 완료',
                f'패턴 태그 적용 완료\n'
                f'파일: {applied_files}개  태그: {applied_tags}건')

        ttk.Button(bf, text='✅ 선택 패턴 태그 적용',
                   style='Acc.TButton',
                   command=apply_all).pack(side='left', padx=4)
        ttk.Button(bf, text='취소',
                   command=win.destroy).pack(side='left', padx=4)
        tk.Label(bf, text=f'총 {len(candidates)}개 패턴 발견',
                 bg='#0d0d14', fg='#444',
                 font=('Consolas', 8)).pack(side='right', padx=8)

    # ── AI 배치 태그 실행 ────────────────────────
    def _llm_run_batch(self, paths, tag_pool):
        """백그라운드 배치 태그 실행 + 진행 팝업"""
        client = self._get_llm_client()
        if not client: return

        filenames = [Path(p).name for p in paths]
        total     = len(paths)

        # 진행 팝업
        popup = tk.Toplevel(self)
        popup.title('🤖 AI 자동 태그 진행 중')
        popup.configure(bg='#0d0d14')
        popup.geometry('460x210')
        popup.resizable(False, False)
        popup.attributes('-topmost', True)

        tk.Label(popup, text='🤖  AI 자동 태그 처리 중',
                 bg='#0d0d14', fg='#dcdcf0',
                 font=('Consolas', 11, 'bold')).pack(pady=(16, 4))
        lbl_p = tk.Label(popup, text=f'0 / {total}',
                         bg='#0d0d14', fg='#7c6ff7',
                         font=('Consolas', 10))
        lbl_p.pack()
        pb = ttk.Progressbar(popup, length=400, mode='determinate',
                             maximum=max(total, 1))
        pb.pack(pady=8, padx=24)
        lbl_n = tk.Label(popup, text='LLM 요청 중...',
                         bg='#0d0d14', fg='#555', font=('Consolas', 8))
        lbl_n.pack()
        ttk.Button(popup, text='백그라운드로',
                   command=popup.withdraw).pack(pady=4)

        self._llm_stop.clear()

        def on_progress(done, tot):
            def _ui():
                pb['value'] = done
                lbl_p.config(text=f'{done} / {tot}')
                lbl_n.config(text=f'배치 처리 중 ({done}/{tot})')
            popup.after(0, _ui)

        def worker():
            try:
                tags_list = client.analyze_and_tag(
                    filenames, tag_pool, on_progress,
                    custom_prompt=self._llm_prompt)
            except Exception as e:
                popup.after(0, lambda: (
                    popup.destroy(),
                    messagebox.showerror('AI 오류', str(e))))
                return

            # 태그 DB 적용
            applied = 0
            for path, tags in zip(paths, tags_list):
                for tag in tags:
                    self.db.add_tag(path, tag)
                    applied += 1

            def done():
                try: popup.destroy()
                except Exception: pass
                self._reload_sidebar()
                self._reload()
                messagebox.showinfo(
                    'AI 태그 완료',
                    f'처리 완료: {len(paths)}개 파일\n'
                    f'적용된 태그: {applied}건\n'
                    f'태그 풀: {" / ".join(tag_pool)}')

            popup.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ── MISC ────────────────────────────────────
    def _check_ffmpeg(self):
        if FFMPEG: self.lbl_ff.config(text='● ffmpeg',fg='#4dffb4')
        else:      self.lbl_ff.config(text='⚠ ffmpeg 없음',fg='#ffd166')

    def _set_status(self,msg):
        self.lbl_status.config(text=msg)

    def on_close(self):
        self._scan_stop.set()
        self._thumb_stop.set()
        self.db.close()
        self.destroy()


if __name__ == '__main__':
    app = VidSort()
    app.protocol('WM_DELETE_WINDOW', app.on_close)
    app.mainloop()
