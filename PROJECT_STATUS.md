# PROJECT_STATUS — mcp-manager

> 跨对话 / 跨阶段的权威状态快照。**所有「已完成/未实现」的判断以本文件为准。**
> 最后更新：2026-10-09（阶段 4 收口）

---

## 基本信息

- 项目名：`mcp-manager`（本地优先的 MCP / Agent 插件管理器）
- 本地路径：`/data/user/0/com.ai.assistance.operit/files/workspace/baa843f1-7d93-466b-93b5-647f4942442b/mcp-manager`
- 技术栈：Python 3.12 + FastAPI + uvicorn + 官方 `mcp` SDK + SQLite + 原生 HTML/CSS/JS + asyncio + SSE
- 环境：Android proot Ubuntu 24.04 aarch64；无 systemd / 无 GPU；可用内存约 2.4 GB
- 目标仓库：**每个阶段一个独立 GitHub 仓库**（见下）

---

## 阶段进度

| 阶段 | 状态 | 仓库 | 备注 |
|---|---|---|---|
| 1 调研与技术选型 | ✅ 已完成 | https://github.com/lzalookyou-stack/mcp-manager-stage1 | 提交 `ec27ff3`，已推送 |
| 2 最小可运行骨架 | ✅ 已完成 | https://github.com/lzalookyou-stack/mcp-manager-stage2 | 77 项测试全绿；MCP stdio 冒烟 9 项通过；Web 实机验证通过 |
| 3 搜索/分析/评分/安全审查 | ✅ 已完成 | https://github.com/lzalookyou-stack/mcp-manager-stage3 | 154 项测试全绿；MCP stdio 冒烟 9 项通过；Web 实机验证通过 |
| 4 网页控制台交互 | ✅ 已完成 | https://github.com/lzalookyou-stack/mcp-manager-stage4 | 175 项测试全绿；SSE 端到端真实验证；Web 实机验证通过 |
| 5 安全安装闭环 | ✅ 已完成 | https://github.com/lzalookyou-stack/mcp-manager-stage5 | 202 项测试全绿；MCP 冒烟 10 项通过；真实 HTTP 授权链路 16 项通过 |
| 6 插件适配器 | ⬜ 未开始 | — | — |
| 7 MCP 集成 | ⬜ 未开始 | — | — |
| 8 完整测试与交付 | ⬜ 未开始 | — | — |

### 阶段仓库

| 阶段 | 仓库地址 | 提交哈希 | 推送状态 |
|---|---|---|---|
| 1 | https://github.com/lzalookyou-stack/mcp-manager-stage1 | 交付提交 `ec27ff3eddf3458302746caefe1a5334ba78928e`；状态回填提交 `806c58c` | ✅ 已推送（远端 `refs/heads/main` 已回读核对） |
| 2 | https://github.com/lzalookyou-stack/mcp-manager-stage2 | 交付提交 `015fdcb521eea18abba32d940ba14fec9cc992ef`；状态回填提交 `b742dcb` | ✅ 已推送（远端 `refs/heads/main` 已回读核对，33 文件树经 API 核验） |
| 3 | https://github.com/lzalookyou-stack/mcp-manager-stage3 | 交付提交 `2d4522bece572ca836dde936d30d8da01f8e99c6`；状态回填提交 `2c8684c7b773ceb6781aabbe7009b7a86a457eb7` | ✅ 已推送（远端 `refs/heads/main` 已回读核对 = 本地 HEAD，45 blob / 11 tree，`truncated: false`） |
| 4 | https://github.com/lzalookyou-stack/mcp-manager-stage4 | 交付提交 `ca56e1a4ec0de9e955d323b1899c2b22989e406a`；状态回填提交 `1b59dfdbc74fdf36844690ce25e1e4b11d3f1c94` | ✅ 已推送（远端 `refs/heads/main` 已回读核对 = 本地 HEAD，48 blob，`truncated: false`） |
| 5 | https://github.com/lzalookyou-stack/mcp-manager-stage5 | 交付提交 `d4de5865e3d0246ae92810971a1e8136ba414bff` | ✅ 已推送（远端 `refs/heads/main` 已回读核对 = 本地 HEAD，62 blob / 12 tree，`truncated: false`，本地跟踪文件与远端文件树逐一比对无差异） |
| 6 | （推送后回填） | （推送后回填） | — |
| 7 | （推送后回填） | （推送后回填） | — |
| 8 | （推送后回填） | （推送后回填） | — |

