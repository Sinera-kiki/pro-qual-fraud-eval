# 高危模板自动入库（high-risk-template）

把资质造假聚簇结果中的实锤簇代表图，自动入库到高危模板库，供入驻拦截策略复用，形成"发现 → 拦截"的能力沉淀。

## 整体链路

```
聚簇结果 / 在线表格 / 本地文件
  ↓ 拉取候选图
挑簇代表图（离簇质心最近的 embedding）
  ↓
VLM 剔纯电子版（只 100% 确定的纯 PDF / 白底截图 / 无拍摄痕迹）
  ↓
跨周期去重（同底版跳过）
  ↓
转存内部对象存储
  ↓
入库高危模板库
  ↓
校验 + 明细写盘（失败可回滚）
```

## 组成

- **high_risk_media_bot.py**：入库机器人，核心链路为「代表图选择 → VLM 剔电子版 → 跨周期去重 → 转存 → 入库 → 校验」。
- **app.py**：人工复核后端，读 runs 目录的 VLM 判定结果在前端网格展示，人工勾选保留/剔除后，同进程调用机器人完成入库。
- **frontend/**：复核页面。
- **AUTOMATION_UPLOAD_API.md**：自动化上传接口协议。

## 目录结构

```
high-risk-template/
├── app.py                    # 人工复核后端
├── high_risk_media_bot.py    # 入库机器人
├── init_db.py                # 数据库初始化（review_decisions + submit_runs）
├── frontend/                 # 复核页面
│   └── index.html
├── AUTOMATION_UPLOAD_API.md  # 自动化上传接口协议
├── requirements.txt
└── README.md
```

## 环境变量

参考根目录 `.env.example`：

| 变量 | 说明 |
| --- | --- |
| `SSO_COOKIE_FILE` | 内部接口 SSO cookie 文件路径 |
| `MITM_CA_PATH` | 自签证书 CA 路径 |
| `HRM_AUTOMATION_UPLOAD_TOKEN` | 自动化上传接口鉴权 Token |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | PostgreSQL 连接信息 |

> VLM 判定依赖运行时注入的 AI 网关配置（base_url + api_key），未配置时判定结果返回 unknown，对应簇默认保留入库。

## 快速启动

```bash
pip install -r requirements.txt
python init_db.py
uvicorn app:app --host 0.0.0.0 --port 3000 --reload
```