#!/usr/bin/env bash
# 阶段 5 真实 HTTP 端到端验证：真实 uvicorn + curl。
# 断言：
#   1) GET / 返回 200 且 CSP 不含 unsafe-inline
#   2) 无会话的写请求被拒绝（403 session_rejected）
#   3) 有会话但错误 CSRF 被拒绝（403）
#   4) 无 Origin 的 POST 被拒绝（403 csrf_origin_rejected）
#   5) 完整授权链路：会话 → 计划 → 确认 → 执行 → 成功
#   6) 已执行令牌不可复用（409）
#   7) 只读操作历史不含令牌明文
cd /data/user/0/com.ai.assistance.operit/files/workspace/baa843f1-7d93-466b-93b5-647f4942442b/mcp-manager || exit 1

export MCPM_DATA_DIR=/tmp/mcpm_stage5_e2e
export MCPM_ALLOWED_HOSTS=127.0.0.1
export MCPM_ALLOWED_ORIGINS=http://127.0.0.1:8791
rm -rf "$MCPM_DATA_DIR"
mkdir -p "$MCPM_DATA_DIR"

FAIL=0
ok()  { echo "PASS  $1"; }
bad() { echo "FAIL  $1"; FAIL=$((FAIL+1)); }

# --- 启动真实 uvicorn ---
.venv/bin/python -m uvicorn scripts._stage5_e2e_app:create_app --factory \
  --host 127.0.0.1 --port 8791 --log-level warning > /tmp/mcpm_s5.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

for _ in $(seq 1 60); do
  sleep 0.5
  if curl -s -o /dev/null "http://127.0.0.1:8791/api/health"; then break; fi
done

B=http://127.0.0.1:8791
O="http://127.0.0.1:8791"

# --- 1) 首页与 CSP ---
H=$(curl -s -D - -o /dev/null "$B/")
echo "$H" | grep -q "200" && ok "GET / 返回 200" || bad "GET / 状态码异常"
echo "$H" | grep -qi "content-security-policy" && ok "携带 CSP" || bad "缺少 CSP"
if echo "$H" | grep -qi "unsafe-inline"; then bad "CSP 含 unsafe-inline"; else ok "CSP 不含 unsafe-inline"; fi

# --- 2) 无会话写请求 ---
CODE=$(curl -s -o /tmp/s5_a.json -w '%{http_code}' -X POST "$B/api/install/plan" \
  -H "Origin: $O" -H 'Content-Type: application/json' -d '{"plugin_id":"x"}')
[ "$CODE" = "403" ] && ok "无会话写请求 403" || bad "无会话写请求返回 $CODE"
grep -q "session_rejected" /tmp/s5_a.json && ok "错误码 session_rejected" || bad "错误码不符"

# --- 3) 建立会话 ---
CSRF=$(curl -s -c /tmp/s5_cookie.txt -D /tmp/s5_hdr.txt -o /dev/null "$B/api/session" && \
  grep -i '^x-csrf-token:' /tmp/s5_hdr.txt | sed 's/.*: *//' | tr -d '\r')
[ -n "$CSRF" ] && ok "会话建立并下发 CSRF" || bad "未拿到 CSRF 令牌"

# --- 3b) 错误 CSRF ---
CODE=$(curl -s -b /tmp/s5_cookie.txt -o /dev/null -w '%{http_code}' -X POST "$B/api/install/plan" \
  -H "Origin: $O" -H 'X-CSRF-Token: wrong' -H 'Content-Type: application/json' -d '{"plugin_id":"x"}')
[ "$CODE" = "403" ] && ok "错误 CSRF 被拒绝 403" || bad "错误 CSRF 返回 $CODE"

# --- 4) 无 Origin ---
CODE=$(curl -s -b /tmp/s5_cookie.txt -o /dev/null -w '%{http_code}' -X POST "$B/api/install/plan" \
  -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' -d '{"plugin_id":"x"}')
[ "$CODE" = "403" ] && ok "无 Origin 被拒绝 403" || bad "无 Origin 返回 $CODE"

# --- 5) 完整链路：需要一个带 pinned_ref 的条目 ---
PID=$(.venv/bin/python - <<'PY'
import os
from app.config import load_settings
from app.db import open_database
from app.runtime import Runtime
from app.models import Plugin, PluginKind

s = load_settings(host="127.0.0.1", port=8791)
rt = Runtime.create(s)
p = rt.plugins.upsert(Plugin.new(
    source="github:acme/demo", slug="demo", name="acme/demo",
    kind=PluginKind.MCP_SERVER, pinned_ref="a"*40), actor="user")
print(p.id)
rt.close()
PY
)
[ -n "$PID" ] && ok "已注册测试条目 $PID" || bad "注册条目失败"

PLAN=$(curl -s -b /tmp/s5_cookie.txt -X POST "$B/api/install/plan" \
  -H "Origin: $O" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d "{\"plugin_id\":\"$PID\"}")
OPID=$(echo "$PLAN" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$OPID" ] && ok "生成安装计划 $OPID" || bad "生成计划失败：$PLAN"

STATUS=$(echo "$PLAN" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
[ "$STATUS" = "awaiting_confirmation" ] && ok "计划状态 awaiting_confirmation" || bad "计划状态 $STATUS"

CONF=$(curl -s -b /tmp/s5_cookie.txt -X POST "$B/api/operations/$OPID/confirm" \
  -H "Origin: $O" -H "X-CSRF-Token: $CSRF")
TOKEN=$(echo "$CONF" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin).get("confirmation_token",""))' 2>/dev/null)
[ -n "$TOKEN" ] && ok "拿到一次性确认令牌" || bad "确认失败：$CONF"

EXEC=$(curl -s -b /tmp/s5_cookie.txt -X POST "$B/api/operations/$OPID/execute" \
  -H "Origin: $O" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d "{\"confirmation_token\":\"$TOKEN\"}")
EXST=$(echo "$EXEC" | .venv/bin/python -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
[ "$EXST" = "succeeded" ] && ok "执行成功 succeeded" || bad "执行状态 $EXST：$EXEC"

# --- 6) 令牌复用 ---
CODE=$(curl -s -b /tmp/s5_cookie.txt -o /dev/null -w '%{http_code}' -X POST \
  "$B/api/operations/$OPID/execute" -H "Origin: $O" -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -d "{\"confirmation_token\":\"$TOKEN\"}")
[ "$CODE" = "409" ] && ok "已用令牌复用被拒绝 409" || bad "令牌复用返回 $CODE"

# --- 7) 只读历史不泄露令牌 ---
DETAIL=$(curl -s "$B/api/operations/$OPID")
if echo "$DETAIL" | grep -q "$TOKEN"; then bad "只读接口泄露了令牌明文"; else ok "只读接口不泄露令牌"; fi
echo "$DETAIL" | grep -q '"logs"' && ok "操作详情含步骤日志" || bad "缺少步骤日志"

echo
if [ "$FAIL" -gt 0 ]; then echo "结果：$FAIL 项失败"; exit 1; fi
echo "结果：全部通过"
exit 0
