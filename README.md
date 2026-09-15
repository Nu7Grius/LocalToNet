# LocalToNet · 内网穿透工具

一个用 Python 标准库 `asyncio` 实现的内网穿透工具：把内网服务暴露到公网，
不需要公网 IP、不需要改路由器、不需要装任何第三方依赖。

需求来源与设计依据见仓库内的 [`内网穿透工具-代码分析.md`](内网穿透工具-代码分析.md)。

---

## 特性

- **三通道分离**：控制通道传指令、数据通道传字节、访客端口收流量，互不阻塞
- **防粘包协议**：4 字节大端长度头 + JSON，正确处理粘包与半包
- **多客户端端口独占**：一个客户端认领的端口，流量只会派给它
- **动态映射**：运行期增删映射端口，服务端即时停/起监听
- **图形界面（可选）**：`gui.py` 可视化编辑映射表，提交即生效，不必改 JSON、不必重启进程
- **双保险保活**：客户端心跳探测 + 服务端失联看门狗
- **指数退避重连**：1s → 2s → 4s → … → 60s 封顶，成功即归零
- **明确错误语义**：404 / 502 兜底，绝不留下"空回复"
- **可选共享令牌鉴权**：一行 `--token` 开启；令牌错了立刻停手（403），容量满了继续重试（503）
- **带宽限流**：按客户端、按方向（上行/下行）独立限速，令牌桶平滑而非"每秒硬切"
- **并发配额**：限制单客户端同时在途的转发数，超了直接回 `429`，不排队、不拖垮服务端
- **映射持久化**：`--mapping-store file` 把映射表落到 JSON，重启服务端不再回到配置文件的状态
- **传输加密（TLS）**：控制通道与数据通道可选走 TLS（默认关闭＝明文），令牌不再以明文出现在线路上；
  支持双向认证（mTLS）作为可选纵深防御
- **访客端口 TLS 终止**：公网入口那一跳也能逐端口开 TLS（`mapping[].tls`，默认明文）；
  证书独立、绝不回落，开关可热切
- **零运行时依赖**：纯标准库（界面用自带的 tkinter，加密用自带的 `ssl`），Python 3.11+（开发环境用 3.13）

## 架构

```
┌─────────────┐        ┌──────────────────────────────┐        ┌──────────────────┐
│  公网访客    │        │        公网服务器             │        │   内网客户端      │
│ 浏览器/curl  │        │         server.py            │        │ client.py/gui.py │
└──────┬──────┘        └───────────────┬──────────────┘        └────────┬─────────┘
       │                               │                                │
       │  9028（访客端口，按映射）       │                                │
       ├──────────────────────────────►│  _handle_visitor               │
       │                               │                                │
       │                               │◄──── 7000 控制通道（长连接）────►│ _handle_control
       │                               │      register / ping / new_conn │
       │                               │                                │
       │                               │◄──── 7001 数据通道（每请求一条）─►│ DataChannelForwarder
       │                               │      register(conn_id)          │
       │                               │                                │
       │                               │                                ├──► 127.0.0.1:8000
```

| 端口 | 通道 | 生命周期 | 承载内容 |
| --- | --- | --- | --- |
| 7000 | 控制 | 长期存活 | 小 JSON 指令：注册、心跳、`new_conn`、映射管理 |
| 7001 | 数据 | **每个请求一条** | 纯字节流，用 `conn_id` 与访客连接配对 |
| 9028 等 | 访客 | 每个映射端口一个监听 | 公网入站流量 |

把控制与数据拆开的意义：控制通道上跑的是"开一条转发"这类必须及时送达的指令。
若所有流量挤在一条 TCP 上，大文件传输的写缓冲堆积会把指令堵在后面，
表现为"打开新页面要等上一个下载结束"。

## 快速开始

三个终端，都从仓库根目录执行：

```bash
# 终端 1：内网后端（演示服务，实际使用时换成你自己的服务）
python examples/demo_backend.py --port 8000

# 终端 2：服务端（公网机器上运行）
python server.py

# 终端 3（二选一）：
python client.py --server 127.0.0.1 --local-ports 8000   # 命令行
python gui.py  --server 127.0.0.1 --local-ports 8000     # 图形界面
```

然后访问 `http://127.0.0.1:9028/`，请求就会被转发到内网的 `8000` 端口：

```bash
curl "http://127.0.0.1:9028/echo?msg=hello"
# → hello
```

命令行参数：

```bash
python server.py --mapping 9028:8000 --mapping 9030:8080 --log-level DEBUG
python client.py --server 1.2.3.4:7000 --local-ports 8000,8080 --client-id my-pc
```

`gui.py` 的参数与 `client.py` **完全一致**（同一套解析函数），另外多一个
`--no-autostart`：打开界面但不自动连接。界面里能做的事见下面「图形界面」一节。

配置优先级：**默认值 < JSON 文件 < 环境变量（`LOCALTONET_` 前缀）< 命令行参数**。

## 开启鉴权

默认 `auth.enabled=false`，开箱即用的本地演示不需要任何令牌。公网部署建议开启**共享令牌**：
服务端校验客户端在 `register_client` 帧里带的 `token`，不匹配就回 `403` 并拒掉连接。

