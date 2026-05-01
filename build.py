#!/usr/bin/env python3
"""Build llm.app and llm.dmg for Apple Silicon macOS."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

DIST_DIR = Path('dist')
APP_NAME = 'llm.app'
DMG_NAME = 'llm.dmg'
SPEC_FILE = 'llm.spec'
_TMP_DMG_STAGING = Path('/tmp/llm_dmg_staging')
_ICON_PNG = Path('docs/assets/llm-512x512.png')
_ICON_ICNS = Path('docs/assets/llm-512x512.icns')
_TMP_ICONSET = Path('/tmp/llm_icon.iconset')


def make_icns() -> None:
    print('--- Generating icns from PNG ---')
    if _TMP_ICONSET.exists():
        shutil.rmtree(_TMP_ICONSET)
    _TMP_ICONSET.mkdir(parents=True)
    sizes = [16, 32, 64, 128, 256, 512]
    for size in sizes:
        out = _TMP_ICONSET / f'icon_{size}x{size}.png'
        subprocess.run(
            ['sips', '-z', str(size), str(size), str(_ICON_PNG), '--out', str(out)],
            check=True,
            capture_output=True,
        )
        out2x = _TMP_ICONSET / f'icon_{size}x{size}@2x.png'
        size2x = min(size * 2, 512)
        subprocess.run(
            ['sips', '-z', str(size2x), str(size2x), str(_ICON_PNG), '--out', str(out2x)],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ['iconutil', '-c', 'icns', str(_TMP_ICONSET), '-o', str(_ICON_ICNS)],
        check=True,
    )
    shutil.rmtree(_TMP_ICONSET)
    print(f'  Written: {_ICON_ICNS}')


def check_platform() -> None:
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        print(
            'Error: this build script targets Apple Silicon macOS only.',
            file=sys.stderr,
        )
        sys.exit(1)


def run_pyinstaller() -> None:
    print('--- PyInstaller build ---')
    try:
        subprocess.run(
            ['pyinstaller', SPEC_FILE, '--clean', '--noconfirm'],
            check=True,
        )
    except FileNotFoundError:
        print(
            'Error: pyinstaller not found.\nRun this via pixi: pixi run build',
            file=sys.stderr,
        )
        sys.exit(1)


def create_dmg() -> None:
    print('--- Creating DMG ---')
    app_src = DIST_DIR / APP_NAME
    dmg_out = DIST_DIR / DMG_NAME

    if not app_src.exists():
        print(f'Error: {app_src} not found.', file=sys.stderr)
        sys.exit(1)

    if _TMP_DMG_STAGING.exists():
        shutil.rmtree(_TMP_DMG_STAGING)
    _TMP_DMG_STAGING.mkdir(parents=True)

    shutil.copytree(app_src, _TMP_DMG_STAGING / APP_NAME)
    (_TMP_DMG_STAGING / 'Applications').symlink_to('/Applications')

    subprocess.run(
        [
            'hdiutil',
            'create',
            '-volname',
            'llm',
            '-srcfolder',
            str(_TMP_DMG_STAGING),
            '-ov',
            '-format',
            'UDZO',
            str(dmg_out),
        ],
        check=True,
    )

    shutil.rmtree(_TMP_DMG_STAGING)


def print_summary() -> None:
    app_path = DIST_DIR / APP_NAME
    dmg_path = DIST_DIR / DMG_NAME
    binary = app_path / 'Contents' / 'MacOS' / 'llm'

    print()
    print('Build complete.')
    print(f'  App bundle  {app_path}')
    print(f'  DMG         {dmg_path}')
    print()
    print('To use from Terminal without installing:')
    print(f'  {binary} --help')
    print()
    print('To install system-wide (add to ~/.zshrc or ~/.bashrc):')
    binary_dir = app_path / 'Contents' / 'MacOS'
    print(f'  export PATH="{binary_dir}:$PATH"')
    print()
    print('To install via DMG:')
    print(f'  open {dmg_path}')
    print('  Drag llm.app to Applications, then add to PATH as above.')


def main() -> None:
    check_platform()
    make_icns()
    run_pyinstaller()
    create_dmg()
    print_summary()


if __name__ == '__main__':
    main()
