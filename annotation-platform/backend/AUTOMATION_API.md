# 资质聚类标注平台 · 调度平台集成接口文档

> **面向**：调度平台自动化任务节点  
> **发布地址**：http://localhost:3001  
> **API Base URL**：`http://localhost:3001/api/v1/automation`  
> **版本**：v1.0.1（2026-07-29）

---

## 1. 鉴权

所有 调度平台接口（除 `/health`）都必须携带 `X-API-Token` 请求头。

```
X-API-Token: <token>
```

### 默认 Token

首次部署时数据库预置了一个默认 Token，调度平台可立即使用：

```
automation-default-token-please-rotate-in-prod
```

> ⚠️ **上线前请轮换**。轮换方式：连数据库 `INSERT INTO api_tokens (token, name) VALUES ('<新 token>', '<用途备注>');`，然后在 调度平台侧改配置。旧 token 可继续用直到 `DELETE FROM api_tokens WHERE token='<旧>';`

### 认证错误

| HTTP 状态 | 情况 |
|---|---|
| 401 | 缺少 `X-API-Token` 请求头 |
| 401 | Token 不在 `api_tokens` 表中 |

---

## 2. 接口列表

| # | 方法 | 路径 | 用途 |
|---|---|---|---|
| AUTO-0 | GET | `/health` | 接入自检（无需 token） |
| AUTO-1 | POST | `/datasets` | 上传 CSV → 创建数据集 |
| AUTO-2 | GET | `/datasets` | 列出所有数据集 |
| AUTO-3 | GET | `/datasets/{dataset_id}/progress` | 查询标注进度 |
| AUTO-4 | GET | `/datasets/{dataset_id}/result` | 下载标注结果 CSV |
| AUTO-5 | DELETE | `/datasets/{dataset_id}` | 删除数据集 |

---

## 3. 详细说明

### AUTO-0：接入自检

```
GET /api/v1/automation/health
```

**Response 200**
```json
{ "status": "ok", "service": "qual-annotation-automation" }
```

用途：调度平台网络探活。**无需 token**。

---

### AUTO-1：上传 CSV

```
POST /api/v1/automation/datasets
Content-Type: multipart/form-data
X-API-Token: <token>
```

**Form 字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | file | ✅ | 聚类后的 CSV 文件（UTF-8 或 UTF-8-BOM） |
| `name` | string | ❌ | 数据集自定义名称；缺省用 CSV 文件名 |

**CSV 列要求**

| 列名 | 必填 | 说明 |
|---|---|---|
| `user_id` | ✅ | 专业号用户 ID |
| `qualification_url` | ✅ | 资质图片 URL |
| `cluster_id` | ✅ | 聚类 ID（整数；`-1` 表示噪声/未分簇，不计入标注） |
| `trade_first_name` | ❌ | 一级行业名 |
| `trade_second_name` | ❌ | 二级行业名 |

**Response 200**
```json
{
  "dataset_id": "6f3c8e9a-1234-4a12-9c8b-abcdef012345",
  "name": "0729_finance_cluster.csv",
  "filename": "0729_finance_cluster.csv",
  "total_rows": 1580,
  "cluster_count": 42
}
```

**Response 400**：CSV 格式错误 / 缺少必要列 / 空文件 / cluster_id 非整数

**curl 示例**
```bash
curl -X POST 'http://localhost:3001/api/v1/automation/datasets' \
  -H 'X-API-Token: automation-default-token-please-rotate-in-prod' \
  -F 'file=@./cluster_result.csv' \
  -F 'name=金融行业_2026W30'
```

---

### AUTO-2：列出数据集

```
GET /api/v1/automation/datasets
X-API-Token: <token>
```

**Response 200**
```json
{
  "datasets": [
    {
      "dataset_id": "6f3c8e9a-1234-4a12-9c8b-abcdef012345",
      "name": "金融行业_2026W30",
      "filename": "cluster_result.csv",
      "total_rows": 1580,
      "cluster_count": 42,
      "created_at": "2026-07-29T20:15:33+00:00"
    }
  ]
}
```

按 `created_at` 倒序返回，最新的在最前。

---

### AUTO-3：查询标注进度

```
GET /api/v1/automation/datasets/{dataset_id}/progress
X-API-Token: <token>
```

