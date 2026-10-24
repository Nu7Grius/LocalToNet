# gui_click.ps1 —— tkinter 窗口冒烟自动化：按标题找窗口、按几何点按钮、截屏、发键
# ============================================================================
# 用途：docs/packaging.md「冒烟验收」里 GUI 两项的驱动工具——
#   1. 验证两个 GUI exe 能起窗口（截图即可目检）；
#   2. 在无头环境里替人点一次「新增映射 → 确定 → 提交映射」，
#      证明打包后的 GUI 完整走通了映射表提交链路。
#
# 原理与坑（都踩过）：
#   * Tk 在 Windows 上每个构件都是一个子窗口，但**类名全是 TkChild、窗口文本为空**
#     （ttk 自己画文本），所以不能按文本定位，只能按几何：
#     工具栏按钮＝最顶一行（按 x 排序，宽 20~400 排除工具条框架）；
#     对话框确定/取消＝最底一行（宽 ≤100 排除按钮框架）；
#     表格＝又高又宽的子窗口里不被其他宽子窗口包含的那个（按 y 排序取第 N 个）。
#   * 默认用 PostMessage 往按钮/表格的窗口句柄直接投递 WM_LBUTTONDOWN/UP，
#     **不经过输入队列**——锁屏（LogonUI 在场）时真实鼠标注入会被系统丢弃，
#     PostMessage 照样送达；要真实鼠标加 -RealMouse（需会话已解锁）。
#   * 截屏用 PrintWindow(PW_RENDERFULLCONTENT)：CopyFromScreen 抓的是屏幕帧缓冲，
#     锁屏/熄屏/GPU 合成时会得到全黑图；PrintWindow 直接问窗口要画面。
#   * 本文件必须带 UTF-8 BOM：PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1，中文会解析失败。
#
# 用法（PowerShell，-ExecutionPolicy Bypass）：
#   gui_click.ps1 -Window "LocalToNet 管理台" -List                  # 列出窗口与全部子窗口（含矩形）
#   gui_click.ps1 -Window "LocalToNet 管理台" -Screenshot shot.png   # 窗口截屏
#   gui_click.ps1 -Window "LocalToNet 管理台" -ClickTopRow 2         # 点工具栏第 3 个按钮（0 起）
#   gui_click.ps1 -Window "新增映射" -ClickBottomRow 0               # 点对话框最底行最左（确定）
#   gui_click.ps1 -Window "LocalToNet 管理台" -ClickTree 1           # 点第 2 棵表格的第 1 行
#   gui_click.ps1 -Window "LocalToNet 管理台" -SendKeys "{TAB}"      # 前置窗口后发键
#
# 注意：-RealMouse 会真实移动并点击，运行前确认没有其他重要窗口抢焦点。
param(
  [Parameter(Mandatory = $true)][string]$Window,   # 顶层窗口标题子串
  [int]$ClickTopRow = -1,                          # 点工具栏（最顶行）第 N 个按钮，0 起；-1 不点
  [int]$ClickBottomRow = -1,                       # 点最底行第 N 个（对话框确定/取消）；-1 不点
  [int]$ClickTree = -1,                            # 点第 N 棵表格（0 起）的第 1 行；-1 不点
  [string]$SendKeys,                               # 发送的按键（WScript.Shell SendKeys 语法）
  [switch]$List,                                   # 只枚举不点击
  [string]$Screenshot,                             # 截屏输出 PNG 路径
  [switch]$RealMouse,                              # 用真实鼠标点击（默认 PostMessage）
  [switch]$Restore,                                # 先把最小化的窗口恢复并强制前台
  [int]$TimeoutSec = 10                            # 等窗口出现的超时
)

Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, int e);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
'@

$WM_LBUTTONDOWN = 0x0201
$WM_LBUTTONUP = 0x0202
$MOUSEEVENTF_LEFTDOWN = 0x0002
$MOUSEEVENTF_LEFTUP = 0x0004

