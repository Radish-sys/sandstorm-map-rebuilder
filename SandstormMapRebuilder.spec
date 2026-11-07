# PyInstaller build for the app: pyinstaller SandstormMapRebuilder.spec
# One windowed exe; the SDK-side scripts ride along and are installed into the SDK by the app.
a = Analysis(
    ['app/gui.py'],
    pathex=['.'],
    datas=[('sdk/smr_menu.py', 'sdk'), ('sdk/rebuild_map.py', 'sdk'), ('app/smr.ico', 'app')],
    hiddenimports=['smr.extract', 'smr.staticmesh', 'smr.gloss', 'smr.uasset', 'smr.pak', 'smr.preflight',
                   'scipy.spatial', 'scipy.spatial._qhull'],
    excludes=['matplotlib', 'pandas', 'IPython', 'pytest'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='SandstormMapRebuilder',
    console=False,
    icon='app/smr.ico',
    upx=False,
)
