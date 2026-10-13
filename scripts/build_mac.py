#!/usr/bin/env python3
"""Build a local Apple Silicon Mac app from a normal checkout."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/mac'


def run(arguments, **kwargs):
    subprocess.run([str(a) for a in arguments], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description='Build FilmDust-Mac.dmg')
    parser.add_argument('--build-python', type=Path, help='Use an already-prepared Python with build dependencies')
    parser.add_argument('--verify', action='store_true', help='Run the real native app with the artificial sample before packaging')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Mac App打包需要Apple Silicon Mac；其他平台可运行浏览器版。')
    for executable in ['swiftc', 'codesign', 'hdiutil']:
        if not shutil.which(executable):
            raise SystemExit(f'缺少{executable}，请先准备Apple命令行开发工具。')
    WORK.mkdir(parents=True, exist_ok=True)
    if args.build_python:
        # Keep a venv's executable path; resolving its symlink loses pyvenv.cfg.
        python = Path(os.path.abspath(args.build_python))
        if not python.is_file():
            raise SystemExit('指定的构建Python不存在。')
    else:
        environment = WORK / 'venv'
        python = environment / 'bin/python'
        if not python.exists():
            run([sys.executable, '-m', 'venv', environment])
        run([python, '-m', 'pip', 'install', '-r', ROOT / 'macos/requirements-build.txt'])
    build_environment = os.environ.copy()
    build_environment['PYINSTALLER_CONFIG_DIR'] = str(WORK / 'pyinstaller-cache')
    command = [python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'FilmDustEngine',
               '--target-arch', 'arm64', '--distpath', WORK / 'dist', '--workpath', WORK / 'pyinstaller',
               '--specpath', WORK, '--paths', ROOT / 'filmdust',
               '--add-data', str(ROOT / 'filmdust/web') + ':web',
               '--add-data', str(ROOT / 'filmdust/examples') + ':examples', '--collect-all', 'imagecodecs']
    for module in ['matplotlib', 'scipy', 'torch', 'pandas', 'IPython', 'pytest', 'tkinter', 'PIL.ImageTk', '_tkinter', 'zarr', 'numcodecs', 'dask']:
        command += ['--exclude-module', module]
    command.append(ROOT / 'macos/desktop_engine.py')
    run(command, env=build_environment)
    run(['swiftc', '-swift-version', '5', '-O', '-target', 'arm64-apple-macosx13.0',
         '-module-cache-path', WORK / 'swift-cache', '-framework', 'AppKit', '-framework', 'WebKit',
         '-framework', 'UniformTypeIdentifiers', ROOT / 'macos/FilmDust.swift', '-o', WORK / 'FilmDustLauncher'])
    with tempfile.TemporaryDirectory(prefix='filmdust-mac-') as temporary:
        stage = Path(temporary)
        app = stage / 'FilmDust.app'
        build_environment['FILMDUST_BUILD_APP'] = str(app)
        run([python, ROOT / 'macos/build_app.py'], env=build_environment)
        if args.verify:
            check = WORK / 'native-check'
            if check.exists():
                shutil.rmtree(check)
            run([app / 'Contents/MacOS/FilmDust', '--verify-app', check], timeout=90)
            failure = check / 'native-failure.txt'
            if failure.exists():
                raise RuntimeError(failure.read_text())
            report = json.loads((check / 'native-check.json').read_text())
            if not report.get('ready') or not report.get('nativeBridge'):
                raise RuntimeError('Native app verification failed')
            run([python, '-c', "import sys,tifffile; a=tifffile.imread(sys.argv[1]); assert a.dtype.name=='uint8' and a.ndim==3", check / 'native-export.tif'])
            print('Native app processing and saving verified')
        installer = stage / 'installer'
        installer.mkdir()
        shutil.copytree(app, installer / 'FilmDust.app', symlinks=True)
        (installer / 'Applications').symlink_to('/Applications')
        (installer / '安装说明.txt').write_text('将FilmDust拖到Applications（应用程序）后打开。\n无需终端或Python，照片完全在本机处理，原稿不覆盖。\nM系列Mac、macOS13及以上。本地临时签名，未做Apple公证。\n')
        run(['codesign', '--verify', '--deep', '--strict', installer / 'FilmDust.app'])
        destination = ROOT / 'dist/FilmDust-Mac.dmg'
        destination.parent.mkdir(exist_ok=True)
        run(['hdiutil', 'create', '-volname', 'FilmDust', '-srcfolder', installer, '-format', 'UDZO', '-ov', destination])
        run(['hdiutil', 'verify', destination])
        print('Built:', destination)


if __name__ == '__main__':
    main()