| 位置 | 方式 | 示例 |
| --- | --- | --- |
| 服务端 | 命令行 | `python server.py --token s3cret` |
| 服务端 | 环境变量 | `LOCALTONET_AUTH_TOKEN=s3cret python server.py` |
| 服务端 | 配置文件 | `config.json` 的 `"auth": { "enabled": true, "token": "s3cret" }` |
| 客户端 | 命令行 | `python client.py --server 1.2.3.4:7000 --local-ports 8000 --token s3cret` |
| 客户端 | 环境变量 | `LOCALTONET_AUTH_TOKEN=s3cret python client.py …` |
| 客户端 | 配置文件 | `client.json` 的 `"auth_token": "s3cret"` |

`gui.py` 的参数与 `client.py` **完全一致**，`--token` 在界面上同样生效。

> 服务端 `--token` 与客户端 `--token` 填的是**同一个共享密钥**，两端差一个字都连不上。
> 本地演示临时要关掉配置文件里开着的鉴权，用 `--no-auth` 覆盖即可（命令行优先级最高）。
> 二者互斥，同时给出会被参数解析拦下并以退出码 `2` 结束。

### 忘了配客户端令牌会怎样

症状很明确，不会含糊成"连不上，原因不明"：

1. 服务端日志出现 `拒绝客户端 test-xxx（127.0.0.1:xxxxx）：客户端 … 提供的 token 不合法`；
2. 客户端收到 `code=403`，状态直接变 `stopped` 并**停止重试**，
   日志写 `注册被永久拒绝（[403] …），停止重试`，事件总线发一条 `CONTROL_LOST(fatal=True)`
   （GUI 的事件日志里能看到）。

这是故意的：凭据不对，重试一万次结果也一样，每 60 秒撞一次墙只会刷满日志、掩盖真正的问题。

| 拒绝原因 | `code` | 客户端行为 |
| --- | --- | --- |
| 令牌错误 / 根本没带令牌 | `403` | **永久失败** → 停止重试，状态 `stopped` |
| 在线客户端数已达 `limits.max_clients` | `503` | **暂时失败** → 继续指数退避重试 |
| `client_id` 非法 / 端口列表非法 | `400` | 继续退避重试（改配置就能好） |

> ⚠️ 令牌目前是**明文**放在 `register_client` 帧里走 TCP，`client.json` 里也是明文落盘。
> 公网部署请至少开启「传输加密（TLS）」（控制+数据两跳），需要访客侧也加密时再按
> 「访客端口 TLS」逐端口打开；置于 Nginx / Caddy 之后同样可行。

## 传输加密（TLS）

控制通道与数据通道可以走 TLS，令牌就不会再以明文出现在公网线路上。**默认完全关闭**——
不配任何 TLS 字段时行为与之前一字不差（明文），本地 demo 不受影响。

三跳里有两跳可以加密，而它们是**各自独立的开关**，任意组合：

```
访客 --① 可选 TLS--> 服务端 --② 可选 TLS--> 客户端 --③ 永远明文--> 内网后端
```

- **① 访客端口 ↔ 服务端**（本轮新增）：TLS 终止在服务端，**逐端口**可选，默认明文。
  见下节「访客端口 TLS（per-port 开关）」。
- **② 控制通道 + 数据通道**（上一轮落地）：令牌不再以明文出现在这段线路上。**默认关闭**——
  不配任何 TLS 字段时行为与之前一字不差（明文），本地 demo 不受影响。
- **③ 客户端 → 内网后端**：走本机/内网，**永远明文**（给内网那一跳套 TLS 是自我感动，
  还会堵死"后端是明文 HTTP"这个绝大多数场景）。

> 三跳之外还有一件 TLS 管不了的事：`client.json` 里的 `auth_token` 明文落盘、`--token` 进进程列表。
> 生产环境优先用环境变量或受限权限的配置文件。

### 快速开始

先用自签证书（一次性测试材料见 `tests/certs/`，绝不可用于生产）：

```bash
# 服务端：给证书与私钥，即隐式开启 TLS
python server.py --tls-cert tests/certs/server.pem --tls-key tests/certs/server.key

# 客户端：给可信 CA（服务端是自签证书时必填）
python client.py --server 127.0.0.1 --local-ports 8000 --tls-ca tests/certs/ca.pem
```

配置文件方式（服务端 `config.json` / 客户端 `client.json`）：

```jsonc
// 服务端
{ "tls": { "enabled": true, "cert": "server.pem", "key": "server.key" } }
// 客户端
{ "tls": { "enabled": true, "ca": "ca.pem" } }
```

### 字段

**服务端 `tls`**：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否开启 TLS | `false` |
| `cert` / `key` | 证书链 / 私钥（PEM） | 空；`enabled` 时必须给 |
| `require_client_cert` | 是否要求客户端证书（mTLS） | `false` |
| `client_ca` | 校验客户端证书用的 CA，仅 mTLS 时用 | 空 |
| `handshake_timeout` | TLS 握手超时（默认 60s 会让"明文打 TLS 端口"白占连接一分钟） | `10.0` |
| `visitor_enabled` | **访客端口** TLS 的**默认值**（未显式表态的端口跟不跟着它，不是总开关） | `false` |
| `visitor_cert` / `visitor_key` | 访客端口证书链 / 私钥（PEM，须成对）。**独立于 `cert`/`key`，绝不回落** | 空 |

