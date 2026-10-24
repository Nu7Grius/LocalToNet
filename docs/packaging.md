# 打包（PyInstaller 单文件发行包）

本文记录 LocalToNet 四个 Windows 单文件（one-file）产物的**完整复现步骤**：
解释器怎么选、四个 spec 为什么这么写、冒烟验收怎么跑、踩过哪些坑。
面向要自己构建或改打包配置的人；只要下载使用请读 README「免安装发行包」。

- 版本号唯一出处：`localtonet/__init__.py` 的 `__version__`（当前 `0.1.0`）。
  四个产物名与 VERSIONINFO 的版本号都从它读取，**别处不允许有第二份硬编码**。
- 运行时零第三方依赖的红线不动：PyInstaller 只进 `requirements-dev.txt`，
  注释标明"仅构建期"。
- 本轮范围：Windows x64、one-file、四个 exe；**不做** onedir、macOS/Linux、
  NSIS 安装包、代码签名。

## 1. 构建环境（选错解释器全盘皆输）

PyInstaller 用**哪个解释器**构建，产物就带哪个解释器的标准库与扩展。
GUI 两个产物需要 tkinter（含 tcl/tk 数据文件），所以构建解释器**必须带 tkinter**：

| 候选 | 版本 | tkinter | 能否用于构建 |
| --- | --- | --- | --- |
| 项目 `venv\Scripts\python.exe` | 3.11.16 / Tk 8.6 / OpenSSL 3.5.7 | 有 | **首选**（与 3.11+ 目标一致） |
| 系统 `…\Python312\python.exe` | 3.12.3 / Tk 8.6 | 有 | 备选 |
| WorkBuddy 托管 Python（`envs\default`） | 3.13.14 | **无** | **禁止**——打出来的 GUI 缺 tcl/tk，无 Python 机器上直接起不来 |

测试解释器另算（不需要 tkinter）：WorkBuddy 托管 Python 跑 `pytest` 即可。

```bash
# 1) 装 PyInstaller（只进 venv，仅构建期）
venv/Scripts/python.exe -m pip install pyinstaller     # 实测 6.22.3
venv/Scripts/pyinstaller --version                      # 有输出即装好
venv/Scripts/python.exe -c "import tkinter; print('tkinter', tkinter.TkVersion)"

# 2) pin 进 requirements-dev.txt（构建期依赖，与 pytest 并列）
```

`requirements-dev.txt` 里对应的一行（版本号以实际安装为准）：

```
pyinstaller==6.22.3   # 仅构建期：打单文件发行包用，运行时零依赖红线不动
```

## 2. 版本与图标资源

### 图标 `assets/icon.ico`

`packaging/make_icon.py` 用**纯标准库**生成 256×256 ICO：手写 ICO 容器
（6 字节头 + 16 字节条目 + 内嵌 zlib 压缩 PNG），图案是深色圆角方块 + 浅色圆环 +
数据包圆点的**占位设计**，后续可人工替换（换图只需覆盖 `assets/icon.ico`，
四个 spec 都引用同一份）。

```bash
venv/Scripts/python.exe packaging/make_icon.py
# 验证：文件头 4 字节为 00 00 01 00（ICO 魔数 + 1 个条目）
```

### VERSIONINFO `packaging/version_info.txt.in`

模板里版本三元组用 `{MAJOR}.{MINOR}.{PATCH}` 占位，公司名/产品名/描述用
LocalToNet 相关值，语言 `040904B0`。构建时由 `build.sh` 用 sed 渲染成
`packaging/_gen/version_info.txt`（生成物，不入库）。

## 3. 四个 spec（`packaging/pyinstaller/`）

| spec | 入口 | console | hiddenimports | excludes |
| --- | --- | --- | --- | --- |
| `server.spec` | `../../server.py` | True | `[]`（无 GUI 依赖） | `localtonet.gui`、`pytest`、`tests` |
| `client.spec` | `../../client.py` | True | `[]`（无 GUI 依赖） | `localtonet.gui`、`pytest`、`tests` |
| `server_gui.spec` | `../../server_gui.py` | False | 见下 | `pytest`、`tests` |
| `client_gui.spec` | `../../gui.py` | False | 见下 | `pytest`、`tests` |

**关键约定（每个 spec 头部也有注释，改之前先读）：**