---

## 阶段交付物

### 阶段 1（已完成）

- `docs/research.md`：6 个候选仓库的实测调研，结论按【已验证】/【推断】/【未检查】/【无法确定】分级。
- `docs/architecture.md`：分层架构、统一插件数据模型、可解释评分规则、安装事务状态机、威胁模型摘要、技术选型结论。
- `docs/plan.md`：阶段 1–8 实施计划与贯穿纪律。
- `scripts/research_github.py`：可复现的 GitHub 调研脚本（仅用标准库，不打印令牌，失败即记录不伪造）。
- `scripts/research_result.json`：本次调研的原始数据（2026-10-09 采集）。

### 阶段 2（已完成）

可运行骨架：**只读** Web 控制台 + **只读** MCP Server，共用同一 service 层。

应用代码：
- `app/config.py`：不可变 `Settings`；默认仅绑回环，非回环需 `MCPM_ALLOW_NON_LOOPBACK=1` 否则**启动即失败**。
- `app/models.py`：统一插件模型（7 种 kind）、四级证据枚举、五级风险枚举、可解释评分（security 权重 0.30，且安全一票否决时总分归零）、`pinned_ref` 强制为 40/64 位 commit（拒绝浮动引用）。
- `app/security.py`：安全标识符校验、`resolve_within`（防 `..` 穿越 / 绝对路径 / 符号链接逃逸）、常量时间比较、日志清洗。
- `app/db.py`：SQLite（stdlib，无 ORM），WAL、外键、`busy_timeout`、事务上下文管理器。
- `app/services/plugin_service.py`：注册 / 查询 / 列表 / 审计；**搜索、评分、审查、安装、回滚五个方法显式 `raise NotImplementedError`**。
- `app/web/app.py`：只读 API + Host 白名单 + Origin(CSRF) 校验 + 体积上限 + 统一安全响应头（严格 CSP，无 `unsafe-inline`）+ 关闭 docs/openapi。
- `app/mcp_server/server.py`：基于 `mcp.server.mcpserver.MCPServer`（**mcp 2.x 中 FastMCP 已改名**）注册 4 个工具；`request_install` 恒返回 `not_implemented` 并记 `denied` 审计。
- `web/index.html` + `web/assets/{style.css,app.js}`：原生前端，全程只用 `textContent`/`createElement` 写 DOM。
- `run_web.py` / `run_mcp.py`：启动入口。
- `scripts/smoke_mcp_stdio.py`：真实子进程 stdio JSON-RPC 冒烟测试。

测试：`tests/` 共 77 项（模型 / 安全 / 服务 / Web），含「无写接口」负向断言与「未实现能力必须抛异常」断言。

### 阶段 3（已完成）

**搜索 / 分析 / 评分 / 安全审查**，全部为本地可复现逻辑，不依赖在线模型。