**客户端 `tls`**：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否开启 TLS | `false` |
| `ca` | 可信 CA；留空用系统信任库（服务端是公网证书时） | 空 |
| `cert` / `key` | 客户端证书/私钥，仅服务端开 mTLS 时用（须成对） | 空 |
| `check_hostname` | 是否校验主机名（关了仍校验证书链） | `true` |
| `skip_verify` | 完全跳过校验（**仅调试**，会记 WARNING） | `false` |

命令行开关（优先级高于配置）：服务端 `--tls-cert/--tls-key/--tls-client-ca/--no-tls`；
客户端 `--tls-ca/--tls-cert/--tls-key/--tls-skip-verify/--no-tls`。`gui.py` 与 `client.py` 参数一致，
自动继承。`--no-tls` 是本地演示逃生门（命令行优先级最高，能覆盖配置/环境变量里开着的 TLS）。
环境变量对应 `LOCALTONET_TLS_*`（如 `LOCALTONET_TLS_CERT`、`LOCALTONET_TLS_CA`）。

### 访客端口 TLS（per-port 开关，默认明文）

让"访客 ↔ 服务端"这一跳也走 TLS。**默认完全关闭**，且**逐端口**表态：

| 位置 | 取值 | 说明 |
| --- | --- | --- |
| `mapping[].tls` | `null`（缺省）/ `true` / `false` | `null` = 跟随 `tls.visitor_enabled`（默认 `false`＝明文） |
| `tls.visitor_enabled` | `true` / `false` | 只决定"未表态的端口"的默认值，**不是总开关** |

```jsonc
// 服务端 config.json：一份访客证书服务所有访客 TLS 端口（per-port 只控开关，不控证书）
{
  "tls": {
    "visitor_cert": "visitor-fullchain.pem",
    "visitor_key": "visitor.key",
    "visitor_enabled": false        // 默认明文，下面逐端口开
  },
  "mapping": [
    { "public_port": 9028, "local_port": 8000, "tls": true  },   // 这个端口 TLS 终止
    { "public_port": 9029, "local_port": 8001, "tls": false }    // 这个端口明文
  ]
}
```

```bash
# 命令行：给出证书即隐式把**全局默认**打开（所有未表态的端口都变 TLS）
python server.py --visitor-tls-cert visitor-fullchain.pem --visitor-tls-key visitor.key
# 逃生门：一键全关，连 mapping[].tls=true 也压过去（本地演示用）
python server.py --no-visitor-tls
```

> ⚠️ 只想给**个别**端口开 TLS 时，把证书写进 `config.json` 的 `tls` 块并保持 `visitor_enabled: false`，
> 用 `mapping[].tls` 逐端口表态。命令行给证书是"隐式打开全局默认"，会把没表态的端口一起变成 TLS。
> per-port 开关**只有 JSON 一条路**（同 `limits` 与 `mapping`），CLI 不做。

**三条刻意的设计决定**：

1. **证书独立、绝不回落**。缺 `visitor_cert`/`visitor_key` 时**报错**，不会退回 `tls.cert`——
   隧道自签证书和公网入口证书信任域不同，回落是隐式行为，误用代价大（以为配了公网证书、
   实则自签，浏览器报红查不出原因）。per-port 开了 TLS 却没证书 → **启动失败**，不静默降级成明文。
2. **只做单向认证**，不支持 mTLS。访客是公网上的陌生人，要求他出示证书等于把服务挂掉。
3. **不协商 ALPN**。一旦协商出 `h2` 而"客户端 → 后端"那跳仍是 HTTP/1.1，隧道并不透传 ALPN，
   会出现"浏览器以为在说 h2、后端在说 h1"的诡异故障；留空让浏览器退回 HTTP/1.1。

**开关可热切，证书路径只重启生效**：改 `mapping[].tls` 提交后监听会重建（`mapping_result.diff`
记在 `changed` 里），换证书文件则需要重启服务端；关掉 TLS 时会额外打一条 WARNING。
每个访客端口启动时都会打一行 `访客端口 N 监听于 host：TLS/明文`——这是"改了开关却没生效"
唯一的可见证据（**不做运行时协议探测**：服务端在配对前不读访客一个字节，要探测就得侵入字节透传路径）。

> **边界**：这个开关解决的是"访客 ↔ 服务端这一跳加密"。如果你的内网后端**本身就是 HTTPS**，
> 它已经能原样穿透隧道（服务端纯字节搬运），此时**不要**打开这个开关——那会变成
> "TLS 里再套一层 TLS"，白付一次握手和加密开销。想换证书品牌、加 WAF 或统一入口，
> 更合适的是在隧道前面放 Nginx/Caddy。

### 认证方向与失败语义

- **默认单向**：客户端验服务端证书（信任锚来自 `tls.ca` 或系统信任库）。应用层已有共享令牌做身份
  校验，mTLS 是**纵深防御**而非必需品——通过 `require_client_cert` + `client_ca` 单独开启。
- **证书校验失败绝不静默降级**：自签证书没给 CA、主机名不匹配、mTLS 缺客户端证书，都会在握手阶段
  明确报错；`skip_verify` 必须显式开启才生效，且会打一条 WARNING。
- **TLS 握手失败 = 永久失败**：与 403 同类。客户端立即停手、状态 `stopped`、退出码 `1`，
  **不会**每 60s 撞一次墙。两端 TLS 配置不一致（一端明文一端加密）也会被识别并停手，
  服务端侧另有一条日志点破"长度头非法，常见原因是两端 TLS 配置不一致"。

