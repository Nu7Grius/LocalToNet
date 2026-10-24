# -*- coding: utf-8 -*-
"""server_gui.spec —— LocalToNet-ServerGUI 单文件（one-file）打包配置。

为什么这么配（动手改之前先读这段）：

1. ``hiddenimports`` 里的 ``localtonet.gui.server_app`` 是**必须加**的：
   ``localtonet/gui/__init__.py`` 的 ``create_server_app()`` 是**函数内延迟 import**
   （刻意为之——没有 tkinter 的机器也要能 ``import localtonet.gui``），
   PyInstaller 静态分析看不见它，不加就是运行时
   ``RuntimeError: 当前 Python 没有可用的 tkinter`` 或 ModuleNotFoundError。
   同组的 ``tkinter`` / ``tkinter.ttk`` / ``tkinter.messagebox`` 其实会被
   server_app 的静态 import 带进来，列在这里是双保险，并说明"tkinter 三件套
   一个都不能少"——缺 tcl/tk 数据文件的 GUI exe 在无 Python 机器上直接起不来。
2. ``excludes=['pytest', 'tests']``：tests 约九千行与 pytest 全链都不该进发行包。
   注意**不能** exclude ``localtonet.gui``——这个产物就是 GUI；
   也不需要 exclude ``localtonet.gui.app``（客户端界面），静态分析到不了它。
3. ``datas=[]``：运行时零数据文件——证书、配置、令牌表全部按路径从磁盘读，
   ``logs/`` 与 ``mappings.json`` 运行时生成（相对当前工作目录）。
   tkinter 的 tcl/tk 数据文件由 PyInstaller 自带的 tkinter hook 自动收集，
   不在这里手工声明。
4. ``binaries`` 不显式声明：ssl hook 自动带 OpenSSL DLL；Tk 的 DLL 同理由 hook 带。
5. 入口 ``server_gui.py`` 复用 ``server`` 模块的 ``build_parser``/``load_config``
   （``from server import …``），静态可见；根级 ``config``/``logging_setup``
   靠 ``pathex`` 指向仓库根解析。
6. ``console=False``：管理台是窗口程序，不能带控制台黑窗。
7. ``version`` 指向 build.sh 渲染出的 ``packaging/_gen/version_info.txt``；
   单独跑 pyinstaller 前必须先渲染，否则会因找不到版本文件而失败——
   这是故意的 fail-fast，正常入口是 packaging/build.sh。
"""

block_cipher = None

a = Analysis(
    ['../../server_gui.py'],
    pathex=['../..'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'localtonet.gui.server_app',  # 延迟导入，静态分析不可见——必须显式列出
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tests'],
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
    name='LocalToNet-ServerGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../../assets/icon.ico',
    version='../_gen/version_info.txt',
)