新增模块：
- `app/search/github_client.py`：**仅用标准库 `urllib` 的只读** GitHub 客户端。`GitHubError` 带 `kind`（rate_limited / not_found / unauthorized / invalid / network / server / error）与限流元数据；`transport` 参数支持测试注入（不发真实请求）；令牌**只放 `Authorization` 头**，绝不写入日志或异常；单文件文本上限 `MAX_TEXT_BYTES = 512 KiB`，超限截断并标注。能力：`search_repositories` / `get_repository` / `get_latest_commit`（校验 40 位 SHA）/ `list_releases` / `list_directory` / `get_file`（base64 解码）。
- `app/search/query.py`：自然语言 → GitHub 检索式。含中英文停用词与同义扩展表（「长期记忆」→`memory`、「轻量」→`lightweight`、「依赖少」→`zero-dependencies`）；支持 `language` / `min_stars` / `pushed_after`（校验 `YYYY-MM-DD`）/ `topics` / `in_name`。**无有效关键词时显式报错**，绝不返回空检索式。`notes` 声明「检索式由本地规则生成，未使用任何在线模型」。模块头声明它**不是安全控制**。
- `app/analysis/security_review.py`：19 条静态检测规则（DL001–DL004 / SH001–SH003 / FS001–FS002 / CR001–CR002 / NET001–NET002 / AU001 / PS001 / DP001），输出带**文件 / 行号 / 片段 / 原因 / 证据等级**的 `Finding`；风险取最高严重度，存在 critical 即 `vetoed=True`。**报告始终携带 `SecurityReport.DISCLAIMER`**，且永不输出「绝对安全」类结论。
- `app/analysis/scoring.py`：可解释评分。维度权重 维护 20% / 质量 25% / 安全 30% / 兼容 15% / 社区 10%；维度内权重全部显式声明；安全维度**未审查时给 0 分并标【未检查】**（保守默认），已审查时按 `{NONE:0, LOW:15, MEDIUM:40, HIGH:70}` 扣分，`vetoed` 时总分归零；Star 仅影响社区维度 40% 以内；每条理由均带来源。
- `app/analysis/compare.py`：`compare_plugins` 输出 `{rows, highlights, notes}`，以**陈述性**方式点出证据缺口（存在 critical 被一票否决 / 未取得明确许可证 / 尚未锁定固定 commit / 存在未获取字段），**不做「最佳推荐」**。

改造：
- `app/models.py`：`Plugin` 增加 `fetched_at` / `missing_fields` / `security_report`；新增 `FindingCategory`、`Finding`、`SecurityReport`、`RepoMetrics`、`SearchQuery`、`CompareRow`。
- `app/services/plugin_service.py`：新增 `SearchUnavailable`（**未配置客户端时显式失败，绝不返回空列表**）；实现 `build_query` / `search_remote`（落库为 `review_status=pending` 候选并写 `search.remote` 审计）/ `inspect_remote`（固定 commit + README/测试/CI/依赖清单探测 + Release 数，失败项记入 `missing_fields`）/ `review`（**先确保有 `pinned_ref`，否则拒绝在浮动引用上给结论**；抓取失败记 `skipped_files`）/ `score` / `compare`；**校验顺序**：先做本地可判定的来源校验，再要求远程客户端可用。
- `app/runtime.py`：`Runtime.create(*, github=None)`，未显式传入时从 `MCPM_GITHUB_TOKEN` / `GITHUB_TOKEN` 构造客户端。

测试：新增 `tests/test_github_client.py`、`test_security_review.py`、`test_scoring.py`、`test_query.py`、`test_stage3_service.py`（全部用注入假 transport，**不发真实网络请求**）。累计 **154 项**。

### 阶段 4（已完成）

**本地网页控制台交互 + SSE 实时**。关键约束：在增加交互的同时，**仍然没有任何写接口**。

新增模块：
- `app/events.py`：进程内发布/订阅事件总线。单订阅队列上限 256（满则**保新弃旧**并计数）、并发订阅上限 32（超出 `subscribe()` 返回 `None` → 端点回 503）；`publish()` **绝不抛异常**（无事件循环 / 无订阅者 / 载荷不可序列化都只累加计数），避免通知故障拖垮主流程；支持 `bind_loop` + `call_soon_threadsafe` 跨线程投递。

改造：
- `app/services/plugin_service.py`：`__init__` 增加 `events` 参数；`audit()` 写库成功后广播 `audit` 事件；`search_remote` 广播 `search.started/finished/failed`；`review` 广播 `review.started/progress/finished`。**事件只含已脱敏的结构化字段**。
- `app/runtime.py`：`Runtime` 增加 `events: EventBus`；`Runtime.create(*, events=None)`。
- `app/web/app.py`：新增只读端点 `GET /api/query`（检索式预览，**不触网**）、`GET /api/search`、`GET /api/plugins/{id}/inspect`、`GET /api/plugins/{id}/review`、`GET /api/compare`、`GET /api/tasks`、`GET /api/events`（SSE）、`GET /api/events/stats`；新增异常映射：`SearchUnavailable` → **503**、`GitHubError` → **502（带 kind）**。SSE 含 15 秒心跳，断开即退订。
- `web/index.html`：四个标签页（总览 / 发现 / 安装 / 任务与历史）+ 搜索表单 + 候选表 + 详情面板。
- `web/assets/app.js`：全部用 `textContent`/`createElement`；所有请求均为 GET；搜索/检查/审查/对比按钮连接**真实后端**；SSE 用 `EventSource` 订阅并按事件类型渲染。
- `pytest.ini`：新增 `asyncio_mode = strict`；`requirements.txt` 锁定 `pytest-asyncio==1.3.0`。