> 回执/展示仍用 `exc.message`、日志用 `str(exc)` 的约定不变；TLS 只影响传输层，协议帧格式零改动。

## 限流、配额与映射持久化

这三项都是**服务端侧**的可选能力，默认全部关闭——不配就是零开销（限流器连对象都不创建）。

### 带宽限流

挂在 `pipe_both` 的写入循环上，按**客户端**聚合、**上行/下行独立**计数：

```json
{
  "limits": {
    "per_client_upload_bps": 1048576,
    "per_client_download_bps": 4194304,
    "max_conns_per_client": 16
  }
}
```

（仓库自带的演示 `config.json` 里这三个字段都没写，即全部不限速、不限并发；
`limits` 下原有的 `max_clients` / `max_mappings` / `max_msg_len` 是**容量**上限，
仍然是"必须大于 0"的硬约束，与新增的配额字段语义不同。）

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `per_client_upload_bps` | 单客户端上行（访客 → 内网）字节/秒 | `0` = 不限 |
| `per_client_download_bps` | 单客户端下行（内网 → 访客）字节/秒 | `0` = 不限 |
| `max_conns_per_client` | 单客户端同时在途的转发连接数 | `0` = 不限 |

命令行：**暂无**。`limits` 这一组字段目前只走配置文件（`server.py` 的 CLI 没有对应开关，
环境变量也只覆盖 `control/data` 端口与 `auth`、`mapping_store`）。要调限流参数请改 `config.json`。

实现是**令牌桶**，不是"每秒整数切分"：桶容量取 `max(rate × burst_seconds, 16KB)`（`burst_seconds`
默认 0.25s，`16KB` 是下限，避免低速时限速被"一次只能发几十字节"卡死），
请求大于桶容量时拆成多段依次等待。所以限速是平滑的，突发小流量不会被生硬地拦下。

```python
# localtonet/core/limiter.py
bucket = TokenBucket(rate=1048576, burst=262144)
waited = await bucket.acquire(len(chunk))   # 返回累计等待秒数，不 busy-wait
```

等待用 `await asyncio.sleep(deficit / rate)`，`acquire()` 返回的等待秒数累加到
`PipeStats.throttled`，最终汇总进 `ServerStats.throttled_seconds`，便于观测"这条链路被限了多久"。
时钟与 sleep 都可注入，测试用假时钟断言**不空转**。

### 并发配额

访客请求进来时先查该客户端在途转发数，超了立刻回 `429` 并**不排队**：

```python
inflight = self._pending.count_for_client(session.client_id)
if limit > 0 and inflight >= limit:
    await self._reply_http(reader, writer, 429, "Per-client concurrency limit reached")
    return
```

选择"直接拒"而不是"排队"：排队的连接会占着服务端 fd 与内存，把服务端自己的容量拖垮；
快速失败让客户端（或前面的 Nginx）自己决定退避策略，边界更清晰。

### 映射持久化

默认 `memory`（与原行为一致，重启回到配置文件状态）。要跨重启保留运行期改动：

```bash
python server.py --mapping-store file --mapping-store-path mappings.json
# 或等价的环境变量
LOCALTONET_MAPPING_STORE=file LOCALTONET_MAPPING_STORE_PATH=mappings.json python server.py
```

```json
{ "mapping_store": { "type": "file", "path": "mappings.json" } }
```

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `type` | `memory` / `file` | 存储后端；`file` 必须给 `path` |
| `path` | 文件路径 | 只给 `--mapping-store-path` 会**隐式切到 file 模式** |

**谁的优先级更高？** 落盘文件是权威，`config.json` 的 `mapping` 只在文件不存在时当种子：

| 启动时 | 实际监听的端口 |
| --- | --- |
| 文件不存在 / 为空 | `config.json` 的 `mapping`（并立即落盘一份） |
| 文件有内容 | **文件内容**；若与 `config.json` 不一致，日志给一条 WARNING 提示 |

> 这与全局的"配置文件优先"铁律是**刻意的例外**：持久化的意义就是"运行期改动能活过重启"。
> 若还让 `config.json` 覆盖它，`set_mapping` 的效果一重启就没了，持久化等于白做。
> 想清空持久化状态，删掉那个 JSON 文件即可。

写入用 `临时文件 + flush + fsync + os.replace`，保证不会出现写了一半的坏文件：

| 情况 | 行为 |
| --- | --- |
| 落盘失败（磁盘满 / 只读） | **降级**：记 ERROR 日志并继续服务，内存映射仍然生效 |
| 文件内容损坏 / 不是数组 / 端口重复 | **fail fast**：抛 `ConfigError`，退出码 `2`，日志给出恢复提示 |

写入失败只降级、读取失败就报错——因为前者的代价是"这次改动没存住"，
后者意味着"接下来监听的端口可能是错的"，宁可不开。

## 目录结构

