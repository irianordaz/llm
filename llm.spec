# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for llm — Apple Silicon macOS only.

a = Analysis(
    ['llm.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='llm',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='llm',
)

app = BUNDLE(
    coll,
    name='llm.app',
    icon=None,
    bundle_identifier='com.llmwrapper.llm',
    version='1.0.0',
    info_plist={
        'CFBundleName': 'llm',
        'CFBundleDisplayName': 'LLM Provider Wrapper',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'LSBackgroundOnly': True,
    },
)