**Response 200**
```json
{
  "dataset_id": "6f3c8e9a-1234-4a12-9c8b-abcdef012345",
  "name": "金融行业_2026W30",
  "total_rows": 1580,
  "total_clusters": 42,
  "annotated_clusters": 42,
  "pending_clusters": 0,
  "progress_percent": 100.00,
  "is_completed": true,
  "created_at": "2026-07-29T20:15:33+00:00"
}
```

| 字段 | 说明 |
|---|---|
| `total_clusters` | 有效簇总数（不含 cluster_id=-1） |
| `annotated_clusters` | 已完成标注的簇数量 |
| `pending_clusters` | 剩余待标注簇 |
| `progress_percent` | 完成百分比（保留 2 位小数） |
| **`is_completed`** | **调度平台主要判断字段**：`true` = 100% 完成可下载 |

**Response 404**：数据集不存在

**调度平台轮询建议**：`is_completed == true` 后再调 AUTO-4 下载。建议轮询间隔 ≥ 5 分钟。

---

### AUTO-4：下载标注结果 CSV

```
GET /api/v1/automation/datasets/{dataset_id}/result[?force=true]
X-API-Token: <token>
```

**Query 参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `force` | bool | `false` | `false` 时未 100% 完成会返回 409；`true` 强制下载当前已标注部分 |

**Response 200**
- `Content-Type: text/csv`
- `Content-Disposition: attachment; filename*=UTF-8''<name>_annotated.csv`
- Body：UTF-8-BOM 编码的 CSV

**CSV 列**

| 列名 | 说明 |
|---|---|
| `user_id` | 原样 |
| `qualification_url` | 原样 |
| `trade_first_name` / `trade_second_name` | 原样（可能为空） |
| `cluster_id` | 原簇 ID |
| `remark_first` | 一级标签：实锤造假 / 疑似造假 / 资质挂靠 / 不违规 |
| `remark_second` | 二级细分标签 |
| **`image_label`** | **最终判定**：`通过` / `违规`（调度平台下游主要用这一列） |

**image_label 判定规则**
- 该图被人工标记为「不违规」→ `通过`
- 所在簇的一级标签是「不违规」→ `通过`
- 其他 → `违规`

**Response 404**：数据集不存在
**Response 409**：标注未完成且未传 `force=true`

**curl 示例**
```bash
curl -f -OJ \
  -H 'X-API-Token: automation-default-token-please-rotate-in-prod' \
  'http://localhost:3001/api/v1/automation/datasets/6f3c8e9a-1234-4a12-9c8b-abcdef012345/result'
```

---

### AUTO-5：删除数据集

```
DELETE /api/v1/automation/datasets/{dataset_id}
X-API-Token: <token>
```

**Response 200**
```json
{ "ok": true, "dataset_id": "6f3c8e9a-1234-4a12-9c8b-abcdef012345" }
```

会级联删除该数据集的所有数据行 / 簇标注 / 图片粒度标记。

---

## 4. 调度平台任务节点推荐编排

### 调度策略（重要）

- **开始时机**：每周二 06:00 启动任务
- **完成则直接下载**：首次轮询如果 `is_completed == true`，直接调 AUTO-4 下载后继续后续处理
- **未完成则小时级轮询**：未完成时每小时轮询一次进度直至 100%，然后下载

### 调度平台DAG 参考

```
每周二 06:00 cron
    │
    ▼
【节点 A】定位本周数据集（上游聚类产出后递交的 dataset_id）
    │
    ▼
【节点 B】AUTO-3 查进度（首次）
    │
    ▼
    is_completed == true ?
    ├── yes ──▶ 【节点 D】AUTO-4 下载结果 ──▶ 后续处理
    └── no  ──▶ 【节点 C】轮询循环（间隔 1h）
                        │
                        ▼
                     AUTO-3 再查
                        │
                        ▼
                     is_completed ?
                        ├── yes ──▶ 节点 D 下载
                        └── no  ──▶ 等 1h 后重新进入节点 C
```

### 调度平台配置建议

