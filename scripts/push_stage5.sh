#!/usr/bin/env bash
# 阶段 5：创建独立仓库并推送
cd /data/user/0/com.ai.assistance.operit/files/workspace/baa843f1-7d93-466b-93b5-647f4942442b/mcp-manager || exit 1

TOKEN=$(sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p' /root/.git-credentials | head -1)
[ -z "$TOKEN" ] && { echo "未找到令牌"; exit 1; }

REPO=mcp-manager-stage5
MSG_FILE=/tmp/commit_msg_stage5.txt

# 1) 创建远端仓库（已存在则忽略）
CODE=$(curl -s -o /tmp/s5_create.json -w '%{http_code}' -X POST \
  -H "Authorization: token $TOKEN" \
  -H 'Accept: application/vnd.github+json' \
  https://api.github.com/user/repos \
  -d "{\"name\":\"$REPO\",\"private\":false,\"description\":\"mcp-manager 阶段5：安全安装闭环（计划/确认/执行/回滚/卸载）\"}")
echo "创建仓库 HTTP $CODE"
sed "s/${TOKEN}/***TOKEN***/g" /tmp/s5_create.json | head -3

# 2) 提交
git add -A
git -c user.name="mcp-manager" -c user.email="dev@example.invalid" \
  commit -F "$MSG_FILE" -q
BASE=$(git rev-parse HEAD)
echo "提交：$BASE"

# 3) 推送
git remote remove stage5 2>/dev/null
git remote add stage5 "https://lzalookyou-stack:${TOKEN}@github.com/lzalookyou-stack/${REPO}.git"
git push -q stage5 main 2>&1 | sed "s/${TOKEN}/***TOKEN***/g"
git remote set-url stage5 "https://github.com/lzalookyou-stack/${REPO}.git"

# 4) 回读远端 HEAD
REMOTE_HEAD=$(git ls-remote stage5 refs/heads/main 2>/dev/null | awk '{print $1}')
echo "远端 HEAD：$REMOTE_HEAD"
[ "$BASE" = "$REMOTE_HEAD" ] && echo "远端 HEAD 一致 ✅" || echo "远端 HEAD 不一致 ❌"

# 5) 远端文件树核验
curl -s -H "Authorization: token $TOKEN" \
  "https://api.github.com/repos/lzalookyou-stack/${REPO}/git/trees/main?recursive=1" \
  > /tmp/s5_tree.json
.venv/bin/python - <<'PY'
import json
d = json.load(open('/tmp/s5_tree.json'))
if 'tree' not in d:
    print('远端文件树核验失败：', json.dumps(d)[:300]); raise SystemExit(1)
blobs = [t for t in d['tree'] if t['type'] == 'blob']
trees = [t for t in d['tree'] if t['type'] == 'tree']
print(f"远端文件树：{len(blobs)} blob / {len(trees)} tree / truncated={d.get('truncated')}")
PY