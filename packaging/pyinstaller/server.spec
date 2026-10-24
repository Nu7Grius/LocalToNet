# -*- coding: utf-8 -*-
"""server.spec —— LocalToNet-Server 单文件（one-file）打包配置。

为什么这么配（动手改之前先读这段）：

1. ``hiddenimports=[]``：服务端与 GUI 无关（``localtonet.gui`` 已在 excludes 里），
   全仓无 importlib / __import__ / import_module，静态依赖分析足够，
   不需要手工补任何隐藏导入。
2. ``excludes=['pytest', 'tests', 'localtonet.gui']``：tests 约九千行与 pytest
   全链都不该进发行包；``localtonet.gui`` 给控制台产物再减一层（已确认无引用）。
3. ``datas=[]``：运行时零数据文件——证书、配置、令牌表全部按路径从磁盘读，
   ``logs/`` 与 ``mappings.json`` 运行时生成（相对当前工作目录），
   没有需要塞进 ``_MEIPASS`` 的资源。
4. ``binaries`` 不显式声明：PyInstaller 的 ssl hook 会自动带上 OpenSSL DLL
   （libcrypto/libssl）；若 TLS 冒烟失败再照 docs/packaging.md 的排查表处理。
5. 入口 ``server.py`` 顶层的 ``from config …`` / ``from logging_setup …``
   是仓库根级模块，靠 ``pathex`` 指向仓库根解析。
   onefile 下入口里的 ``sys.path.insert`` 指向 ``_MEIPASS``，无害。
6. ``console=True``：服务端日志要可见。
7. ``version`` 指向 build.sh 渲染出的 ``packaging/_gen/version_info.txt``
   （模板 version_info.txt.in，版本号取自 localtonet/__init__.py）；
   单独跑 pyinstaller 前必须先渲染，否则会因找不到版本文件而失败——
   这是故意的 fail-fast，正常入口是 packaging/build.sh。
"""

block_cipher = None

a = Analysis(
    ['../../server.py'],
    pathex=['../..'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tests', 'localtonet.gui'],
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
    name='LocalToNet-Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../../assets/icon.ico',
    version='../_gen/version_info.txt',
)