1. **hidden imports 为什么必须加。** `localtonet/gui/__init__.py` 的
   `create_app()` / `create_server_app()` 是**函数内延迟 import**
   （刻意为之——没有 tkinter 的机器也要能 `import localtonet.gui`），
   PyInstaller 静态分析看不见。GUI 两个 spec 必须显式列出：
   ```python
   hiddenimports=[
       'localtonet.gui.server_app',   # 或 'localtonet.gui.app'（client_gui.spec）
       'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
   ]
   ```
   后三个其实会被 app 模块的静态 import 带进来，列出来是双保险，
   同时钉住"tkinter 三件套一个都不能少"。
2. **`datas=[]`。** 运行时零数据文件：证书、配置、令牌表全部按路径从磁盘读；
   `logs/` 与 `mappings.json` 运行时生成（相对 cwd）。tkinter 的 tcl/tk 数据文件
   由 PyInstaller 自带的 tkinter hook 自动收集，不手工声明。
3. **`binaries` 不显式声明。** ssl hook 自动带 OpenSSL DLL；Tk 的 DLL 同理由 hook 带。
   （若换 PyInstaller 版本后 TLS 冒烟失败，再考虑在 spec 里显式带 libcrypto/libssl。）
4. **`excludes`。** server/client 两个 spec exclude `localtonet.gui`（减重，
   已确认无引用）；四个 spec 都 exclude `pytest`、`tests`——tests 约九千行与
   pytest 全链都不该进发行包（不收会直接胖 40MB+）。
5. **路径相对 spec 文件所在目录解析。** spec 在 `packaging/pyinstaller/` 下，
   所以入口写 `../../server.py`、`pathex=['../..']`、`icon='../../assets/icon.ico'`、
   `version='../_gen/version_info.txt'`（`_gen` 在 `packaging/` 下，比仓库根少一层）。
   **这层目录差错过两次**，症状是 "script not found" / 找不到版本文件。
6. **`version` 指向渲染后的 `packaging/_gen/version_info.txt`。** 单独跑
   pyinstaller 前必须先渲染，否则因找不到版本文件而失败——这是故意的 fail-fast，
   正常入口是 `packaging/build.sh`。

## 4. 一键构建 `packaging/build.sh`

```bash
bash packaging/build.sh            # 清掉 dist/ 与 build/ 后全量构建（默认）
bash packaging/build.sh --keep     # 保留现有 dist/ 不预清
PY=... packaging/build.sh          # 换构建解释器（默认 venv/Scripts/python.exe）
```

脚本做的事（按序）：

1. **解释器自检**：存在 + `import tkinter` 成功，否则直接退出并提示换解释器；
2. **读版本号**：从 `localtonet/__init__.py` 正则取 `__version__`，校验 X.Y.Z；
3. **渲染 VERSIONINFO**：`version_info.txt.in` → `packaging/_gen/version_info.txt`；
4. **清理**：`rm -rf dist build`（`--keep` 跳过；两者都已 gitignore）；
5. **依次构建**：`pyinstaller --noconfirm --clean` 四个 spec（onefile 写在 spec 里）；
6. **重命名**：`dist/LocalToNet-Server.exe` → `dist/LocalToNet-Server-<ver>.exe` 等；
7. **校验 + 清单**：dist/ 必须恰好 4 个 exe，打印文件名与字节数。

实测产物（0.1.0，PyInstaller 6.22.3，venv 3.11.16）：

| 产物 | 字节数 | 约 |
| --- | --- | --- |
| LocalToNet-Server-0.1.0.exe | 9,840,670 | 9.4 MB |
| LocalToNet-Client-0.1.0.exe | 9,782,826 | 9.3 MB |
| LocalToNet-ServerGUI-0.1.0.exe | 12,975,047 | 12.4 MB |
| LocalToNet-ClientGUI-0.1.0.exe | 12,971,734 | 12.4 MB |

验收线：控制台 < 15MB、GUI < 25MB、冷启动到可交互 < 5 秒（实测服务端冷启动到
控制端口可连 **642ms**）。

**绝不出现在 dist/ 的东西**（硬约束）：`config.json`（演示配置
`auth.enabled=false` 是测试钉死的值，不是生产模板）、`tokens.json`、
`client.prod.json`、`file_server.secret`、`*.token`、`tests/` 下任何证书密钥；
`共享服务面板.pyw` / `启动共享服务.bat` / `examples/` 是运维件与示例，保持源码形态。