```
LocalToNet/
├── protocol.py               帧格式、Codec 抽象、指令常量、send_msg/recv_msg
├── config.py                 配置模型与校验（dataclass，强类型 + fail fast）
├── config.json               服务端配置示例
├── client.json               客户端配置示例
├── server.py / client.py     命令行入口
├── gui.py                    图形界面入口（参数与 client.py 共用）
├── localtonet/
│   ├── errors.py             业务异常（带 code，用于映射 HTTP 状态码）
│   ├── core/                 两端共用基础设施
│   │   ├── dispatcher.py     指令注册表（@handler 装饰器）
│   │   ├── pipe.py           双向字节搬运 pipe_both + 限流挂载点
│   │   ├── limiter.py        令牌桶与按客户端限流器（ClientRateLimiter）
│   │   ├── heartbeat.py      心跳任务 + 失联看门狗
│   │   ├── backoff.py        指数退避
│   │   ├── events.py         事件总线（GUI / 指标挂载点）
│   │   ├── rules.py          端口与映射表校验（服务端与 GUI 共用同一份规则）
│   │   ├── tls.py            TLS 上下文构建（服务端/客户端，建一次全程复用）
│   │   └── runtime.py        后台任务托管、对端地址格式化
│   ├── server/               服务端
│   │   ├── core.py           三通道编排
│   │   ├── registry.py       在线客户端表 + 端口归属路由
│   │   ├── pending.py        访客连接与数据通道的配对挂起
│   │   ├── mapping.py        映射表、存储后端（内存 / JSON 文件）与访客端口监听生命周期
│   │   └── auth.py           鉴权（放行 / 共享令牌，常量时间比较）
│   ├── client/               客户端
│   │   ├── core.py           控制长连接、心跳、重连
│   │   └── forwarder.py      数据通道 + 内网后端连接
│   └── gui/                  图形界面（tkinter 只出现在 app.py）
│       ├── model.py          映射表编辑缓冲区 + 界面状态（纯逻辑）
│       ├── bridge.py         asyncio 线程 ↔ tkinter 线程的桥
│       ├── controller.py     拉起客户端、订阅事件、提交映射
│       ├── viewmodel.py      邮筒消息 → 表格与状态栏（纯逻辑）
│       └── app.py            窗口、表格、按钮、状态栏、日志面板
├── examples/demo_backend.py  演示用内网 HTTP 服务
└── tests/                    281 项测试（单测 + 端到端 + GUI + 命令行 + TLS + 访客端 TLS）
```

## 协议

### 帧格式

```
┌──────────────────────────────┬────────────────────┐
│ 4 字节大端长度头 (struct "!I") │ JSON 消息体 (UTF-8) │
└──────────────────────────────┴────────────────────┘
```

接收端先 `readexactly(4)` 拿长度，再 `readexactly(length)` 精确切出消息体。
单条消息上限 10MB，对端谎报超长长度会被**立刻拒绝**，不会按该长度分配内存。

### 指令集

| 指令 | 方向 | 载荷 | 用途 |
| --- | --- | --- | --- |
| `register_client` | 客户端 → 服务端 | `client_id`, `local_ports`, `token`, `version`, `hostname` | 注册身份并认领本地端口 |
| `register_ack` | 服务端 → 客户端 | `ok`, `code`, `msg`, `claimed`, `conflicts`, `data_host`, `data_port` | 回执认领结果与数据通道地址 |
| `mapping_list` | 服务端 → 客户端 | `mapping` | 下发当前映射表 |
| `new_conn` | 服务端 → 客户端 | `conn_id`, `local_port`, `public_port` | 通知客户端开一条转发 |
| `register` | 客户端 → 服务端（数据通道） | `conn_id` | 数据通道配对 |
| `ping` / `pong` | 双向 | `ts` | 心跳保活 |
| `conn_error` | 客户端 → 服务端 | `conn_id`, `reason` | 本地连接失败上报 |
| `set_mapping` | 客户端 → 服务端 | `mapping` | 动态修改映射表 |
| `mapping_result` | 服务端 → 客户端 | `ok`, `msg`, `diff` | 映射修改回执 |

> 相对教程协议表，本项目在 `register_ack` 上扩展了 `ok` / `code` / `msg` / `data_host` / `data_port`。
> 前三个让客户端能区分"网络抖动"与"凭据错误"（403 属于永久失败，停止重试而非每 60 秒撞一次墙）；
> 后两个让客户端的数据通道地址由服务端下发，同一份配置在本机演示与公网部署下都能直接跑。
>
> `code` 里有两类必须**分开对待**，混同任何一边都是 bug：
> `403` 凭据不对 → **永久失败**，客户端停止重试；
> `503`（`limits.max_clients` 已满）/ `429`（单客户端配额已满）→ **暂时失败**，客户端继续退避重试。

### 访客端口上的 HTTP 状态码

服务端只在**自己无法转发**时才手写最小 HTTP 响应（正常转发是纯字节搬运，不解析 HTTP）：

| 状态码 | 触发条件 | 语义 |
| --- | --- | --- |
| `404` | 访客端口没有对应映射规则（竞态窗口） | 请求打到了不该监听的端口 |
| `502` | 无在线客户端 / 数据通道没就绪 / 配对超时 / 内网后端连不上 | 转发链路断了，原因写在 body 里 |
| `429` | 该客户端的在途转发数已达 `limits.max_conns_per_client` | **客户端自己的额度满了**，稍后重试即可 |
| `503` | 服务端容量满（`limits.max_clients`） | **服务端整体挤不下**，与具体客户端无关 |

> `429` 与 `503` 是两件事：前者是"你一个人的额度用完了"（其他客户端照常），
> 后者是"整台机器满了"。合并成一个码会让客户端无法判断该不该退避、运维也无法定位瓶颈。
>
> 回执与展示统一用 `exc.message`（不带 `[code]` 前缀），只有日志里才用 `str(exc)`。
> 混用会出现 `[403] [403] …` 叠字。