| 项 | 取值 | 说明 |
|---|---|---|
| 任务启动 cron | `0 6 * * 2` | 每周二 06:00 |
| 首次进度查询 | 启动后立即运行 | 一旦完成可立即跳过轮询 |
| 轮询间隔 | 3600 秒（即 1 小时） | 人工标注是小时级任务，无需更密 |
| 总时长兜底 | 72–96h | 防止人工时长很长时一直占任务槽；到时告警不失败 |
| 409 处理 | 不算失败，继续轮询 | 409 = 还没标完，不是错误 |
| 401 / 5xx | 重试 2–3 次后告警 | 避免瞬时重试饱和 |

### 伪代码（供构建 调度平台DAG 参考）

```python
# 节点 B/C：轮询进度
def poll_progress(dataset_id, token):
    r = http.get(
        f"{BASE}/api/v1/automation/datasets/{dataset_id}/progress",
        headers={"X-API-Token": token},
    ).json()
    return r["is_completed"], r["progress_percent"]

# 节点 D：下载
def download_result(dataset_id, token, dst_path):
    r = http.get(
        f"{BASE}/api/v1/automation/datasets/{dataset_id}/result",
        headers={"X-API-Token": token},
    )
    r.raise_for_status()
    open(dst_path, "wb").write(r.content)
```

---

## 5. 错误码汇总

| HTTP | 场景 | 处理 |
|---|---|---|
| 400 | CSV 格式错 / 缺列 / 空 | 检查上游产物是否符合列约定 |
| 401 | Token 缺失或无效 | 检查 `X-API-Token` 请求头 |
| 404 | dataset_id 不存在 | 确认 dataset_id 未被误删 |
| 409 | 标注未 100% 完成 | 继续轮询 AUTO-3，或传 `force=true` |
| 5xx | 平台故障 | 重试；持续失败联系产品 |

---

## 6. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0.2 | 2026-07-29 | 调度策略更新：每周二 06:00 启动，未完成每小时轮一次 |
| v1.0.1 | 2026-07-29 | 新增 `/api/v1/automation/*` 调度平台集成接口 |
| v1.0.0 | 2026-07-29 | 初始发布（前端 SSO 通道） |
---

## 7. v1.1 双通道（评估 / 审核）

### 背景

平台从 v1.1 开始支持两个业务通道：

| 通道 | channel 值 | 权限 | 用途 |
|---|---|---|---|
| 评估 | `evaluation` | 任意 SSO 用户 | 调度平台每周二例行评估任务（现有流程） |
| 审核 | `review` | 仅审核员白名单 | 人工审核，含任务分配管理 |

**两个通道数据物理隔离**（同一张表按 channel 字段区分），源头独立，不做自动衔接。

### 调度平台接口兼容性总览

| 接口 | v1.0 行为 | v1.1 变更 |
|---|---|---|
| POST /datasets | 上传 | 新增 form 字段 channel，默认 evaluation（**旧任务无需改动**） |
| GET /datasets | 列全部 | 新增 query 参数 ?channel=evaluation\|review 可过滤；返回体新增 channel 字段 |
| GET /datasets/{id}/progress | 返回进度 | 返回体新增 channel 字段 |
| GET /datasets/{id}/result | 下载 CSV | CSV 新增 annotator_email 列，记录每簇由谁标注（审核通道场景） |
| DELETE /datasets/{id} | 删除 | 无变更 |

**兼容性承诺**：现有 调度平台任务不带 channel 参数时，行为完全不变（等价于 channel=evaluation）。

### 审核通道 调度平台任务模板

如果审核通道也要接 调度平台，参考评估通道的 cron 模板即可，只需在上传时指定 channel=review。

调度建议由业务方按需设置（例如每日一次，或每周三 06:00 起动）。轮询节奏同评估通道：首次即查，未完成每小时轮一次。

### 审核通道下载的完成语义

审核通道的 is_completed 仍以"数据集所有簇（cluster_id≠-1）都被标注"为准，与评估通道口径一致。不区分是谁标的，只看全量。导出 CSV 会带 annotator_email 一列，调度平台拿到后可再按人分组或统计工作量。

### 审核员管理

审核员由数据库表 review_members 维护，追加人员的 SQL：

```sql
-- 普通审核员
INSERT INTO review_members (email, display_name, is_admin)
VALUES ('user1@example.com', '姓名', false)
ON CONFLICT (email) DO NOTHING;

-- 管理员（可分配任务）
INSERT INTO review_members (email, display_name, is_admin)
VALUES ('admin@example.com', '姓名', true)
ON CONFLICT (email) DO NOTHING;
```

