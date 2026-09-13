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
- **双保险保活**：客户端心跳探测 + 服务端失联看门狗
- **指数退避重连**：1s → 2s → 4s → … → 60s 封顶，成功即归零
- **明确错误语义**：404 / 502 兜底，绝不留下"空回复"
- **零运行时依赖**：纯标准库，Python 3.11+（开发环境用 3.13）

## 架构

```
┌─────────────┐        ┌──────────────────────────────┐        ┌──────────────────┐
│  公网访客    │        │        公网服务器             │        │   内网客户端      │
│ 浏览器/curl  │        │         server.py            │        │    client.py     │
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

# 终端 3：客户端（内网机器上运行）
python client.py --server 127.0.0.1 --local-ports 8000
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

配置优先级：**默认值 < JSON 文件 < 环境变量（`LOCALTONET_` 前缀）< 命令行参数**。

## 目录结构

```
LocalToNet/
├── protocol.py               帧格式、Codec 抽象、指令常量、send_msg/recv_msg
├── config.py                 配置模型与校验（dataclass，强类型 + fail fast）
├── config.json               服务端配置示例
├── client.json               客户端配置示例
├── server.py / client.py     命令行入口
├── localtonet/
│   ├── errors.py             业务异常（带 code，用于映射 HTTP 状态码）
│   ├── core/                 两端共用基础设施
│   │   ├── dispatcher.py     指令注册表（@handler 装饰器）
│   │   ├── pipe.py           双向字节搬运 pipe_both
│   │   ├── heartbeat.py      心跳任务 + 失联看门狗
│   │   ├── backoff.py        指数退避
│   │   ├── events.py         事件总线（GUI / 指标挂载点）
│   │   └── runtime.py        后台任务托管、对端地址格式化
│   ├── server/               服务端
│   │   ├── core.py           三通道编排
│   │   ├── registry.py       在线客户端表 + 端口归属路由
│   │   ├── pending.py        访客连接与数据通道的配对挂起
│   │   ├── mapping.py        映射表与访客端口监听生命周期
│   │   └── auth.py           鉴权扩展点
│   └── client/               客户端
│       ├── core.py           控制长连接、心跳、重连
│       └── forwarder.py      数据通道 + 内网后端连接
├── examples/demo_backend.py  演示用内网 HTTP 服务
└── tests/                    83 项测试（单测 + 端到端）
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

> ⚠️ 配置约束：`pair_timeout` 必须**明显大于**客户端的 `connect_timeout`。
> 否则客户端来不及上报 `conn_error`，访客只会看到笼统的"配对超时"，
> 真正的原因（后端没启动、端口写错）反而被吞掉。

## 扩展点

| 扩展点 | 抽象位置 | 当前实现 | 可替换为 |
| --- | --- | --- | --- |
| 帧编解码 | `protocol.Codec` | 长度头 + JSON | msgpack / protobuf / 压缩 |
| 指令处理 | `core.dispatcher.MessageDispatcher` | `@handler` 注册表 | 新增指令零侵入主循环 |
| 客户端鉴权 | `server.auth.Authenticator` | `NoneAuthenticator` 放行 | Token / mTLS / SSO |
| 端口路由策略 | `ClientRegistry(routing=...)` | 归属优先 → 首个在线 | 轮询 / 加权 / 标签路由 |
| 映射持久化 | `server.mapping.MappingStore` | 内存 | JSON 文件 / SQLite / Redis |
| 观测与界面 | `core.events.EventBus` | 结构化日志订阅者 | tkinter GUI / Prometheus |
| 限流配额 | `core.pipe.pipe_both` 写入循环 | 无 | 令牌桶限带宽 |
| 传输加密 | 建立连接处 | 明文 | TLS / 会话密钥 |
| 超时参数 | `config.Timeouts` | 集中默认值 | 环境变量 / 运行时可调 |

事件总线已预留 `REQUEST_START` / `REQUEST_END` / `CONN_ERROR` / `MAPPING_CHANGED` 等事件，
GUI 与指标系统挂上去即可，核心链路无需改动。
客户端 `TunnelClient.set_mapping()` 已封装好"提交映射并等待回执"的语义，可直接给表格的提交按钮用。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

当前 **83 项全部通过**，其中 12 项是真实拉起三件套、走真实 TCP 的端到端测试：

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

`tests/test_core.py` 另有针对 `pipe_both` 交叉配对的回归用例——
上行与下行必须写向**对侧**，写成 `a_reader → a_writer` 就成了原地回环，
表面上"日志里有流量"，对端却永远收不到。

## 已知限制与后续路线

- 只代理 TCP，不支持 UDP
- 不做 HTTP 解析与改写：转发是纯字节搬运，仅在自己无法转发时才手写最小 HTTP 错误响应
- 映射表仅存内存，服务端重启后回到配置文件的状态
- 未内置限速、流量统计页面与图形界面（接口均已预留）
- 明文传输，公网部署建议置于 TLS 终止层之后，或按上面的扩展点接入加密

后续计划：tkinter 可视化客户端（映射表格编辑 + 实时状态）→ 映射持久化 →
按请求的带宽统计 → TLS 与鉴权强化。