function Get-WinTitle([IntPtr]$h) {
  $sb = New-Object System.Text.StringBuilder 256
  [void][Win32]::GetWindowText($h, $sb, 256)
  return $sb.ToString()
}

function Get-WinClass([IntPtr]$h) {
  $sb = New-Object System.Text.StringBuilder 256
  [void][Win32]::GetClassName($h, $sb, 256)
  return $sb.ToString()
}

function Find-TopWindow([string]$titleSub, [int]$timeoutSec) {
  $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    $script:hits = New-Object System.Collections.ArrayList
    $cb = [Win32+EnumProc] {
      param($h, $l)
      if ([Win32]::IsWindowVisible($h)) {
        $t = Get-WinTitle $h
        if ($t -and $t.Contains($titleSub)) { [void]$script:hits.Add($h) }
      }
      return $true
    }
    [void][Win32]::EnumWindows($cb, [IntPtr]::Zero)
    if ($script:hits.Count -gt 0) { return $script:hits[0] }
    Start-Sleep -Milliseconds 500
  }
  return [IntPtr]::Zero
}

function Get-Rect([IntPtr]$h) {
  $r = New-Object Win32+RECT
  [void][Win32]::GetWindowRect($h, [ref]$r)
  return $r
}

function Restore-AndFocus([IntPtr]$h) {
  # 两个都踩过：
  #   1. 最小化（IsIconic）的窗口坐标在 (-32000,-32000)，点它等于点别处；
  #   2. **前台激活 ≠ Z 序置顶**：用户中途点过 Chrome 后，Chrome 升到管理台上面，
  #      此时 GetForegroundWindow 仍是管理台，但 WindowFromPoint  hit-test 返回的是
  #      Chrome——真实鼠标全点到 Chrome 上。必须每次都 BringWindowToTop/SetWindowPos
  #      把窗口升到 Z 序顶部，光 SetForegroundWindow 不够。
  if ([Win32]::IsIconic($h)) {
    [void][Win32]::ShowWindow($h, 9)  # 9 = SW_RESTORE
    Start-Sleep -Milliseconds 400
    Write-Host "窗口原已最小化，已 SW_RESTORE 恢复"
  }
  $fg = [Win32]::GetForegroundWindow()
  $fgPid = 0
  $fgTid = [Win32]::GetWindowThreadProcessId($fg, [ref]$fgPid)
  $myTid = [Win32]::GetCurrentThreadId()
  # 只许前台进程抢焦点：先挂到前台线程的输入队列，操作完再摘
  [void][Win32]::AttachThreadInput($fgTid, $myTid, $true)
  [void][Win32]::BringWindowToTop($h)
  [void][Win32]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, 0, 0, 0x0003)  # HWND_TOP|SWP_NOSIZE|SWP_NOMOVE
  [void][Win32]::SetForegroundWindow($h)
  [void][Win32]::AttachThreadInput($fgTid, $myTid, $false)
  Start-Sleep -Milliseconds 400
  $fg2 = [Win32]::GetForegroundWindow()
  Write-Host ("置顶+前台：{0}" -f $(if ($fg2 -eq $h) { "成功" } else { "激活失败（前台=$fg2）" }))
}

function Get-Descendants([IntPtr]$parent) {
  # BFS 收集全部后代；Tk 有一个 wrapper 窗口会让枚举出现重复句柄，按句柄去重。
  $all = @{}
  $queue = New-Object System.Collections.Queue
  $queue.Enqueue($parent)
  while ($queue.Count -gt 0) {
    $node = $queue.Dequeue()
    $script:kids = New-Object System.Collections.ArrayList
    $cb = [Win32+EnumProc] { param($h, $l) [void]$script:kids.Add($h); return $true }
    [void][Win32]::EnumChildWindows($node, $cb, [IntPtr]::Zero)
    foreach ($k in $script:kids) {
      if (-not $all.ContainsKey([int64]$k)) {
        $all[[int64]$k] = $k
        $queue.Enqueue($k)
      }
    }
  }
  return $all.Values
}

