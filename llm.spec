# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for llm_dashboard — Apple Silicon macOS only.

a = Analysis(
       ['llm_dashboard.py'],
     pathex=[],
     binaries=[],
     datas=[],
     hiddenimports=['wx', 'wx.lib.scrolledpanel'],
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
     console=False,
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
     icon='docs/assets/llm.icns',
     bundle_identifier='com.llmwrapper.llm',
     version='1.0.0',
     info_plist={
           'CFBundleName': 'LLM Dashboard',
           'CFBundleDisplayName': 'LLM Dashboard',
           'CFBundleVersion': '1.0.0',
           'CFBundleShortVersionString': '1.0.0',
           'LSMinimumSystemVersion': '12.0',
           'NSHighResolutionCapable': True,
       },
 )