- 普通审核员：只能看/标"分配给自己"的簇
- 管理员：能看/标全部簇，且能在网页上做任务分配（"均分"）

### 审核通道任务分配

**MVP 只支持"均分"策略**：管理员在数据集卡片上点"分配任务"按钮 → 选一批审核员 → 系统按 cluster_id 顺序 round-robin 均分。旧分配会被覆盖。

管理型 API（SSO admin 调用；调度平台不需要）：

- GET  /api/review/members — 列出所有审核员
- GET  /api/review/datasets/{id}/assignments — 查看某数据集的分配情况和每人进度
- POST /api/review/datasets/{id}/assignments — 创建/更新分配（strategy=even 传 assignees；strategy=manual 传 assignments 映射）
- DELETE /api/review/datasets/{id}/assignments/{email} — 撤销某人的分配

### 变更记录（新）

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.1.0 | 2026-07-31 | 双通道支持：加 channel 字段隔离评估/审核；审核通道加白名单+任务分配；导出 CSV 加 annotator_email 列 |
---

## 8. v1.2 抢单模式（推翻 v1.1 白名单+预分配）

### 变更概述

| 维度 | v1.1（旧） | v1.2（当前） |
|---|---|---|
| 审核通道权限 | 白名单 review_members 才能入 | **任意 SSO 用户都能入** |
| 任务分配 | 管理员用 modal 均分预分配 | **抢单式**：审核员进入数据集时系统自动分配一簇 |
| 完成继续 | 手动选下一簇 | **保存后自动领下一簇** |
| 已标注簇修改 | 允许 | **禁止**（v1.2 起簇标完即锁） |
| 并发风险 | 无 | **`SELECT ... FOR UPDATE SKIP LOCKED` 保证同一簇不会被两人同时拿到** |

### 底层机制

新表 cluster_pool（审核数据集上传时自动灌入）：

```sql
CREATE TABLE cluster_pool (
    dataset_id     UUID    NOT NULL,
    cluster_id     INT     NOT NULL,
    assignee_email TEXT,           -- NULL = 未认领
    claimed_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,    -- !=NULL 表示已完成且锁死
    PRIMARY KEY (dataset_id, cluster_id)
);
```

抢单 SQL（事务内，毫秒级完成）：

```sql
BEGIN;
SELECT cluster_id FROM cluster_pool
WHERE dataset_id = $1 AND assignee_email IS NULL AND completed_at IS NULL
ORDER BY cluster_id LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE cluster_pool SET assignee_email = $2, claimed_at = NOW()
WHERE dataset_id = $1 AND cluster_id = $picked;
COMMIT;
```

- FOR UPDATE：并发的另一个事务如果也想拿同一行会阻塞
- SKIP LOCKED：并发的另一个事务不阻塞，直接跳过被锁的行去拿下一个
- 事务只 hold 到 commit，不 hold 到人工标注完成

### 已知限制（下次迭代）

- **认领超时未做自动释放**：审核员认领后关掉浏览器，簇会挂在他名下直到主动 release 或系统清理。当前需 SQL 手工释放：`UPDATE cluster_pool SET assignee_email=NULL, claimed_at=NULL WHERE dataset_id=... AND cluster_id=... AND completed_at IS NULL;`
- **图片粒度剔除（item_labels）**：审核通道下可用；同一图片被剔除后，会计入结果 CSV 的 image_label=通过。

### 新增内部 SSO 接口（调度平台不涉及，供前端使用）

- POST /api/review/datasets/{id}/claim_next — 抢下一个未认领的簇
- GET  /api/review/datasets/{id}/my_task — 查我当前的簇
- POST /api/review/datasets/{id}/release — 主动释放当前认领

### 调度平台接口影响

**没有影响。** 调度平台上传接口和结果下载接口签名都不变：

- POST /api/v1/automation/datasets ?channel=review — 审核通道数据集上传时会自动灌 cluster_pool
- GET /api/v1/automation/datasets/{id}/result — 结果 CSV 仍带 annotator_email 列

### 变更记录（新）

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.2.0 | 2026-07-31 | 审核通道推翻白名单+预分配，改为任何 SSO 可入 + 抢单模式 + 标完即锁；并发用 FOR UPDATE SKIP LOCKED 兜底 |
