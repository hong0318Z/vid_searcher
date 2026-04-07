"""
VidSort 웹 갤러리 — 로컬 Flask 서버
브라우저에서 영상 탐색 / HTML5 웹플레이어 / 태그 카테고리
"""

import os, sys, hashlib, threading, webbrowser, sqlite3, mimetypes, time
from pathlib import Path
from flask import (Flask, render_template_string, send_file, jsonify,
                   request, Response, abort, redirect, url_for)

app    = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600

_cfg    = {'db_path': None, 'thumb_dir': None, 'port': 8765}
_pcache = {}   # md5_id -> path
_srv    = None

# ─────────────────────────────────────────────────
#  DB / 경로 헬퍼
# ─────────────────────────────────────────────────
def _conn():
    return sqlite3.connect(str(_cfg['db_path']), check_same_thread=False)

def _build_cache():
    _pcache.clear()
    for (p,) in _conn().execute("SELECT path FROM files").fetchall():
        _pcache[hashlib.md5(p.encode()).hexdigest()] = p

def _get_path(vid_id):
    if vid_id not in _pcache:
        _build_cache()
    return _pcache.get(vid_id)

def _vid_id(path):
    return hashlib.md5(path.encode()).hexdigest()

def _fmt_dur(sec):
    if not sec: return ''
    s = int(sec)
    h = s // 3600
    m = (s % 3600) // 60
    s = s % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def _fmt_size(b):
    if not b: return ''
    for u in ('B','KB','MB','GB'):
        if b < 1024: return f'{b:.0f} {u}'
        b /= 1024
    return f'{b:.1f} TB'

# ─────────────────────────────────────────────────
#  DB 쿼리
# ─────────────────────────────────────────────────
def _query_videos(tag=None, search=None, offset=0, limit=40):
    c  = _conn()
    wh = []; pa = []
    if tag:
        wh.append("f.path IN (SELECT path FROM tags WHERE tag=?)")
        pa.append(tag)
    if search:
        wh.append("(f.name LIKE ? OR f.alias LIKE ?)")
        pa += [f'%{search}%', f'%{search}%']
    where = ('WHERE ' + ' AND '.join(wh)) if wh else ''
    total = c.execute(
        f"SELECT COUNT(*) FROM files f {where}", pa).fetchone()[0]
    rows = c.execute(
        f"SELECT f.path,f.name,f.alias,f.duration,f.size,f.width,f.height,"
        f"f.thumb_ok,COALESCE(f.description,'') "
        f"FROM files f {where} ORDER BY RANDOM() LIMIT ? OFFSET ?",
        pa + [limit, offset]).fetchall()
    cols = ('path','name','alias','duration','size','width','height','thumb_ok','description')
    return [dict(zip(cols,r)) for r in rows], total

def _get_video(vid_id):
    path = _get_path(vid_id)
    if not path: return None
    c = _conn()
    r = c.execute(
        "SELECT path,name,alias,duration,size,width,height,folder,"
        "COALESCE(description,'') "
        "FROM files WHERE path=?", (path,)).fetchone()
    if not r: return None
    v = dict(zip(('path','name','alias','duration','size','width','height','folder','description'), r))
    v['tags'] = [x[0] for x in c.execute(
        "SELECT tag FROM tags WHERE path=? ORDER BY tag", (path,)).fetchall()]
    v['id']   = vid_id
    v['dur_str']  = _fmt_dur(v['duration'])
    v['size_str'] = _fmt_size(v['size'])
    return v

