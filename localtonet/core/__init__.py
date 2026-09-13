# -*- coding: utf-8 -*-
"""localtonet.core —— 客户端与服务端共用的基础设施。

| 模块 | 职责 |
| --- | --- |
| ``dispatcher`` | 指令 → 处理函数的注册表与分发 |
| ``pipe`` | 双向字节搬运（``pipe_both``） |
| ``heartbeat`` | 心跳任务与失联看门狗 |
| ``backoff`` | 指数退避序列 |
| ``events`` | 事件总线（GUI / 指标 / 日志挂载点） |
"""
