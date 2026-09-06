# 资质造假浓度评估看板

周度资质造假浓度评估的全流程管理与多维展示服务。

## 模块

- **管理控制台（`/`）**：展示当前周和历史周任务状态、阶段进度流水、产物上传/下载、跳转标注平台与提醒。
- **数据大盘（`/report/{run_id}`）**：造假浓度走势、行业违规分布、二级类目下钻、认证方式与入驻来源等维度图表。
- **Push API（`/api/push/...`）**：供离线管线与脚本调用的快照推送接口，基于 Token 鉴权。

## 目录结构

```
dashboard/
├── app.py              # FastAPI 后端（状态机、数据下钻、Push API）
├── report_page.html    # 数据大盘页面（ECharts 5）
├── frontend_v3.html    # 工作流管理面板页面
├── requirements.txt    # 依赖
└── README.md
```

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

可选用本地 PostgreSQL，未配置数据库时自动降级为演示模式：

```bash
export DB_HOST=localhost
export DB_PORT=5432
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_NAME=qual_dashboard
export WORKFLOW_PUSH_TOKEN=your_secure_push_token
```

### 3. 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 3000 --reload
```

访问：控制台 `http://localhost:3000/`，数据大盘 `http://localhost:3000/report`。