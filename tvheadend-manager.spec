# PyInstaller 单文件构建配置；运行 scripts/build-binary.sh 即可。
a = Analysis(['app.py'], datas=[('static', 'static')], hiddenimports=[], hookspath=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='tvheadend-manager', console=True, strip=True)