def _get_tag_groups(limit_tags=6, vids_per_tag=6):
    """태그별 추천 그룹 — 영상 수 많은 태그 순"""
    c    = _conn()
    tags = c.execute(
        "SELECT tag, COUNT(*) as cnt FROM tags "
        "GROUP BY tag ORDER BY cnt DESC LIMIT ?", (limit_tags,)).fetchall()
    groups = []
    for tag, cnt in tags:
        rows = c.execute(
            "SELECT f.path,f.name,f.alias,f.duration,f.thumb_ok,"
            "COALESCE(f.description,'') "
            "FROM files f JOIN tags t ON f.path=t.path "
            "WHERE t.tag=? ORDER BY RANDOM() LIMIT ?",
            (tag, vids_per_tag)).fetchall()
        vids = []
        for r in rows:
            v = {'path':r[0],'name':r[1],'alias':r[2],'duration':r[3],
                 'thumb_ok':r[4],'description':r[5]}
            v['id']      = _vid_id(r[0])
            v['dur_str'] = _fmt_dur(r[3])
            v['tags']    = [tag]
            vids.append(v)
        if vids:
            groups.append({'tag': tag, 'cnt': cnt, 'vids': vids})
    return groups


def _get_related(vid_id, limit=16):
    path = _get_path(vid_id)
    if not path: return []
    c    = _conn()
    tags = [x[0] for x in c.execute(
        "SELECT tag FROM tags WHERE path=?", (path,)).fetchall()]
    if not tags: return []
    ph = ','.join('?'*len(tags))
    rows = c.execute(
        f"SELECT DISTINCT f.path,f.name,f.duration,f.thumb_ok "
        f"FROM files f JOIN tags t ON f.path=t.path "
        f"WHERE t.tag IN ({ph}) AND f.path != ? "
        f"ORDER BY RANDOM() LIMIT ?",
        tags + [path, limit]).fetchall()
    out = []
    for r in rows:
        out.append({'path':r[0],'name':r[1],'duration':r[2],
                    'thumb_ok':r[3],'id':_vid_id(r[0]),
                    'dur_str':_fmt_dur(r[2])})
    return out

def _get_tags_with_stats():
    c    = _conn()
    tags = c.execute(
        "SELECT t.tag, COUNT(t.path) as cnt, "
        "       m.description "
        "FROM tags t LEFT JOIN tag_meta m ON t.tag=m.tag "
        "GROUP BY t.tag ORDER BY cnt DESC").fetchall()
    out = []
    for tag, cnt, desc in tags:
        # 썸네일 있는 랜덤 영상
        r = c.execute(
            "SELECT f.path FROM files f JOIN tags t ON f.path=t.path "
            "WHERE t.tag=? AND f.thumb_ok=1 ORDER BY RANDOM() LIMIT 1",
            (tag,)).fetchone()
        thumb = _vid_id(r[0]) if r else None
        out.append({'tag':tag, 'cnt':cnt, 'desc':desc or '', 'thumb':thumb})
    return out

# ─────────────────────────────────────────────────
#  HTML 템플릿
# ─────────────────────────────────────────────────
_BASE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VidSort Gallery</title>
<style>
:root{--bg:#111;--bg2:#1a1a1a;--bg3:#222;--acc:#ff9000;--acc2:#e07800;
      --txt:#fff;--sub:#aaa;--brd:#333;--card:#1e1e1e}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:'Segoe UI',Arial,sans-serif;min-height:100vh}
a{color:inherit;text-decoration:none}
/* HEADER */
#hdr{background:#000;height:56px;display:flex;align-items:center;
     padding:0 20px;gap:16px;position:sticky;top:0;z-index:100;
     border-bottom:2px solid var(--acc)}
#logo{font-size:22px;font-weight:900;color:var(--acc);letter-spacing:-1px;white-space:nowrap}
#logo span{color:#fff;font-weight:300}
#search-form{flex:1;display:flex;max-width:600px;margin:0 auto}
#search-form input{flex:1;background:#222;border:1px solid #444;border-right:none;
  color:#fff;padding:8px 14px;font-size:14px;border-radius:4px 0 0 4px;outline:none}
#search-form input:focus{border-color:var(--acc)}
#search-form button{background:var(--acc);border:none;color:#000;font-weight:700;
  padding:8px 18px;cursor:pointer;border-radius:0 4px 4px 0;font-size:14px}
#search-form button:hover{background:var(--acc2)}
#hdr-nav{display:flex;gap:8px}
#hdr-nav a{padding:6px 14px;border-radius:4px;font-size:13px;
           background:#222;color:var(--sub);transition:.15s}
