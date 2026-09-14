#!/usr/bin/env bash
#
# 重新生成 tests/certs/ 下的抛弃型测试证书。
#
# ⚠️ 正常开发**不需要**跑这个脚本。证书已经入库，pytest 直接读它们，
#    测试路径上不调用 openssl —— 这是刻意的：让"零运行时依赖"也适用于测试，
#    换台没装 openssl 的机器照样能跑。
#    只有证书过期（2056 年）或需要改 SAN 时才重跑本脚本。
#
# 用法（Git Bash）：
#   export PATH="/usr/bin:/bin:/c/Program Files/Git/cmd:$PATH"
#   bash tests/certs/gen_certs.sh
#
set -euo pipefail
cd "$(dirname "$0")"

DAYS=10950   # 30 年。足够覆盖项目寿命，又不会触发某些工具对"超长有效期"的告警噪音。

if ! command -v openssl >/dev/null 2>&1; then
  echo "找不到 openssl；请把 Git 自带的 openssl 加入 PATH（/c/Program Files/Git/usr/bin）" >&2
  exit 1
fi

# ---------------------------------------------------------------- 1) 自签 CA
openssl req -x509 -newkey rsa:2048 -sha256 -days "$DAYS" -nodes \
  -keyout ca.key -out ca.pem \
  -subj "/O=LocalToNet/CN=LocalToNet Test CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

# --------------------------------------------------- 2) 由 CA 签发的三张叶证书
sign() {  # sign <名字> <主题> <扩展配置文件> <扩展节名>
  local name="$1" subject="$2" extfile="$3" section="$4"
  openssl req -newkey rsa:2048 -nodes -keyout "$name.key" -out "$name.csr" -subj "$subject"
  openssl x509 -req -in "$name.csr" \
    -CA ca.pem -CAkey ca.key -CAcreateserial \
    -out "$name.pem" -days "$DAYS" -sha256 \
    -extfile "$extfile" -extensions "$section"
  rm -f "$name.csr"
}

# 正常服务端证书：SAN 必须同时含 DNS:localhost 与 IP:127.0.0.1。
# 测试一律连 127.0.0.1，ssl 模块会对 IP 做 SAN 校验，漏掉 IP 那条正例就全红。
sign server             "/O=LocalToNet/CN=localhost"            ext/server.cnf          v3_server
# 主机名不匹配专用：证书链有效，只有 SAN 对不上
sign server-badhost     "/O=LocalToNet/CN=localto.net.invalid"  ext/server-badhost.cnf  v3_server
# 双向认证用的客户端证书
sign client             "/O=LocalToNet/CN=localtonet-test-client" ext/client.cnf        v3_client

rm -f ca.srl
echo "已重新生成：ca / server / server-badhost / client"
openssl x509 -in server.pem -noout -subject -ext subjectAltName
