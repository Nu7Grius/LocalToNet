#!/usr/bin/env bash
# build.sh —— LocalToNet 单文件（one-file）发行包一键构建（Git Bash 下执行）
#
# 用法：
#   packaging/build.sh              # 清掉 dist/ 与 build/ 后全量构建（默认）
#   packaging/build.sh --keep       # 保留现有 dist/ 不预清（增量覆盖同名产物）
#   PY=... packaging/build.sh       # 换构建解释器（默认 venv/Scripts/python.exe）
#
# 构建解释器必须满足两条（选错全盘皆输，详见 docs/packaging.md）：
#   1. 带 tkinter——GUI 两个产物要收 tcl/tk，否则在无 Python 机器上起不来；
#   2. Windows x64。默认 venv（3.11.16 / Tk 8.6）与系统 Python312（3.12.3）二选一；
#      WorkBuddy 托管 Python（3.13，无 tkinter）禁止用于构建。
#
# 产物（全部落在 dist/，已 gitignore；版本号唯一出处是 localtonet/__init__.py
# 的 __version__，本脚本读取并拼进产物名，不允许在别处硬编码第二份）：
#   LocalToNet-Server-<ver>.exe      控制台   服务端
#   LocalToNet-Client-<ver>.exe      控制台   客户端
#   LocalToNet-ServerGUI-<ver>.exe   无控制台 管理台
#   LocalToNet-ClientGUI-<ver>.exe   无控制台 客户端界面
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PY:-venv/Scripts/python.exe}"
SPECS=(server client server_gui client_gui)
declare -A PRODUCT=(
  [server]=LocalToNet-Server
  [client]=LocalToNet-Client
  [server_gui]=LocalToNet-ServerGUI
  [client_gui]=LocalToNet-ClientGUI
)

KEEP=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      echo "错误：未知参数 $arg（支持 --keep / -h）" >&2
      exit 2
      ;;
  esac
done

# ---- 0. 构建解释器自检：存在 + 带 tkinter ------------------------------------ #
if [[ ! -f "$PY" ]]; then
  command -v "$PY" >/dev/null 2>&1 || {
    echo "错误：构建解释器 $PY 不存在（默认 venv/Scripts/python.exe；可用 PY=... 覆盖）" >&2
    exit 1
  }
fi
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
  echo "错误：构建解释器 $PY 没有 tkinter——GUI 产物会缺 tcl/tk，在无 Python 机器上起不来。" >&2
  echo "      换 venv/Scripts/python.exe 或系统 Python312 再做构建（见 docs/packaging.md）。" >&2
  exit 1
fi

# ---- 1. 版本号：唯一出处 localtonet/__init__.py ------------------------------ #
VERSION="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' localtonet/__init__.py)"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：无法从 localtonet/__init__.py 读取 X.Y.Z 形式的 __version__（读到：${VERSION:-空}）" >&2
  exit 1
fi
REST="${VERSION#*.}"
MAJOR="${VERSION%%.*}"
MINOR="${REST%%.*}"
PATCH="${REST#*.}"
echo "==> 版本号：$VERSION（$MAJOR.$MINOR.$PATCH，构建解释器 $PY）"

# ---- 2. 渲染 VERSIONINFO 模板 ------------------------------------------------ #
mkdir -p packaging/_gen
sed -e "s/{MAJOR}/$MAJOR/g" -e "s/{MINOR}/$MINOR/g" -e "s/{PATCH}/$PATCH/g" \
  packaging/version_info.txt.in > packaging/_gen/version_info.txt
echo "==> 已渲染 packaging/_gen/version_info.txt"

# ---- 3. 清理（--keep 跳过；两者都已 gitignore） ------------------------------ #
if [[ "$KEEP" -eq 0 ]]; then
  rm -rf dist build
  echo "==> 已清理 dist/ 与 build/"
else
  echo "==> --keep：保留现有 dist/ 与 build/"
fi

# ---- 4. 依次构建四个 spec（onefile 模式写在 spec 里） ------------------------ #
for spec in "${SPECS[@]}"; do
  echo "==> pyinstaller packaging/pyinstaller/${spec}.spec"
  "$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build \
    "packaging/pyinstaller/${spec}.spec"
done

# ---- 5. 重命名为带版本号的产物名 --------------------------------------------- #
for spec in "${SPECS[@]}"; do
  src="dist/${PRODUCT[$spec]}.exe"
  dst="dist/${PRODUCT[$spec]}-${VERSION}.exe"
  [[ -f "$src" ]] || { echo "错误：构建未产出 $src" >&2; exit 1; }
  mv -f "$src" "$dst"
done

# ---- 6. 清单与校验：dist/ 必须恰好 4 个 exe ---------------------------------- #
echo "==> dist/ 清单："
count=0
for f in dist/*.exe; do
  [[ -e "$f" ]] || { echo "错误：dist/ 下没有任何 exe" >&2; exit 1; }
  printf '    %-42s %10d 字节\n' "$(basename "$f")" "$(stat -c '%s' "$f")"
  count=$((count + 1))
done
if [[ "$count" -ne 4 ]]; then
  echo "错误：dist/ 下应有 4 个 exe，实际 $count 个" >&2
  exit 1
fi
echo "==> 完成：4 个单文件产物，版本号均为 $VERSION"