function Click-Hwnd([IntPtr]$h) {
  $r = Get-Rect $h
  if ($RealMouse) {
    $x = [int](($r.Left + $r.Right) / 2)
    $y = [int](($r.Top + $r.Bottom) / 2)
    [void][Win32]::SetCursorPos($x, $y)
    Start-Sleep -Milliseconds 100
    # mouse_event 的 dx/dy 非 ABSOLUTE 时是相对位移：必须先 SetCursorPos 到位，再传 0,0
    [void][Win32]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    Start-Sleep -Milliseconds 80
    [void][Win32]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
  } else {
    # PostMessage：lParam 是窗口客户区坐标（相对窗口左上角），锁屏也能送达
    $x = [int](($r.Right - $r.Left) / 2)
    $y = [int](($r.Bottom - $r.Top) / 2)
    $lp = [IntPtr](($y -shl 16) -bor ($x -band 0xFFFF))
    [void][Win32]::PostMessage($h, $WM_LBUTTONDOWN, [IntPtr]1, $lp)
    Start-Sleep -Milliseconds 60
    [void][Win32]::PostMessage($h, $WM_LBUTTONUP, [IntPtr]0, $lp)
  }
}

function Click-Point([IntPtr]$h, [int]$x, [int]$y) {
  # 在指定子窗口的客户区坐标 (x,y) 上点击
  if ($RealMouse) {
    $r = Get-Rect $h
    [void][Win32]::SetCursorPos($r.Left + $x, $r.Top + $y)
    Start-Sleep -Milliseconds 100
    [void][Win32]::mouse_event($MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    Start-Sleep -Milliseconds 80
    [void][Win32]::mouse_event($MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
  } else {
    $lp = [IntPtr](($y -shl 16) -bor ($x -band 0xFFFF))
    [void][Win32]::PostMessage($h, $WM_LBUTTONDOWN, [IntPtr]1, $lp)
    Start-Sleep -Milliseconds 60
    [void][Win32]::PostMessage($h, $WM_LBUTTONUP, [IntPtr]0, $lp)
  }
}

function Save-WindowShot([IntPtr]$h, [string]$path) {
  # 用 PrintWindow 而不是 CopyFromScreen：后者抓的是屏幕帧缓冲，
  # 熄屏/多显示器/GPU 合成时会得到全黑图；PrintWindow 直接问窗口要画面。
  $r = Get-Rect $h
  $w = $r.Right - $r.Left
  $ht = $r.Bottom - $r.Top
  $bmp = New-Object System.Drawing.Bitmap $w, $ht
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $hdc = $g.GetHdc()
  [void][Win32]::PrintWindow($h, $hdc, 2)  # 2 = PW_RENDERFULLCONTENT
  $g.ReleaseHdc($hdc)
  $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose()
  $bmp.Dispose()
}

# ---- 主流程 ------------------------------------------------------------------ #
$hwnd = Find-TopWindow $Window $TimeoutSec
if ($hwnd -eq [IntPtr]::Zero) {
  Write-Error "在 $TimeoutSec 秒内找不到标题包含 '$Window' 的可见窗口"
  exit 1
}
Write-Host ("窗口 hwnd={0} 标题=[{1}]" -f $hwnd, (Get-WinTitle $hwnd))

if ($Restore -or $RealMouse) { Restore-AndFocus $hwnd }

$children = Get-Descendants $hwnd | Where-Object {
  $r = Get-Rect $_
  ($r.Right - $r.Left) -gt 0 -and ($r.Bottom - $r.Top) -gt 0
}

if ($List) {
  $children | Sort-Object { (Get-Rect $_).Top }, { (Get-Rect $_).Left } | ForEach-Object {
    $r = Get-Rect $_
    $cls = Get-WinClass $_
    $txt = Get-WinTitle $_
    Write-Host ("  h={0} class={1} rect=({2},{3})-({4},{5}) text=[{6}]" -f $_, $cls, $r.Left, $r.Top, $r.Right, $r.Bottom, $txt)
  }
}

if ($Screenshot) {
  Save-WindowShot $hwnd $Screenshot
  Write-Host "截屏已存 $Screenshot"
}

if ($ClickTopRow -ge 0) {
  # 最顶一行：工具栏按钮。宽 20~400：挡住 2px 分隔符和 1080px 的工具条框架，
  # 再按 x 排序（0=启动服务端 1=停止服务端 2=新增映射 3=复制 4=删除 5=提交映射 6=放弃修改）。
  $minY = ($children | ForEach-Object { $r = Get-Rect $_; ($r.Top + $r.Bottom) / 2 } | Measure-Object -Minimum).Minimum
  $row = $children | Where-Object {
    $r = Get-Rect $_
    $w = $r.Right - $r.Left
    $w -ge 20 -and $w -le 400 -and [math]::Abs((($r.Top + $r.Bottom) / 2) - $minY) -lt 6
  } | Sort-Object { (Get-Rect $_).Left }
  if ($ClickTopRow -ge $row.Count) { Write-Error "顶行只有 $($row.Count) 个按钮，点不了第 $ClickTopRow 个"; exit 1 }
  Click-Hwnd $row[$ClickTopRow]
  Write-Host "已点击顶行第 $ClickTopRow 个按钮（共 $($row.Count) 个）"
}

if ($ClickBottomRow -ge 0) {
  # 最底一行：对话框的确定/取消/是/否。宽 ≤100 排除包住它们的按钮框架。
  $maxY = ($children | ForEach-Object { $r = Get-Rect $_; ($r.Top + $r.Bottom) / 2 } | Measure-Object -Maximum).Maximum
  $row = $children | Where-Object {
    $r = Get-Rect $_
    ($r.Right - $r.Left) -le 100 -and [math]::Abs((($r.Top + $r.Bottom) / 2) - $maxY) -lt 6
  } | Sort-Object { (Get-Rect $_).Left }
  if ($ClickBottomRow -ge $row.Count) { Write-Error "底行只有 $($row.Count) 个按钮，点不了第 $ClickBottomRow 个"; exit 1 }
  Click-Hwnd $row[$ClickBottomRow]
  Write-Host "已点击底行第 $ClickBottomRow 个按钮（共 $($row.Count) 个）"
}

if ($ClickTree -ge 0) {
  # 表格：又高又宽的子窗口里，**不被其他宽子窗口包含**的才是真正的 Treeview
  # （LabelFrame 包着 Treeview，直接按大小筛会把框架也算进来）。
  $wide = $children | Where-Object {
    $r = Get-Rect $_
    ($r.Right - $r.Left) -gt 900 -and ($r.Bottom - $r.Top) -gt 100
  }
  $trees = @()
  foreach ($c in $wide) {
    $rc = Get-Rect $c
    $containsOther = $false
    foreach ($o in $wide) {
      if ($o -eq $c) { continue }
      $ro = Get-Rect $o
      if ($ro.Left -ge $rc.Left -and $ro.Right -le $rc.Right -and $ro.Top -ge $rc.Top -and $ro.Bottom -le $rc.Bottom) {
        $containsOther = $true
        break
      }
    }
    if (-not $containsOther) { $trees += $c }
  }
  $trees = $trees | Sort-Object { (Get-Rect $_).Top }
  if ($ClickTree -ge $trees.Count) { Write-Error "只有 $($trees.Count) 棵表格，点不了第 $ClickTree 个"; exit 1 }
  $t = Get-Rect $trees[$ClickTree]
  Click-Point $trees[$ClickTree] 80 35
  Write-Host "已点击第 $ClickTree 棵表格的第一行（rect=($($t.Left),$($t.Top))-$($t.Right),$($t.Bottom))）"
}

if ($SendKeys) {
  [void][Win32]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds 300
  $ws = New-Object -ComObject WScript.Shell
  $ws.SendKeys($SendKeys)
  Write-Host "已发送按键：$SendKeys"
}