## 核心机制

**`PendingConn` 配对握手**
访客先到、数据通道后到，中间靠两个 future 协调：`ready` 表示配对是否成功，
`done` 表示搬运是否结束。配对超时用 `asyncio.wait` 而非 `wait_for`——
后者超时会**取消**它等待的 future，导致迟到的数据通道撞上 `InvalidStateError`。

**端口独占路由**
`port_owner: {本地端口 → client_id}` 登记归属。访客请求优先派给端口归属者，
归属者不在线才退回第一个在线客户端。客户端断开时自动释放其持有的全部端口。

**错误兜底**

| 场景 | 访客看到 |
| --- | --- |
| 端口没有映射规则（竞态窗口） | `404 No mapping for this port` |
| 服务端上没有在线客户端 | `502 No client online` |
| 客户端连不上内网后端 | `502` + **真实原因**（经 `conn_error` 上报） |
| 配对超时 | `502` + 超时时长 |
| 单客户端在途转发数超配额 | `429 Per-client concurrency limit reached` |

> 响应写完先半关闭写端（`write_eof`）再丢弃对端剩余请求字节——否则带着未读数据关闭连接，
> TCP 会发 **RST 而不是 FIN**，Windows 上未读走的响应字节会被直接丢掉，
> 表现为客户端读到 0 字节 + `ConnectionAbortedError`（`curl` 因为读得快反而"看起来正常"）。

> ⚠️ 配置约束：`pair_timeout` 必须**明显大于**客户端的 `connect_timeout`。
> 否则客户端来不及上报 `conn_error`，访客只会看到笼统的"配对超时"，
> 真正的原因（后端没启动、端口写错）反而被吞掉。

## 图形界面

```bash
python gui.py                                  # 用 client.json + 自动连接
python gui.py --server 1.2.3.4:7000 --local-ports 8000 --token <令牌>
python gui.py --no-autostart                   # 先把映射表配好再连
```

界面能做的事：

| 操作 | 说明 |
| --- | --- |
| 连接 / 断开 | 拉起或停止客户端主循环，心跳与重连策略完全复用命令行客户端那一套 |
| 新增 / 复制 / 删除 / 双击编辑 | 直接改映射表；非法输入当场拦下并说明原因。表格里的「访客 TLS」列是三态：跟随 / 开 / 关，编辑对话框用下拉选择 |
| 提交 | 走既有的 `set_mapping` 指令，服务端**立即起停对应端口**，不必重启进程 |
| 放弃修改 | 回到服务端最近一次下发的版本 |
| 状态栏 | 连接状态、认领端口、端口冲突、活跃转发、请求数与上下行字节 |
| 事件日志 | 断线原因、重连次数、转发失败原因等；成功请求不逐条记，避免刷屏 |

两个值得说明的设计：

**1. GUI 是壳，业务逻辑一行都没重写。**
界面只做三件事：把 `TunnelClient.snapshot()` 的公开状态画出来、订阅 `EventBus`、
按钮点击时调 `TunnelClient.set_mapping()`。校验也**不是**另一套——
`localtonet/core/rules.py` 里的 `parse_mapping` / `parse_ports` 同时被服务端准入
校验和界面即时校验调用，所以不会出现"界面放行、服务端拒绝"的分裂。

**2. 界面与网络分属两个线程，中间只有一条单向邮筒。**

```
主线程（tkinter）                       工作线程（asyncio）
App  ──root.after(100ms)──► drain()  ◄── queue ◄── EventBus 订阅者
 └── LoopThread.submit(coro) ──────────► client.run() / stop() / set_mapping()
```

tkinter 的控件调用必须留在主线程，而 asyncio 的循环一旦跑起来就独占线程，
所以事件循环搬到了后台线程。跨线程只有两个受控入口：`submit()`（提交协程）
与 `drain()`（取消息）。投递用非阻塞写队列——界面卡住时宁可丢显示消息，
也绝不阻塞网络线程。

**服务端推送与用户编辑的冲突**：其他客户端改了映射，服务端会广播给所有人。
若界面直接刷新，正在编辑的人会被静默清空输入。所以只要本地有未提交改动，
界面就**保留用户输入**，只更新"已保存"快照并提示"远端已更新"。静默覆盖用户
正在敲的内容是最伤人的交互之一。

**无界面环境照常可用**：`localtonet/gui/__init__.py` 不 import `app`，
因此没有 tkinter 的机器上 `import localtonet.gui` 依然成功，服务端与命令行客户端
完全不受影响（有一条子进程测试把 `tkinter` 从 `sys.modules` 里挖掉来实测这一点）。
只有真的要开窗口时才会给出可读提示并以退出码 2 结束。
Linux 上若缺 tkinter，安装系统包 `python3-tk` 即可（Windows/macOS 官方发行版自带）。

## 扩展点