测试：新增 `tests/test_events.py`（7 项）、`tests/test_web_stage4.py`（14 项）。累计 **175 项**。

**阶段 4 的 SSE 验证方式（重要）**：`TestClient` 的同步 `stream()` 在等待流式响应时无法再发第二个请求（会死锁，且连接永不结束）。因此 SSE 的推送验证改为：
1. 单元测试在 **ASGI 层**直接驱动（`app(scope, receive, send)`），真实验证 `event: audit` 与数据体；
2. 另有一次**真实 HTTP 端到端**验证（真实 uvicorn + 后台 `curl -N` 挂 SSE，同时触发搜索与审查），实际收到 19 条事件（含 `search.started` / `audit` / `search.finished` / `review.started` / `review.progress` / `review.finished`），且**未出现任何令牌或凭据**。

---

## 验证基线（阶段 5）

以下为**真实执行**得到的结果（非声称）：

1. `pytest -q -p no:cacheprovider`：**202 passed**（阶段 5 新增 26 项，位于 `tests/test_stage5_install.py`）。
2. `scripts/smoke_mcp_stdio.py`：**10 项 PASS，退出码 0**；其中包含关键安全断言「Agent 响应中不含任何确认令牌」。
3. `scripts/verify_stage5_http.sh`（真实 uvicorn + curl）：**16 项 PASS**，覆盖：
   - `GET /` 200 且 CSP 不含 `unsafe-inline`；
   - 无会话写请求 → 403 `session_rejected`；
   - 错误 CSRF → 403；无 Origin 的 POST → 403 `csrf_origin_rejected`；
   - 完整授权链路：会话 → 计划（`awaiting_confirmation`）→ 确认（拿到一次性令牌）→ 执行（`succeeded`）；
   - 已用令牌复用 → 409；
   - 只读接口不泄露令牌明文，且含步骤日志。

关键安全性质（已在测试中断言）：

- 计划生成**不产生任何文件写入**；
- 无固定 commit → 拒绝生成计划；安全审查 `vetoed` → 拒绝生成计划；
- 确认令牌：一次性、常量时间比较、有 TTL、绑定计划摘要（计划变化即失效）；
- 执行前**重新验证**令牌与当前计划一致；
- 失败即 `failed` 并回滚，绝不残留半成品；
- 崩溃遗留的 `running` / `rolling_back` 事务标记为 `interrupted`，**绝不视为成功**；
- 卸载只删除归属台账中登记且位于受管根目录内的文件（越界路径绝不删除）；
- `run_command` 拒绝字符串命令、拒绝越界 cwd、`shell=False` 下 `;` 不被解释为命令分隔符、超时可控。

---

## 未实现（**严禁声称已实现**）

以下能力在阶段 5 **确实不存在**，调用会显式失败（`NotImplementedError` / `SearchUnavailable` / `InstallUnavailable` 或返回 `not_found` / `plan_rejected`）：

- `PluginService` **不再提供** `install` / `rollback` / `uninstall`（阶段 5 已移除占位方法）。
  写操作的唯一入口是 `app.install.service.InstallService`，必须走「生成计划 → 用户通过受信任网页确认 → 校验令牌 → 执行」。
  没有任何代码路径可以绕过用户授权直接写入文件系统（`tests/test_plugin_service.py::test_write_methods_absent_from_plugin_service` 断言这些方法**不存在**）。
- `PluginService.search_remote` / `inspect_remote` / `review` / `score` / `compare`：**已在阶段 3 实现**；但在**未配置 GitHub 令牌**时 `search_remote` 会抛 `SearchUnavailable`（显式失败，**不会**返回空列表被误读为「没有结果」）。
- Web 层**已有写接口**（阶段 5 引入），但全部要求「同源 Origin + 会话 Cookie（HttpOnly/SameSite=Strict）+ CSRF 令牌」三件套；
  未授权访问一律 403（`session_rejected` / `csrf_origin_rejected`）。负向断言**已同步更新而非删除**：
  `tests/test_web.py::test_write_endpoints_require_authorization` 与 `tests/test_web_stage4.py::test_no_write_endpoints_still_true`。
