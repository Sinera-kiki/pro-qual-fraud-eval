# 调度平台 自动上传接口

## 目的

调度平台 工作流完成取数后，直接上传 `.xlsx` / `.xls` / `.csv` 到本应用。服务会创建批次、异步运行 VLM，并将结果放入人工复核队列。

## 地址

`POST https://app.example.com/s/qual-hrm-review-backend/api/push/uploads`

## 鉴权

请求头必须携带：

```text
X-Push-Token: <HRM_AUTOMATION_UPLOAD_TOKEN>
```

Token 的 SHA-256 哈希已存入应用 PostgreSQL 配置表，不依赖 Platform Studio 环境变量。不得把 Token 写进 调度平台 脚本、SQL 或日志；请使用 调度平台 密钥配置。

## 请求格式

`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `file` | 文件 | 是 | `.xlsx`、`.xls` 或 `.csv`，最大 50MB |
| `short_name` | 文本 | 是 | 1–24 字；建议 `YYYYMMDD-行业-批次`，仅中文/字母/数字/横线/下划线 |
| `url_col` | 文本 | 否 | 图片 URL 列名。省略时按 `qualification_url`、`资质图URL`、`url`、`URL`、`图片URL`、`图片链接` 自动识别 |

文件内图片 URL 须为 `http` 或 `https` 开头。可选列：`user_id`、`trade_first_name`（或中文同义列）。

## 响应

成功：

```json
{"tag":"upload-20260903-金融","total_urls":1234}
```

批次创建成功后即开始异步 VLM 识别。可轮询：

`GET /api/uploads/{tag}/status`

VLM 完成且 `vlm_status=ready` 后，打开网站即可人工复核。

## curl 示例

```bash
curl -X POST 'https://app.example.com/s/qual-hrm-review-backend/api/push/uploads' \
  -H "X-Push-Token: $HRM_AUTOMATION_UPLOAD_TOKEN" \
  -F 'short_name=20260903-金融-01' \
  -F 'url_col=资质图URL' \
  -F 'file=@/path/to/high-risk-templates.xlsx'
```

## 错误码

- `401`：Token 无效
- `409`：同名批次已存在，请换 `short_name`
- `413`：文件超过 50MB
- `503`：应用鉴权配置暂不可用或缺失