| 扩展点 | 抽象位置 | 当前实现 | 可替换为 |
| --- | --- | --- | --- |
| 帧编解码 | `protocol.Codec` | 长度头 + JSON | msgpack / protobuf / 压缩 |
| 指令处理 | `core.dispatcher.MessageDispatcher` | `@handler` 注册表 | 新增指令零侵入主循环 |
| 客户端鉴权 | `server.auth.Authenticator` | `NoneAuthenticator` / `TokenAuthenticator`（共享令牌） | mTLS / 签名挑战 / SSO |
| 端口路由策略 | `ClientRegistry(routing=...)` | 归属优先 → 首个在线 | 轮询 / 加权 / 标签路由 |
| 映射持久化 | `server.mapping.MappingStore` | 内存 / JSON 文件（原子写） | SQLite / Redis |
| 映射校验规则 | `core.rules.parse_mapping` | 服务端与 GUI 共用一份 | 增删规则只改这一处 |
| 界面与观测 | `core.events.EventBus` | tkinter GUI + 结构化日志 | Web 界面 / Prometheus |
| 限流配额 | `core.pipe.RateLimitHook` | `ClientRateLimiter`（令牌桶，按客户端 × 方向） | 加权公平队列 / 按端口限速 |
| 传输加密 | 建立连接处 | 明文 / TLS（`core.tls` 建上下文），控制+数据+访客三跳各自可选 | 会话密钥 / 换 TLS 库 |
| 超时参数 | `config.Timeouts` | 集中默认值 | 环境变量 / 运行时可调 |

事件总线已预留 `REQUEST_START` / `REQUEST_END` / `CONN_ERROR` / `MAPPING_CHANGED` 等事件，
GUI 已经挂上去（见 `localtonet/gui/controller.py` 的订阅白名单），加指标系统同理，核心链路无需改动。
客户端 `TunnelClient.set_mapping()` 封装了"提交映射并等待回执"的语义，界面表格的提交按钮直接调它。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

当前 **281 项全部通过**（test_protocol 17 / test_config 56 / test_core 40 / test_e2e 21 /
test_server_cli 21 / test_client_cli 5 / test_limiter 12 / test_mapping_store 21 /
test_tls 10 / test_visitor_tls 13 / test_gui_model 49 / test_gui_bridge 10 / test_gui_controller 6），
其中 21 项是真实拉起三件套、走真实 TCP 的端到端测试：

| 用例 | 验证内容 |
| --- | --- |
| 基础打通 | 公网请求到达内网后端并原样返回 |
| 并发 20 请求 | 不同 `conn_id` 不串流量 |
| 1MB 大包 | 长连接大流量不丢字节 |
| 流式响应 | 分块到达顺序正确、未被整段缓冲 |
| 无在线客户端 | `502 No client online` |
| 客户端掉线 | `502`，端口归属被释放 |
| 后端未启动 | `conn_error` 上报 + 502 带真实原因 |
| 端口独占 | 第二个客户端被拒，老客户端不受影响 |
| 动态映射 | 加端口立即可用、删端口立即失效 |
| 无映射规则 | `404`（竞态窗口分支） |
| 未映射端口 | 根本不监听，连接直接被拒 |
| 控制连接断开 | 客户端自动重连并重新认领端口 |
| 鉴权通过 | 服务端开鉴权 + 客户端带对令牌 → 注册成功、映射生效、访客端口可访问 |
| 令牌错误 | `403` → 客户端**只拨号一次**、状态 `stopped`、`CONTROL_LOST(fatal=True)` |
| 令牌缺失 | 同上，症状与令牌错误完全一致 |
| 容量已满 | `503` **不**被当成 fatal，客户端继续退避重试；已在线客户端不受影响 |
| 带宽限流 | 限速后同一份数据的耗时**明显长于**不限速基线，且字节数一个不少 |
| 并发配额 | 打满 `max_conns_per_client` 后新请求回 `429`；在途请求照常返回 200 |
| 在线数观测 | `stats.clients_online` 随客户端上下线增减 |
| 运行期持久化 | `set_mapping` 后映射文件立刻出现新端口 |
| 重启后存活 | 换一个 `TunnelServer` 实例重启，仍监听上次持久化的端口，而 `config.json` 里的端口未生效 |

> 后两条守的是同一条线：`403` 与 `503` 必须**分开处理**——凭据错重试无意义（停手），
> 容量满重试有意义（继续）。把它们统一成任一种都是 bug。

`tests/test_tls.py`（10 项）覆盖传输加密，守四条验收线：明文与 TLS 两套端到端用例**并存且都绿**
（加密不取代明文）；**证伪用例**——服务端只开 TLS 时明文客户端连不上（证明加密真在生效，而不是
"配了但没起作用"）；**证书校验失败显式报错、不静默降级**（自签不给 CA、主机名不匹配、mTLS 缺证书
三种都测，`skip_verify` 必须显式开启才生效）；**TLS 握手失败 = 永久失败**（客户端停手、退出码 1，
不无限退避重试）。测试证书是 `tests/certs/` 里入库的一次性材料（有效期 30 年，见其 README），
`pytest` 路径上不调用 openssl——保持"零运行时依赖"也适用于测试。

`tests/test_visitor_tls.py`（13 项）覆盖**访客端口** TLS 终止，守六条线：**正向**（per-port 开 TLS
后往返正常、TLS 端口与明文端口并存于同一张映射表、512KB 不丢字节）；**证伪**（明文访客打 TLS 端口、
TLS 访客打明文端口都必须拿不到数据，否则"配置里多了几个字段但连接还走明文"会蒙混过关）；
**默认行为一字不变**（不配任何 visitor 字段就是全明文；手写缺 `tls` 键的老 `mappings.json` 照旧加载）；
**回滚安全**（`MappingRule(tls=None).to_dict()` 不含 `tls` 键，而 `tls=False` 必须落盘）；
**不静默降级**（缺证书一律报错，per-port 开了 TLS 却没证书 → 启动失败且一个监听都不留）；
**热切换**（切 `tls` 时 `diff.changed` 有它、监听重建、握手协议真的换了；缺证书的更新必须在
停监听**之前**被拒，端口仍能正常服务）。

