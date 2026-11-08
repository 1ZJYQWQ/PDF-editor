# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（目录版）。

    python -m PyInstaller pdfviewer.spec

★ 为什么是"目录版"而不是单文件
  单文件每次启动都要把 30MB 解压到临时目录，冷启动 3~6 秒，
  而且 UPX/自解压在杀软眼里更像可疑程序。目录版启动 1~2 秒，
  出问题还能直接看见是哪个文件缺失。

★ upx=False 是刻意的
  UPX 压缩是 Windows Defender 误报的常见触发点。省下的那几 MB
  不值得换来"下载完就被隔离"。

★ static/ 和 assets/ 必须整体带进去
  前者是前端和 pdf.js，后者是中文字体。漏了分别表现为"白屏"和"中文变方块"——
  都在打包机上复现不出来，只在别人的电脑上出现。
"""

import os

datas = [
    ("static", "static"),
    ("assets", "assets"),
]

# 只排掉确定用不到的东西；宁可多带几 MB，也不要在运行时才发现
# "某个模块被 exclude 了"（那种 ImportError 只在打包版出现，很难查）
excludes = ["tkinter", "turtle", "pydoc", "distutils", "test"]

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDFViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 留个窗口显示端口和"关掉窗口即退出"
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="packaging/app.ico",    # 由 packaging/make_icon.py 生成
    version="exe_version.txt",   # 让 exe 属性里显示名字和版本，而不是"未知"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PDFViewer",
)
