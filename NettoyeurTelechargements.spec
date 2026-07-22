# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    # assets/icon.ico embarque dans l'exe onefile (audit D8/E1) : icon= sur
    # EXE() ci-dessous ne sert que l'icone du FICHIER exe/l'icone Explorateur/
    # barre des taches - la fenetre elle-meme (self.iconbitmap dans gui.py)
    # doit pouvoir relire ce meme fichier depuis le dossier temporaire
    # d'extraction PyInstaller (sys._MEIPASS) au demarrage, d'ou cette entree
    # 'datas' qui le rend accessible a l'execution, pas seulement au build.
    datas=[('assets/icon.ico', 'assets')],
    hiddenimports=[],
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
    name='NettoyeurTelechargements',
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
    icon='assets/icon.ico',
)