#hdr-nav a:hover,#hdr-nav a.act{background:var(--acc);color:#000;font-weight:700}
/* LAYOUT */
.wrap{max-width:1400px;margin:0 auto;padding:20px 16px}
.section-hdr{display:flex;align-items:baseline;gap:12px;margin-bottom:16px}
.section-hdr h2{font-size:18px;font-weight:700}
.section-hdr .cnt{color:var(--sub);font-size:13px}
.section-hdr a.more{margin-left:auto;color:var(--acc);font-size:13px}
.section-hdr a.more:hover{text-decoration:underline}
/* TAG GRID */
.tag-grid{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(200px,1fr));
  gap:12px;margin-bottom:36px}
.tag-card{position:relative;border-radius:6px;overflow:hidden;
  cursor:pointer;background:#000;aspect-ratio:16/9;
  transition:transform .18s,box-shadow .18s}
.tag-card:hover{transform:scale(1.03);
  box-shadow:0 4px 24px rgba(255,144,0,.35)}
.tag-card img{width:100%;height:100%;object-fit:cover;opacity:.7;
  transition:opacity .18s}
.tag-card:hover img{opacity:1}
.tag-card .tc-info{position:absolute;bottom:0;left:0;right:0;
  padding:24px 10px 8px;
  background:linear-gradient(transparent,rgba(0,0,0,.85))}
