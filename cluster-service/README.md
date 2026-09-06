# 聚类跑批服务（cluster-service）

资质图片聚簇的跑批任务管理服务：接收聚簇需求，由后台跑批执行器取数、聚簇、回传产物。

## 组成

- **app.py**：跑批任务管理后端（FastAPI），提供任务领取 / 文件上传 / 产物回传 / 结果查询。
- **qual_cluster_runner.py**：跑批执行器，轮询领取任务 → 取数（Dataverse HiveSQL）→ 调 cluster_fast.py 聚簇 → 上传产物 → 回写结果。
- **frontend/**：任务管理页面。

## 目录结构

```
cluster-service/
├── app.py                  # 后端：领取任务 / 产物回传 / 结果回写
├── qual_cluster_runner.py  # 跑批执行器
├── init_db.py              # 数据库初始化
├── selfcheck_backend.py    # 后端自检
├── frontend/               # 任务管理页面
│   ├── index.html
│   └── xlsx.full.min.js
├── requirements.txt
└── README.md
```

## 环境变量

参考根目录 `.env.example`：

| 变量 | 说明 |
| --- | --- |
| `CLUSTER_SERVICE_URL` | 跑批服务后端地址 |
| `WORKFLOW_RUNNER_TOKEN` | 跑批执行器鉴权 Token |
| `RUNNER_TOKEN_FILE` | 鉴权 Token 文件路径（未设 WORKFLOW_RUNNER_TOKEN 时使用） |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | PostgreSQL 连接信息 |

## 快速启动

```bash
pip install -r requirements.txt
python init_db.py
uvicorn app:app --host 0.0.0.0 --port 3000 --reload
```