`tests/test_limiter.py` 用假时钟（记录每次 sleep 的时长）断言令牌桶**真的在等**而不是空转：
初始桶是满的、请求大于桶容量时被拆分且总量守恒、`rate <= 0` 被拒、禁用时不创建任何桶。
`tests/test_mapping_store.py` 覆盖文件后端的原子写、损坏文件的 fail fast、写入失败的降级，
以及"落盘文件权威、`config.json` 只当种子"这条语义（内存后端同样遵守，因为是存储层语义）。

`tests/test_server_cli.py` 覆盖服务端命令行参数（`--token` / `--no-auth` / `--mapping-store*` /
`--tls-cert` / `--tls-key` / `--tls-client-ca` / `--no-tls` 与配置优先级铁律），其中一条专门钉死
"仓库自带的 `config.json` 必须保持 `auth.enabled=false` 且 `tls.enabled=false`"——
免得哪天演示配置被顺手改成要令牌/要证书，本地 demo 突然跑不起来。

`tests/test_client_cli.py` 用子进程跑真实 `client.py` 对着一个"必定拒绝"的假服务端，钉死
**退出码契约**：令牌错 → `1`；配置缺失 → `2`；`503` → 进程**继续活着**（不是启动即退）；
TLS 客户端连明文服务端 → `1`（TLS 错配是永久失败）；以及回执消息里**不带** `[403]` 前缀。

`tests/test_core.py` 另有针对 `pipe_both` 交叉配对的回归用例——
上行与下行必须写向**对侧**，写成 `a_reader → a_writer` 就成了原地回环，
表面上"日志里有流量"，对端却永远收不到。

GUI 相关的三项测试（`test_gui_model` / `test_gui_bridge` / `test_gui_controller`）
覆盖的都是"不需要显示器也能验"的部分：映射表编辑与校验、服务端推送与用户编辑的冲突策略、
后台事件循环里的真实 TCP I/O、以及界面提交映射的完整链路（真实后端 + 真实服务端 + 真客户端，
提交后新端口真的能被 `curl` 到、删掉后真的不再监听）。
窗口本身（`app.py`）不做自动化测试——它需要显示器，改版式也不该影响这些断言。

## 已知限制与后续路线

- 只代理 TCP，不支持 UDP
- 不做 HTTP 解析与改写：转发是纯字节搬运，仅在自己无法转发时才手写最小 HTTP 错误响应
- 映射表默认只存内存；`--mapping-store file` 已可落盘，但**只支持单进程**（多实例共享同一文件会互相覆盖）
- **界面改的是服务端的映射表**；客户端"认领哪些本机端口"仍来自启动配置
  （协议里没有运行期修改认领端口的指令，要支持得先扩展协议）
- 图形界面需要 tkinter（CPython 标准库，不算第三方依赖）；
  精简安装的 Linux 上可能需要 `apt install python3-tk`
- 限流是**按客户端聚合**的粗粒度：同一客户端的所有端口共享一份带宽额度，
  要做"按端口"或"按访客 IP"限速需换 `RateLimitHook` 实现
- 限流与配额**只在服务端生效**：客户端侧不做自我限速（服务端是唯一的流量汇聚点，
  在汇聚点限流才能防住"客户端被改坏/恶意"的情况）
- **传输默认仍是明文**：不配 `tls` 时，令牌放在 `register_client` 帧里走 TCP。开 TLS 后**线路上**不再明文
  （控制+数据两跳），但 `client.json` 里的 `auth_token` 仍是明文落盘、`--token` 会出现在进程列表里——
  这两条 TLS 解决不了，生产环境优先用环境变量或受限权限的配置文件
- 鉴权只有**共享令牌**，没有按客户端区分身份：持有令牌的客户端可以认领任意访客端口；
  mTLS（`require_client_cert`）能在传输层加一道客户端证书身份，但分发/轮换客户端证书本身是运维负担
- 令牌是**静态**的：轮换需要重启服务端（换成 mTLS / 签名挑战 / 一次性票据见扩展点表）
- 访客端口 TLS **只做单向认证**：访客是公网陌生人，不支持也不打算支持 mTLS
- 访客端口 TLS 的证书是**一份服务所有端口**：per-port 只控开关，不能逐端口配不同证书；
  证书路径**只在重启时生效**（改证书文件需重启服务端），能热切的只有开关
- 显式设了 per-port `tls` 的 `mappings.json` **无法回滚到 `beaaf5f`**：老版本的 `_check_unknown`
  会因多出的 `tls` 键拒绝启动。只有"未显式设置"（不写 `tls` 键）的规则才回滚安全
- 访客端口**不做运行时协议探测**：服务端在配对前不读访客一个字节（纯透传），
  所以"明文请求打到了 TLS 端口"这类错配只能靠启动日志的逐端口状态提示，不会自动纠正

后续计划：服务端侧管理界面 → 按请求的带宽统计与限流粒度细化 → 令牌热轮换（免重启）。