- Web 层现有独立**操作表**（`operations` / `operation_logs` / `confirmations` / `snapshots` / `file_ownership`）：
  `/api/operations`（列表）与 `/api/operations/{id}`（详情 + 确认状态 + 步骤日志）为只读；`/api/tasks` 仍读取审计日志。
- MCP 工具 `request_install` **不产生任何安装行为**：它只生成「等待用户确认」的安装计划（`ok: true, status: awaiting_confirmation`）；
  未知条目返回 `not_found`，计划被拒返回 `plan_rejected`。Agent **无法**确认或执行，响应中**不含**任何确认令牌。
- 无插件适配器（阶段 6）。
- 计划中的 `commands` 恒为空：本系统**默认禁止**自动执行仓库内安装脚本。
- 本项目自身**尚无 LICENSE**（阶段 8 前确认）。
- 安全审查为**静态模式匹配**：必然存在漏报，未命中**不代表**安全。

---

## 已知缺口 / 风险

见 `docs/research.md` 第 3 节。摘要：
- agentbridge 的 scanner 实现与检出率：**未检查**。
- MCP Registry API 的破坏性变更时间表：**无法确定**（官方声明 preview + v0.1 freeze）。
- `tamb/simple-mcp-manager` 的真实许可证：**无法确定**（README 称 MIT，API 为 null）。

---

## 验证基线

阶段 1（可复跑）：

```bash
cd mcp-manager
python3 scripts/research_github.py --out scripts/research_result.json   # 应输出 6 个仓库的真实字段
```

阶段 2（可复跑，必须先建好 `.venv`）：

```bash
cd mcp-manager
.venv/bin/python -m pytest                                   # 期望 77 passed
.venv/bin/python scripts/smoke_mcp_stdio.py                  # 期望 SMOKE_EXIT=0，9 项 PASS
.venv/bin/python run_web.py &                                # 真实启动
curl -s http://127.0.0.1:8765/api/health                     # {"status":"ok",...}
```

阶段 2 的实测结果（2026-10-09）：

- `pytest`：**77 passed in 2.29s**。
- `smoke_mcp_stdio.py`：**9 项 PASS，退出码 0**；`tools/list` 返回 4 个工具；`request_install` 被拒绝（`not_implemented`）。
- Web 实机：`GET /` 200（含完整安全头与严格 CSP）、`/api/health` 200、`/api/stats` 200、`/api/plugins` 200、`/assets/app.js` 200、无 Origin 的 `POST /api/plugins` **403**、非法 Host **400**、`/docs` **404**。

> 环境说明：系统 Python 受 PEP 668 保护，**必须使用项目内 `.venv`**；依赖走清华镜像安装。
> `mcp 2.3.0` 实测：`FastMCP` 已改名为 `MCPServer`（`from mcp.server.mcpserver import MCPServer`），旧 `mcp.server.fastmcp` 导入会失败。

阶段 3（可复跑，必须先建好 `.venv`）：

```bash
cd mcp-manager
.venv/bin/python -m pytest                                   # 期望 154 passed
.venv/bin/python scripts/smoke_mcp_stdio.py                  # 期望 SMOKE_EXIT=0，9 项 PASS
.venv/bin/python run_web.py &                                # 真实启动
curl -s http://127.0.0.1:8765/api/stats                      # {"total":0,...}
```

阶段 3 的实测结果（2026-10-09）：

- `pytest`：**154 passed in 5.36s**。
- `smoke_mcp_stdio.py`：**9 项 PASS，退出码 0**（与阶段 2 一致；`request_install` 仍被拒绝为 `not_implemented`）。
- Web 实机（真实 uvicorn + 真实 curl）：`GET /` **200** 且携带严格 CSP（`default-src 'none'; script-src 'self'; …`，**不含 `unsafe-inline`**）与 `nosniff` / `DENY` / `no-referrer` / `same-origin`；无 Origin 的 `POST /api/plugins` **403 `csrf_origin_rejected`**；非法 Host **400 `invalid_host`**；`/docs` 与 `/openapi.json` 均 **404**；`/api/stats`、`/api/plugins` 均 **200**。
- 阶段 3 首轮曾出现 11 项失败，已全部定位并修复（见「阶段 3 修复记录」）。

