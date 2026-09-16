# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Dynamically gather all hidden imports, binaries, and data for aiohttp
aio_datas, aio_binaries, aio_hiddenimports = collect_all('aiohttp')

a = Analysis(
    ['services/orchestrator/src/ipc/uds_server.py'],
    pathex=['.', 'services/orchestrator/src'],
    binaries=[] + aio_binaries,
    datas=[
        ('services/orchestrator/config.json', 'services/orchestrator')
    ] + aio_datas,
    hiddenimports=[
        'services',
        'rag',
        'agent',
        'memory',
        'lancedb',
        'fastembed',
        'aiohttp'
    ] + aio_hiddenimports,
    hookspath=[],
    hooksconfig={}
,
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
    name='uds_server-win-x64',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
