# BuyRobot.spec  —  PyInstaller build spec
# Run:  pyinstaller BuyRobot.spec

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

added_files = [
    ("buy_index.html",    "."),
    ("buy_settings.html", "."),
    ("static",            "static"),
]

hidden_imports = (
    collect_submodules("engineio")
    + collect_submodules("socketio")
    + collect_submodules("flask_socketio")
    + collect_submodules("websockets")
    + collect_submodules("numpy")
    + [
        "engineio.async_drivers.threading",
        "dns.resolver",
    ]
)

a = Analysis(
    ["buy_app.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="BuyRobot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # keep True so you can see errors; set False to hide terminal
    icon=None,
)