.tag-card .tc-name{font-size:14px;font-weight:700;color:#fff}
.tag-card .tc-cnt{font-size:11px;color:var(--acc)}
.tag-card .tc-desc{font-size:10px;color:#ccc;margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* VIDEO GRID */
.vid-grid{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
  gap:16px;margin-bottom:36px}
.vid-card{background:var(--card);border-radius:6px;overflow:hidden;
  cursor:pointer;transition:transform .18s,box-shadow .18s}
.vid-card:hover{transform:translateY(-3px);
  box-shadow:0 6px 24px rgba(0,0,0,.6)}
.vid-card .vc-thumb{position:relative;aspect-ratio:16/9;background:#000;overflow:hidden}
.vid-card .vc-thumb img{width:100%;height:100%;object-fit:cover;
  transition:transform .3s}
.vid-card:hover .vc-thumb img{transform:scale(1.05)}
.vid-card .vc-dur{position:absolute;bottom:6px;right:6px;
  background:rgba(0,0,0,.82);color:#fff;font-size:11px;font-weight:700;
  padding:2px 6px;border-radius:3px}
.vid-card .vc-play{position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;opacity:0;transition:.18s;
  background:rgba(0,0,0,.3)}
.vid-card:hover .vc-play{opacity:1}
.vc-play-ico{width:52px;height:52px;border-radius:50%;
  background:rgba(255,144,0,.9);display:flex;align-items:center;
  justify-content:center;font-size:20px}
.vid-card .vc-info{padding:10px 10px 12px}
.vid-card .vc-title{font-size:13px;font-weight:600;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;margin-bottom:6px;color:#fff}
.vid-card .vc-tags{display:flex;flex-wrap:wrap;gap:4px}
.vc-tag{background:#2a2a2a;color:var(--acc);font-size:10px;
  padding:2px 7px;border-radius:3px;cursor:pointer;transition:.15s}
.vc-tag:hover{background:var(--acc);color:#000}
.vc-desc{font-size:11px;color:#777;margin-top:5px;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* 태그 그룹 섹션 */
.tag-group{margin-bottom:40px}
.tag-group-hdr{display:flex;align-items:center;gap:10px;margin-bottom:12px;
  padding-bottom:8px;border-bottom:2px solid var(--acc)}
.tag-group-hdr h3{font-size:16px;font-weight:700}
.tag-group-hdr .tg-cnt{color:var(--sub);font-size:12px}
.tag-group-hdr a{margin-left:auto;color:var(--acc);font-size:13px}
/* 플레이어 설명 */
.player-desc{background:#1a1a1a;border-left:3px solid var(--acc);
  padding:12px 16px;margin-top:12px;border-radius:0 6px 6px 0;
  color:#ccc;font-size:14px;line-height:1.7;white-space:pre-wrap}
.no-thumb{background:#1a1a1a;display:flex;align-items:center;
  justify-content:center;color:#444;font-size:32px}
/* PAGINATION */
.pager{display:flex;justify-content:center;gap:8px;padding:20px 0 40px}
.pager a,.pager span{padding:7px 14px;border-radius:4px;font-size:14px;
  background:#222;color:var(--sub)}
.pager a:hover{background:#333;color:#fff}
.pager .cur{background:var(--acc);color:#000;font-weight:700}
/* TAGS BAR (search/tag page) */
.tag-bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.tag-pill{padding:5px 14px;border-radius:20px;font-size:13px;cursor:pointer;
  border:1px solid var(--brd);color:var(--sub);transition:.15s}
.tag-pill:hover{border-color:var(--acc);color:var(--acc)}
.tag-pill.sel{background:var(--acc);border-color:var(--acc);
  color:#000;font-weight:700}
/* VIDEO PLAYER PAGE */
.player-wrap{background:#000;border-radius:8px;overflow:hidden;margin-bottom:16px}
.player-wrap video{width:100%;display:block;max-height:72vh}
.player-meta{background:var(--bg2);border-radius:8px;padding:16px;margin-bottom:16px}
.player-meta h1{font-size:18px;font-weight:700;margin-bottom:10px;line-height:1.4}
.player-meta .meta-row{display:flex;gap:20px;flex-wrap:wrap;
  color:var(--sub);font-size:13px;margin-bottom:12px}
.player-meta .meta-row span b{color:#fff}
.btn-open{display:inline-flex;align-items:center;gap:6px;
  background:#2a2a2a;color:#fff;border:1px solid #444;
  padding:8px 18px;border-radius:4px;font-size:13px;cursor:pointer;
  transition:.15s;margin-top:8px}
.btn-open:hover{background:#333;border-color:var(--acc)}
.side-vids h3{font-size:15px;font-weight:700;margin-bottom:12px;
  color:var(--sub);text-transform:uppercase;letter-spacing:.5px}
.side-card{display:flex;gap:10px;margin-bottom:12px;cursor:pointer;
  border-radius:6px;padding:4px;transition:.15s}
.side-card:hover{background:#222}
.side-card .sc-thumb{width:120px;flex-shrink:0;aspect-ratio:16/9;
  background:#000;border-radius:4px;overflow:hidden}
.side-card .sc-thumb img{width:100%;height:100%;object-fit:cover}
.side-card .sc-info{flex:1;min-width:0}
.side-card .sc-title{font-size:12px;font-weight:600;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  overflow:hidden;color:#ddd}
.side-card .sc-dur{font-size:11px;color:var(--acc);margin-top:4px}
/* Breadcrumb */
.breadcrumb{color:var(--sub);font-size:13px;margin-bottom:20px}
.breadcrumb a{color:var(--acc)}.breadcrumb a:hover{text-decoration:underline}
/* Flash message */
.flash{background:#1a1a1a;border:1px solid var(--acc);color:var(--acc);
  padding:10px 16px;border-radius:6px;margin-bottom:16px;font-size:14px}
/* Responsive */
@media(max-width:900px){
  .vid-grid{grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
  .tag-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
}
</style>
</head>
<body>
<header id="hdr">
  <a href="/" id="logo">▶<span>VidSort</span></a>
  <form id="search-form" action="/search" method="get">
    <input name="q" placeholder="제목, 파일명 검색..." value="{{ q|default('') }}">
    <button type="submit">검색</button>
  </form>
  <nav id="hdr-nav">
    <a href="/" class="{{ 'act' if nav=='home' else '' }}">홈</a>
    <a href="/tags" class="{{ 'act' if nav=='tags' else '' }}">카테고리</a>
    <a href="/search" class="{{ 'act' if nav=='search' else '' }}">검색</a>
  </nav>
</header>
__BODY__
<script>
function openNative(vid_id){
  fetch('/open/'+vid_id).then(()=>console.log('opened'));
}
</script>
__SCRIPTS__
</body>
</html>"""

_HOME = _BASE.replace('__BODY__', """
<div class="wrap">
  <!-- 카테고리 -->
  {% if tags %}
  <div class="section-hdr">
    <h2>카테고리</h2>
    <span class="cnt">{{ tags|length }}개 태그</span>
    <a href="/tags" class="more">전체 보기 →</a>
  </div>
  <div class="tag-grid">
  {% for t in tags[:12] %}
    <a href="/tag/{{ t.tag|urlencode }}" class="tag-card">
      {% if t.thumb %}
      <img src="/thumb/{{ t.thumb }}" loading="lazy" alt="{{ t.tag }}">
      {% else %}
      <div class="no-thumb" style="height:100%">🎬</div>
      {% endif %}
      <div class="tc-info">
        <div class="tc-name">{{ t.tag }}</div>
        <div class="tc-cnt">{{ t.cnt }}개 영상</div>
        {% if t.desc %}<div class="tc-desc">{{ t.desc }}</div>{% endif %}
      </div>
    </a>
  {% endfor %}
  </div>
  {% endif %}

  <!-- 태그 그룹 추천 -->
  {% for grp in tag_groups %}
  <div class="tag-group">
    <div class="tag-group-hdr">
      <h3>{{ grp.tag }}</h3>
      <span class="tg-cnt">{{ grp.cnt }}개 영상</span>
      <a href="/tag/{{ grp.tag|urlencode }}">전체 보기 →</a>
    </div>
    <div class="vid-grid">
    {% for v in grp.vids %}{{ _vid_card(v) }}{% endfor %}
    </div>
  </div>
  {% endfor %}

  <!-- 전체 랜덤 추천 -->
  <div class="section-hdr">
    <h2>랜덤 추천</h2>
    <span class="cnt">{{ total }}개 영상</span>
  </div>
  <div class="vid-grid">
  {% for v in videos %}{{ _vid_card(v) }}{% endfor %}
  </div>
  {{ _pager(page, pages, '/') }}
</div>
""").replace('__SCRIPTS__', '')

_TAGS = _BASE.replace('__BODY__', """
<div class="wrap">
  <div class="section-hdr">
    <h2>카테고리 전체</h2>
    <span class="cnt">{{ tags|length }}개 태그</span>
  </div>
  <div class="tag-grid">
  {% for t in tags %}
    <a href="/tag/{{ t.tag|urlencode }}" class="tag-card">
      {% if t.thumb %}
      <img src="/thumb/{{ t.thumb }}" loading="lazy" alt="{{ t.tag }}">
      {% else %}
      <div class="no-thumb" style="height:100%">🎬</div>
      {% endif %}
      <div class="tc-info">
        <div class="tc-name">{{ t.tag }}</div>
        <div class="tc-cnt">{{ t.cnt }}개 영상</div>
        {% if t.desc %}<div class="tc-desc">{{ t.desc }}</div>{% endif %}
      </div>
    </a>
  {% endfor %}
  </div>
</div>
""").replace('__SCRIPTS__', '')

_TAG_PAGE = _BASE.replace('__BODY__', """
<div class="wrap">
  <div class="breadcrumb">
    <a href="/">홈</a> › <a href="/tags">카테고리</a> › {{ tag }}
  </div>
  <div class="section-hdr">
    <h2>{{ tag }}</h2>
    <span class="cnt">{{ total }}개 영상</span>
  </div>
  {% if desc %}<p style="color:var(--sub);margin-bottom:16px;font-size:14px">{{ desc }}</p>{% endif %}
  <div class="vid-grid">
  {% for v in videos %}{{ _vid_card(v) }}{% endfor %}
  </div>
  {{ _pager(page, pages, '/tag/'~tag) }}
</div>
""").replace('__SCRIPTS__', '')

_SEARCH = _BASE.replace('__BODY__', """
<div class="wrap">
  {% if q %}
  <div class="section-hdr">
    <h2>"{{ q }}" 검색 결과</h2>
    <span class="cnt">{{ total }}개</span>
  </div>
  {% else %}
  <div class="section-hdr"><h2>전체 영상</h2><span class="cnt">{{ total }}개</span></div>
  {% endif %}
  <div class="vid-grid">
  {% for v in videos %}{{ _vid_card(v) }}{% endfor %}
  </div>
  {{ _pager(page, pages, '/search', q=q) }}
</div>
""").replace('__SCRIPTS__', '')

_VIDEO = _BASE.replace('__BODY__', """
<div class="wrap">
  <div class="breadcrumb"><a href="/">홈</a> › 영상</div>
  <div style="display:grid;grid-template-columns:1fr 300px;gap:20px">
    <div>
      <div class="player-wrap">
        <video id="vp" controls autoplay preload="metadata"
               src="/stream/{{ v.id }}">
          브라우저가 HTML5 비디오를 지원하지 않습니다.
        </video>
      </div>
      <div class="player-meta">
        <h1>{{ v.alias or v.name }}</h1>
        <div class="meta-row">
          {% if v.dur_str %}<span><b>{{ v.dur_str }}</b> 재생시간</span>{% endif %}
          {% if v.width %}<span><b>{{ v.width }}×{{ v.height }}</b></span>{% endif %}
          {% if v.size_str %}<span><b>{{ v.size_str }}</b></span>{% endif %}
        </div>
        <div class="tag-bar">
        {% for tg in v.tags %}
          <a href="/tag/{{ tg|urlencode }}" class="tag-pill">{{ tg }}</a>
        {% endfor %}
        </div>
        {% if v.description %}
        <div class="player-desc">{{ v.description }}</div>
        {% endif %}
        <button class="btn-open" onclick="openNative('{{ v.id }}')">
          📂 시스템 플레이어로 열기
        </button>
      </div>
    </div>
    <aside>
      <div class="side-vids">
        <h3>관련 영상</h3>
        {% for r in related %}
        <a href="/video/{{ r.id }}" class="side-card">
          <div class="sc-thumb">
            {% if r.thumb_ok %}
            <img src="/thumb/{{ r.id }}" loading="lazy">
            {% else %}
            <div class="no-thumb" style="height:100%">🎬</div>
            {% endif %}
          </div>
          <div class="sc-info">
            <div class="sc-title">{{ r.name }}</div>
            {% if r.dur_str %}<div class="sc-dur">{{ r.dur_str }}</div>{% endif %}
          </div>
        </a>
        {% endfor %}
      </div>
    </aside>
  </div>
</div>
""").replace('__SCRIPTS__', '''<script>
// 키보드 단축키
document.addEventListener('keydown',function(e){
  const vp=document.getElementById('vp');
  if(!vp)return;
  if(e.code==='Space'){e.preventDefault();vp.paused?vp.play():vp.pause()}
  if(e.code==='ArrowRight')vp.currentTime+=10;
  if(e.code==='ArrowLeft')vp.currentTime-=10;
  if(e.code==='ArrowUp'){e.preventDefault();vp.volume=Math.min(1,vp.volume+.1)}
  if(e.code==='ArrowDown'){e.preventDefault();vp.volume=Math.max(0,vp.volume-.1)}
  if(e.code==='KeyF'){if(!document.fullscreenElement)vp.requestFullscreen();
                      else document.exitFullscreen()}
});
</script>
''')
# ─────────────────────────────────────────────────
#  공통 Jinja2 매크로
# ─────────────────────────────────────────────────
def _render(template, **ctx):
    from urllib.parse import quote
    from markupsafe import Markup

    def _vid_card(v):
        from markupsafe import escape
        tags_html = ''.join(
            f'<span class="vc-tag">{escape(t)}</span>'
            for t in v.get('tags', [])[:4])
        thumb = (f'<img src="/thumb/{v["id"]}" loading="lazy" alt="">'
                 if v.get('thumb_ok') else
                 '<div class="no-thumb" style="height:100%">🎬</div>')
        dur   = (f'<span class="vc-dur">{v["dur_str"]}</span>'
                 if v.get('dur_str') else '')
        desc  = v.get('description', '') or ''
        desc_html = (f'<div class="vc-desc">{escape(desc)}</div>' if desc else '')
        title = escape(v.get("alias") or v["name"])
        return Markup(f'''
        <a href="/video/{v["id"]}" class="vid-card">
          <div class="vc-thumb">{thumb}{dur}
            <div class="vc-play"><div class="vc-play-ico">▶</div></div>
          </div>
          <div class="vc-info">
            <div class="vc-title">{title}</div>
            {desc_html}
            <div class="vc-tags">{tags_html}</div>
          </div>
        </a>''')

    def _pager(page, pages, base, **kw):
        if pages <= 1: return ''
        q_str = '&'.join(f'{k}={quote(str(v))}' for k,v in kw.items() if v)
        sep   = '&' if q_str else ''
        html  = '<div class="pager">'
        if page > 1:
            html += f'<a href="{base}?page={page-1}{sep}{q_str}">‹ 이전</a>'
        for p in range(max(1,page-3), min(pages+1,page+4)):
            cls = 'cur' if p==page else ''
            html += f'<{"span" if p==page else "a"} class="{cls}" {"" if p==page else f"href={chr(39)}{base}?page={p}{sep}{q_str}{chr(39)}"} >{p}</{"span" if p==page else "a"}>'
        if page < pages:
            html += f'<a href="{base}?page={page+1}{sep}{q_str}">다음 ›</a>'
        html += '</div>'
        return Markup(html)

    ctx.setdefault('q', '')
    ctx.setdefault('nav', 'home')
    ctx['_vid_card'] = _vid_card
    ctx['_pager']    = _pager
    ctx['urlencode'] = quote
    return render_template_string(template, **ctx)

# ─────────────────────────────────────────────────
#  라우트
# ─────────────────────────────────────────────────
@app.route('/')
def index():
    pg    = int(request.args.get('page', 1))
    PER   = 40
    vids, total = _query_videos(offset=(pg-1)*PER, limit=PER)
    tags        = _get_tags_with_stats()
    tag_groups  = _get_tag_groups(limit_tags=6, vids_per_tag=6)
    c           = _conn()
    for v in vids:
        v['id']      = _vid_id(v['path'])
        v['dur_str'] = _fmt_dur(v['duration'])
        v['tags']    = [x[0] for x in c.execute(
            "SELECT tag FROM tags WHERE path=? ORDER BY tag",
            (v['path'],)).fetchall()]
    return _render(_HOME, nav='home', videos=vids, tags=tags,
                   tag_groups=tag_groups,
                   total=total, page=pg, pages=(total+PER-1)//PER)

@app.route('/tags')
def tags_page():
    tags = _get_tags_with_stats()
    return _render(_TAGS, nav='tags', tags=tags)

@app.route('/tag/<tagname>')
def tag_page(tagname):
    pg   = int(request.args.get('page', 1))
    PER  = 40
    vids, total = _query_videos(tag=tagname, offset=(pg-1)*PER, limit=PER)
    c    = _conn()
    desc = (c.execute("SELECT description FROM tag_meta WHERE tag=?",
                      (tagname,)).fetchone() or ('',))[0]
    for v in vids:
        v['id']      = _vid_id(v['path'])
        v['dur_str'] = _fmt_dur(v['duration'])
        v['tags']    = [x[0] for x in c.execute(
            "SELECT tag FROM tags WHERE path=? ORDER BY tag",
            (v['path'],)).fetchall()]
    return _render(_TAG_PAGE, nav='tags', tag=tagname, desc=desc,
                   videos=vids, total=total,
                   page=pg, pages=(total+PER-1)//PER)

@app.route('/search')
def search_page():
    q    = request.args.get('q', '').strip()
    pg   = int(request.args.get('page', 1))
    PER  = 40
    vids, total = _query_videos(search=q if q else None,
                                offset=(pg-1)*PER, limit=PER)
    c    = _conn()
    for v in vids:
        v['id']      = _vid_id(v['path'])
        v['dur_str'] = _fmt_dur(v['duration'])
        v['tags']    = [x[0] for x in c.execute(
            "SELECT tag FROM tags WHERE path=? ORDER BY tag",
            (v['path'],)).fetchall()]
    return _render(_SEARCH, nav='search', q=q, videos=vids,
                   total=total, page=pg, pages=(total+PER-1)//PER)

@app.route('/video/<vid_id>')
def video_page(vid_id):
    v = _get_video(vid_id)
    if not v: abort(404)
    related = _get_related(vid_id, limit=16)
    return _render(_VIDEO, nav='', v=v, related=related)

@app.route('/open/<vid_id>')
def open_native(vid_id):
    path = _get_path(vid_id)
    if path:
        try:
            if sys.platform == 'win32':   os.startfile(path)
            elif sys.platform == 'darwin': import subprocess; subprocess.Popen(['open', path])
            else:                          import subprocess; subprocess.Popen(['xdg-open', path])
        except: pass
    return jsonify({'ok': True})

@app.route('/thumb/<h>')
def serve_thumb(h):
    p = Path(_cfg['thumb_dir']) / (h + '.jpg')
    if p.exists():
        return send_file(str(p), mimetype='image/jpeg',
                         max_age=86400)
    abort(404)

@app.route('/stream/<vid_id>')
def stream_video(vid_id):
    path = _get_path(vid_id)
    if not path: abort(404)

    # Windows 경로 처리
    # NAS/UNC (\\server\ 또는 //server/) 는 \\?\ 접두어 금지
    if sys.platform == 'win32':
        is_unc = path.startswith('\\\\') or path.startswith('//')
        if is_unc:
            lp = path
        else:
            try:
                lp = '\\\\?\\' + str(Path(path).resolve())
            except Exception:
                lp = path
    else:
        lp = path

    if not os.path.exists(lp): abort(404)

    try:
        size = os.path.getsize(lp)
    except Exception:
        abort(500)

    mime = mimetypes.guess_type(path)[0] or 'video/mp4'
    rng  = request.headers.get('Range')

    if rng:
        try:
            raw = rng.replace('bytes=', '')
            b1s, b2s = raw.split('-', 1)
            b1 = int(b1s)
            b2 = int(b2s) if b2s.strip() else size - 1
            b2 = min(b2, size - 1)
            if b1 > b2: abort(416)
        except (ValueError, TypeError):
            abort(416)
        length = b2 - b1 + 1

        def gen(start=b1, end=b2, fp=lp):
            with open(fp, 'rb') as f:
                f.seek(start)
                rem = end - start + 1
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk: break
                    yield chunk
                    rem -= len(chunk)

        rv = Response(gen(), 206, mimetype=mime, direct_passthrough=True)
        rv.headers['Content-Range']  = f'bytes {b1}-{b2}/{size}'
        rv.headers['Accept-Ranges']  = 'bytes'
        rv.headers['Content-Length'] = str(length)
        return rv

    # 전체 파일 스트리밍
    def full_gen(fp=lp):
        with open(fp, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk: break
                yield chunk

    rv = Response(full_gen(), 200, mimetype=mime, direct_passthrough=True)
    rv.headers['Accept-Ranges']  = 'bytes'
    rv.headers['Content-Length'] = str(size)
    return rv

# ─────────────────────────────────────────────────
#  서버 시작
# ─────────────────────────────────────────────────
def start(db_path, thumb_dir, port=8765):
    global _srv
    _cfg['db_path']   = db_path
    _cfg['thumb_dir'] = thumb_dir
    _cfg['port']      = port

    _build_cache()

    if _srv and _srv.is_alive():
        webbrowser.open(f'http://localhost:{port}')
        return

    def _run():
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        app.run(host='127.0.0.1', port=port, threaded=True,
                debug=False, use_reloader=False)

    _srv = threading.Thread(target=_run, daemon=True, name='vidsort-web')
    _srv.start()
    time.sleep(0.9)
    webbrowser.open(f'http://localhost:{port}')
