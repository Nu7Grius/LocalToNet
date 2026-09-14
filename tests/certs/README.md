# 测试证书（抛弃型）

> ⚠️ 这些证书**只用于本地测试**，私钥入库是**刻意的**——它们是明确标注的抛弃型材料，
> 有效期 30 年（至 2056 年），保证测试不因证书过期而失败。
> **绝不可用于生产**，也不要拿它签任何真实流量。

## 为什么证书要入库，而不是测试时现生成

方案对比时选的是"仓库内置"而非"openssl 子进程现生成"，理由：

1. **测试不能依赖 openssl**。`pytest` 路径上**不调用** `openssl`（`gen_certs.sh` 只在手工
   重放时用）。换一台没装 openssl 的机器，测试照样能跑——保持"零运行时依赖"也适用于测试。
2. **确定性**。现生成有随机性与时序开销，内置证书让测试结果可复现。
3. 私钥入库的代价是"有人误拿去当生产证书"，靠本 README 的醒目警告 + 30 年有效期
   的双保险压到可接受范围。

## 材料清单

| 文件 | 用途 |
| --- | --- |
| `ca.pem` / `ca.key` | 自签 CA。客户端 `tls.ca` 指向它来信任服务端；服务端 mTLS 的 `client_ca` 也用它 |
| `server.pem` / `server.key` | 服务端证书，由 CA 签发，SAN 含 `DNS:localhost` + `IP:127.0.0.1` |
| `server-badhost.pem` / `server-badhost.key` | 主机名不匹配专用：证书链有效，SAN 只写 `DNS:localto.net.invalid` |
| `client.pem` / `client.key` | 双向认证用的客户端证书，由 CA 签发，`extendedKeyUsage=clientAuth` |
| `ext/*.cnf` | 各证书的扩展配置 |
| `gen_certs.sh` | 可重放的生成脚本 |

## 关键细节

- **`server.pem` 的 SAN 必须同时含 `DNS:localhost` 与 `IP:127.0.0.1`**。
  测试一律连 `127.0.0.1`，Python `ssl` 模块会对 IP 地址做 **IP SAN** 校验——
  漏掉 `IP:127.0.0.1` 这一条，本地开 TLS 的正例用例会**全部失败**（证书本身没问题）。
- "证书校验失败"的负例靠**两种**材料覆盖：
  - `server-badhost` → 证书链 OK、主机名不匹配（`CertificateError: hostname ... doesn't match`）；
  - 自签但不给 `tls.ca` → 证书链不受信任（`SSLCertVerificationError`）。
  这两种是不同层级的失败，不能只测一种。

## 重新生成

证书过期（2056 年）或需要改 SAN 时，用 Git Bash 跑：

```bash
export PATH="/usr/bin:/bin:/c/Program Files/Git/cmd:$PATH"
bash tests/certs/gen_certs.sh
```
