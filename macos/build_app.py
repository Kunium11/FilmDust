#!/usr/bin/env python3
"""Assemble the native app around the frozen engine; never edit existing apps."""
from pathlib import Path
import importlib.metadata as metadata
import plistlib, shutil, subprocess, sys, os, stat
from PIL import Image, ImageDraw

root=Path(__file__).resolve().parents[1]
work=root/'build/mac'
app=Path(os.environ.get('FILMDUST_BUILD_APP','/private/tmp/filmdust-app-build/FilmDust.app'))
if app.exists():
    # Retain the previous build until the new one is verified.
    previous=work/'previous-build.app'
    if previous.exists():shutil.rmtree(previous)
    shutil.move(str(app),str(previous))
contents=app/'Contents';resources=contents/'Resources';binary=contents/'MacOS'
binary.mkdir(parents=True);resources.mkdir()
def copy_runtime(source, destination):
    shutil.copyfile(source, destination)
    os.chmod(destination, stat.S_IMODE(os.stat(source).st_mode))
    return str(destination)
copy_runtime(work/'FilmDustLauncher',binary/'FilmDust')
shutil.copytree(work/'dist/FilmDustEngine',resources/'Engine',symlinks=True,copy_function=copy_runtime)
info={
 'CFBundleDevelopmentRegion':'zh_CN', 'CFBundleIdentifier':'local.filmdust.mac',
 'CFBundleName':'FilmDust', 'CFBundleDisplayName':'FilmDust',
 'CFBundleExecutable':'FilmDust','CFBundlePackageType':'APPL',
 'CFBundleShortVersionString':'1.1.2','CFBundleVersion':'4',
 'CFBundleIconFile':'FilmDust.icns','LSMinimumSystemVersion':'13.0',
 'NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication',
 'LSApplicationCategoryType':'public.app-category.photography',
 'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
 'WKAppBoundDomains':['127.0.0.1'],
 'NSHumanReadableCopyright':'FilmDust · 本机处理，原图保留',
}
with (contents/'Info.plist').open('wb') as f:plistlib.dump(info,f)
size=1024
icon=Image.new('RGBA',(size,size));pix=icon.load()
for y in range(size):
    for x in range(size):
        t=(x+y)/(size*2)
        pix[x,y]=(int(75*(1-t)),int(172-52*t),255,255)
mask=Image.new('L',(size,size));ImageDraw.Draw(mask).rounded_rectangle((48,48,976,976),radius=220,fill=255)
icon.putalpha(mask)
d=ImageDraw.Draw(icon)
d.rounded_rectangle((232,232,792,792),radius=152,outline='white',width=32)
for x in (395,629):d.line((x,240,x,784),fill='white',width=27)
for y in (398,626):
    d.line((242,y,390,y),fill='white',width=24);d.line((634,y,782,y),fill='white',width=24)
d.polygon([(462,564),(512,468),(562,564)],outline='white',width=22)
icon.save(resources/'FilmDust.icns',format='ICNS')
licenses=resources/'Licenses';licenses.mkdir()
shutil.copy2(root/'LICENSE',licenses/'FilmDust-LICENSE.txt')
for package in ['numpy','opencv-python','opencv-python-headless','Pillow','tifffile','imagecodecs','pyinstaller']:
    try:dist=metadata.distribution(package)
    except metadata.PackageNotFoundError:continue
    folder=licenses/package;folder.mkdir()
    for entry in dist.files or []:
        name=str(entry)
        if any(part.upper().startswith(('LICENSE','COPYING','NOTICE')) for part in entry.parts) and dist.locate_file(entry).is_file():
            target=folder/entry.name
            if not target.exists():shutil.copy2(dist.locate_file(entry),target)
    (folder/'package.txt').write_text(f'{package} {dist.version}\n'+(dist.metadata.get('License') or '')+'\n')
python_license=Path(sys.base_prefix)/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'LICENSE.txt'
if python_license.exists():shutil.copy2(python_license,licenses/'Python-LICENSE.txt')
# Strip Finder-only decoration from our new build, never quarantine or an
# existing third-party app. Resource forks are rejected by codesign.
for item in [app, *app.rglob('*')]:
    if item.is_symlink():continue
    for attribute in ['com.apple.FinderInfo', 'com.apple.ResourceFork']:
        subprocess.run(['/usr/bin/xattr', '-d', attribute, str(item)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# swiftc already ad-hoc signs the native executable; the PyInstaller binaries
# retain their own signatures. Sign the assembled outer bundle only.
subprocess.run(['codesign','--force','--sign','-','--timestamp=none',str(app)],check=True)
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
print(app)