## 5. 冒烟验收（2026-10-24 全量跑过的记录）

以下每条都在**无 Python 的干净环境**（`env -i`，PATH 只留 System32）里
对 `dist/` 产物实测通过。复现脚本没有随仓库提交（用到本机路径与临时目录），
按本文命令即可重跑。

1. **干净环境启动 + `--help`**：四个 exe 全部 exit=0，输出正常。
   注意中文 metavar 在 GBK 控制台按系统代码页显示——看着"乱码"只是
   Git Bash 的 UTF-8 终端在显示 GBK 字节，日志文件本身是 UTF-8。
   中文 Windows 上直接看或 `chcp 65001` 都正常。
2. **E2E 明文转发**：`python -m http.server 18000` 起本地后端 →
   `Server exe --no-auth` → `Client exe --server 127.0.0.1:7000 --local-ports 18000` →
   `curl http://127.0.0.1:9028/` 得 **200**；停掉 client 后同端口得
   **502 No client online**；从未映射过的端口（如 9099）**连接被拒**
   （服务端根本不会在它上面监听——见下面第 6 条的语义纠正）。
3. **令牌鉴权**：临时 `tokens.json`（测试令牌，非生产）+ `--auth-file` 重放第 2 条：
   错令牌注册被拒（403，服务端日志与 `REGISTRATION_REJECTED` 事件可观测），
   正确令牌以身份 `smoke-tester` 上线。
4. **TLS**：用 `tests/certs/` 现成夹具写最小 JSON（`tls.enabled=true` + cert/key），
   `Server exe -c 该JSON` 启动，客户端带 `--tls-ca/--tls-cert/--tls-key` 上线，
   访客侧 `curl --cacert ca.pem https://127.0.0.1:9028/` 得 **200（TLSv1.3）**；
   明文访问被拒、无 CA 连不上、mTLS 正常上线。
   *Windows curl 的坑*：schannel 会做吊销检查，测试 CA 没有 CRL，
   报 "upstream connect failed" 一类错误——加 `--ssl-revoke-best-effort` 即可，
   与服务端无关。
5. **GUI**：两个 GUI exe 无控制台黑窗、窗口正常，并各完成一次
   「新增映射 → 确定 → 提交映射」全链路：
   - 管理台：新增 9000→18000，日志「映射提交成功：新增 9000」，
     `curl :9000` 得 200；
   - 客户端：自动连接并认领 18000，新增 9001→18000，提交后 `curl :9001` 得 200。
   GUI 自动化工具在 `packaging/smoke/gui_click.ps1`（按几何定位 tkinter 构件，
   见下）。
6. **访客错误语义（纠正任务书的预期）**：**"未映射端口得 404"不成立**。
   源码语义（`server/core.py` 访客处理函数开头）：端口没有映射规则 → 404，
   但注释明确它**只在"连接已建立、映射紧接着被摘掉"的竞态窗口里出现**；
   而**从未配置过的端口服务端根本不监听**，访问是连接被拒。
   仓库自己的测试也钉死了这两点：`test_404_when_port_has_no_mapping_rule`
   （用 in-process 中继绕开监听器才能确定性复现 404）与
   `test_unmapped_port_is_not_listened_at_all`（断言未映射端口访问抛 OSError）。
   打包产物行为与源码完全一致（200 / 502 / 拒连均已实测）。
7. **one-file 行为（`%TEMP%` 的 `_MEIxxxxxx`）**：受控对比实测——
   正常退出（`--help` 跑 3 次）：退出后新增 _MEI 目录 **0 个**；
   运行中：恰好 **1 个**；强制 kill 后：目录**残留**（bootloader 来不及清理，
   正常现象，重启或手工删即可）。**绝不**把配置/令牌/映射表/日志放进
   `sys._MEIPASS`——那目录退出即删。
8. **体积与冷启动**：见第 4 节表格；冷启动 642ms。

### GUI 冒烟自动化（`packaging/smoke/gui_click.ps1`）

Tk 在 Windows 上每个构件都是子窗口，但**类名全是 TkChild、窗口文本为空**
（ttk 自己画文本），没法按文本定位，只能按几何：工具栏按钮＝最顶一行、
对话框确定/取消＝最底一行、表格＝不被其他宽子窗口包含的那个。
脚本用 PostMessage / 真实鼠标两种模式点击，用 PrintWindow 截屏。