### 阶段 3 修复记录（诚实留痕）

首轮 `pytest` 为 **11 failed / 140 passed**，逐项定位后确认：

1. **实现缺陷 1（免责声明未落地）**：`review_files` 从未把 `SecurityReport.DISCLAIMER` 写入 `notes`，与模块契约（「报告始终携带免责声明」）不符，测试因此在 `notes[-1]` 上 `IndexError`。→ 修实现：无论是否命中，均追加免责声明；未命中时额外说明「未命中不代表不存在风险」。
2. **实现缺陷 2（校验顺序错误）**：`inspect_remote` / `review` 先取远程客户端、后校验来源，导致「非 GitHub 源」被误报成 `SearchUnavailable`，掩盖真正根因。→ 修实现：先做本地可判定的来源校验，再要求客户端可用。
3. **测试数据缺陷（10 项）**：测试用单字符查询 `"x"`，被关键词过滤器（长度 < 2）丢弃后触发「检索式为空必须报错」这一**合理约束**。→ 修测试数据（改为 `"memory mcp"`），**未**放宽 `build_search_query` 的空检索式报错约束；并**新增**测试把该约束固化下来。
4. **测试断言过弱**：免责声明用例的 `assert A or B` 使其形同虚设。→ 改为强断言 `SecurityReport.DISCLAIMER in report.notes`。

> 全部修复均**未删除任何安全检查、未降低任何断言**；新增强断言与新增用例后，总数由 140 增至 154。

阶段 4（可复跑，必须先建好 `.venv`）：

```bash
cd mcp-manager
.venv/bin/python -m pytest                                   # 期望 175 passed
.venv/bin/python scripts/smoke_mcp_stdio.py                  # 期望 SMOKE_EXIT=0，9 项 PASS
.venv/bin/python run_web.py &                                # 真实启动
curl -s http://127.0.0.1:8765/api/events/stats               # {"subscribers":0,...}
curl -sN http://127.0.0.1:8765/api/events                    # 应收到 ": connected"
```

阶段 4 的实测结果（2026-10-09）：

- `pytest`：**175 passed in 6.94s**。
- `smoke_mcp_stdio.py`：**9 项 PASS，退出码 0**（与阶段 2/3 一致）。
- Web 实机（真实 uvicorn + 真实 curl）：`GET /` **200** 且含阶段 4 面板与严格 CSP（**不含 `unsafe-inline`**）；`GET /api/query` **200**（返回真实检索式与「未使用任何在线模型」声明）；`GET /api/search`（无令牌）**503 `search_unavailable`**；`GET /api/events/stats` **200**；`GET /api/events` 真实收到 `: connected`；无 Origin 的 `POST /api/search` **403**；`/docs` **404**。
- **SSE 端到端（真实 HTTP）**：后台 `curl -N` 挂 SSE 的同时触发真实搜索与审查，收到 **19 条真实事件**（`search.started` / `audit` / `search.finished` / `review.started` / `review.progress` ×6 / `review.finished`），事件体中**不含任何令牌或凭据**；`/api/events/stats` 在订阅挂起时显示 `subscribers: 1`。

> 说明：SSE 的单元测试**不能**用 `TestClient.stream()`（同步客户端在等流式响应时无法再发第二个请求，会死锁），因此改用 ASGI 层直接驱动 `app(scope, receive, send)`，并用上述真实 HTTP 端到端做补充验证。

---

## 纪律（不可违反）

1. **不伪造**仓库数据、测试结果、执行状态、安全结论。
2. 所有结论标注证据等级（`EvidenceLevel`：verified / inferred / unchecked / undetermined）。
3. **不以正则或提示词作为安全控制**；不为通过测试而删除安全检查或降低断言。
4. **Agent 只能提交操作申请，授权必须来自用户通过受信任网页的明确确认**（禁止 Agent 自造授权）。
5. 每阶段成果提交并推送到**新的独立 Git 仓库**。
6. 测试串行执行，避免 OOM（本机可用内存约 2.4 GB）。