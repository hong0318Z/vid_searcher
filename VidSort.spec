# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['vidsort.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # ── 로컬 모듈 (동적 import라 자동 감지 안 됨) ──────────
        'web_gallery',
        'jav_scraper',
        'llm_api',

        # ── Flask / 웹 갤러리 ────────────────────────────────
        'flask',
        'flask.templating',
        'flask.json',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.routing',
        'werkzeug.exceptions',
        'werkzeug.middleware.dispatcher',
        'jinja2',
        'jinja2.ext',
        'markupsafe',

        # ── httpx (LLM API + 스크래퍼) ───────────────────────
        'httpx',
        'httpcore',
        'httpcore._sync.interfaces',
        'httpcore._async.interfaces',
        'anyio',
        'anyio._backends._asyncio',
        'anyio._backends._trio',

        # ── 스크래퍼 ─────────────────────────────────────────
        'bs4',
        'bs4.builder',
        'bs4.builder._htmlparser',
        'html.parser',

        # ── curl_cffi (선택 — Cloudflare 우회) ──────────────
        # 설치되지 않은 경우 주석 처리해도 빌드 가능
        'curl_cffi',
        'curl_cffi.requests',

        # ── PIL / 썸네일 ─────────────────────────────────────
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'PIL.ImageFile',
        'PIL.JpegImagePlugin',

        # ── python-vlc (인라인 플레이어, 선택) ──────────────
        'vlc',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VidSort',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
