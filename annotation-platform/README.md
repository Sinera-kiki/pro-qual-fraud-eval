# 资质聚簇标注平台

面向资质图片聚簇场景的人工复核标注平台。审核人员按图片簇逐簇复核，打标后导出结果用于浓度计算。

## 功能

- **簇级复核**：同一簇的多个账号聚合呈现，逐簇判定，避免孤立看图无法判断是否同版复用。
- **逐图细看**：支持放大查看、逐图确认公章、二维码、拍摄角度等关键特征。
- **先整理后打标**：与簇明显无关的图片可先移出或删除，再对整理后的簇打标。
- **两级标签**：一级标签（实锤造假 / 疑似造假 / 资质挂靠 / 不违规）+ 二级标签细分造假类型。
- **双通道**：评估通道（周度浓度评估）与审核通道（抢单制常态审核）。
- **自动化任务对接**：提供标准 REST API（`X-API-Token` 鉴权），支持自动化创建数据集、查询进度、导出结果。

## 目录结构

```
annotation-platform/
├── backend/                  # 后端服务（FastAPI + asyncpg + PostgreSQL）
│   ├── app.py                # 数据集管理、标注流转与导出 API
│   ├── automation_router.py        # 自动化任务对接 API
│   ├── init_db.py            # 数据库初始化脚本
│   ├── requirements.txt      # 后端依赖
│   └── AUTOMATION_API.md           # 自动化任务接口协议说明
├── frontend/                 # 前端应用（React 18 + TypeScript + Vite + Tailwind CSS）
│   ├── src/
│   │   ├── App.tsx           # 主界面
│   │   ├── main.tsx
│   │   └── index.css
│   ├── package.json
│   ├── vite.config.ts
│   └── tailwind.config.js
└── README.md
```

## 快速启动

### 1. 后端

```bash
cd backend
pip install -r requirements.txt

# 初始化数据库结构
export DB_HOST=localhost
export DB_PORT=5432
export DB_USER=postgres
export DB_PASSWORD=postgres
export DB_NAME=qual_annotation

python init_db.py

# 启动服务
uvicorn app:app --host 0.0.0.0 --port 3001 --reload
```

### 2. 前端

```bash
cd ../frontend
npm install
npm run dev
```

构建生产版本：

```bash
npm run build
```