踩过的坑（都写进了脚本注释）：

- **前台激活 ≠ Z 序置顶**：用户中途点过 Chrome 后，Chrome 升到管理台上面，
  此时 `GetForegroundWindow` 仍是管理台，但鼠标点击全落在 Chrome 上。
  必须先 `BringWindowToTop` / `SetWindowPos(HWND_TOP)` 再点；
  仅 `SetForegroundWindow` 不够。
- **最小化窗口的坐标在 (-32000,-32000)**，点它等于点别处；先 `SW_RESTORE`。
- 锁屏（LogonUI 在场）时真实鼠标注入被系统丢弃，PostMessage 才能送达。
- `mouse_event` 的 dx/dy 非 ABSOLUTE 模式下是**相对位移**：先 `SetCursorPos`
  到位，再传 0,0。
- PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1，中文脚本必须带 UTF-8 BOM。

### 已知事项（非打包问题，供排查参考）

客户端 GUI 快速连点「断开」再「连接」时，可能卡在重连循环
（日志显示"1.0s 后第 1 次重连"但重试不触发）；**重启进程即恢复**，
全新进程自动连接、认领端口一切正常。这是源码层的行为，与打包无关，
留待后续单独排查。

## 6. 故障排查表

| 症状 | 根因 | 处置 |
| --- | --- | --- |
| `ModuleNotFoundError: localtonet.gui.app` | 延迟导入没进包 | 检查 GUI spec 的 hiddenimports |
| spec 报 script / 版本文件 not found | spec 相对路径少一层目录 | 入口/pathex/icon 是 `../../`，version 是 `../_gen/` |
| `ssl.SSLError` / DLL 加载失败 | OpenSSL DLL 未打包 | 先确认 PyInstaller 版本；仍不行在 spec binaries 里显式带 libcrypto/libssl |
| GUI exe 起不来、源码能跑 | 构建解释器没有 tkinter | 换 venv / Python312 重新构建 |
| "Failed to execute script xxx" | 分析缺失或 bootloader 问题 | 临时改 onedir 构建隔离：onedir 正常＝onefile 特有参数/杀软问题 |
| 杀软报毒 | PyInstaller 产物常见误报 | README 已说明；本轮不做签名 |
| 找不到配置文件 | cwd 约定没说清 | 用绝对路径传参；README「免安装发行包」有说明 |
| `_MEI` 目录残留 | 进程被 kill/崩溃 | 正常现象；调试可用 `--debug` 关清理 |
| 中文日志乱码 | 控制台 GBK 代码页 | `chcp 65001` 或直接看 UTF-8 日志文件 |
| 体积异常大（>40MB） | 误收 tests/pytest | 检查 excludes |
| 冷启动 >10 秒 | 杀软扫描 _MEI | 加白名单 |
| Windows curl 走 TLS 报吊销错误 | schannel 查 CRL，测试 CA 没有 | `--ssl-revoke-best-effort` |
| curl 走了系统代理（upstream connect failed） | Clash 等代理环境变量 | 用 `no_proxy` 环境变量或 `--noproxy` 列表 |
| 8000 端口被占用 | 本机文件共享服务在跑 | 冒烟换 18000 等空闲端口 |
| GUI 自动化点了没反应 | Z 序/最小化/锁屏 | 见第 5 节 GUI 自动化的坑 |

## 7. 改动清单与红线

本次打包层新增/改动（**运行时源码零改动**）：

```
packaging/make_icon.py          图标生成（纯标准库）
packaging/version_info.txt.in   VERSIONINFO 模板
packaging/build.sh              一键构建脚本
packaging/pyinstaller/*.spec    四个打包配置
packaging/smoke/gui_click.ps1   GUI 冒烟自动化
assets/icon.ico                 生成的占位图标
requirements-dev.txt            +pyinstaller pin（仅构建期）
README.md                       +「免安装发行包」节、目录结构补 packaging/ 与 assets/
docs/packaging.md               本文
```

`dist/` 与 `build/` 已 gitignore，是本地产物不进库；`packaging/_gen/` 是渲染
生成物（每次构建重新生成）。测试基线：打包前 456 项全绿（61.62s），
打包后 456 项全绿（67.50s）——两次都必须绿才算过。
