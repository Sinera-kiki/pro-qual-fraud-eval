"""
Automation integration router.
面向调度平台任务节点的机器接口——不走 SSO，用固定 API Token 鉴权。
Header: X-API-Token: <token>（在 api_tokens 表预置）
"""
from __future__ import annotations

import csv
import io
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse


REQUIRED_COLUMNS = {"user_id", "qualification_url", "cluster_id"}
VALID_CHANNELS = {"evaluation", "review"}


def _validate_channel_automation(channel: str) -> str:
    if channel not in VALID_CHANNELS:
        raise HTTPException(400, f"无效的通道：{channel}（可选：evaluation / review）")
    return channel


def build_automation_router(get_pool):
    """get_pool: 复用 app.py 里的连接池 getter。"""

    automation = APIRouter(prefix="/api/v1/automation", tags=["automation"])

    async def _validate_token(request: Request) -> str:
        token = request.headers.get("x-api-token") or request.headers.get("X-API-Token")
        if not token:
            raise HTTPException(401, "缺少 X-API-Token 请求头")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token FROM api_tokens WHERE token = $1",
                token,
            )
            if not row:
                raise HTTPException(401, "无效的 API Token")
            await conn.execute(
                "UPDATE api_tokens SET last_used_at = NOW() WHERE token = $1",
                token,
            )
        return token

    @automation.get("/health")
    async def automation_health():
        """调度平台接入自检；不需要 token。"""
        return {"status": "ok", "service": "qual-annotation-automation"}

    @automation.post("/datasets")
    async def upload_dataset(
        request: Request,
        file: UploadFile = File(..., description="聚类后的 CSV"),
        name: Optional[str] = Form(None),
        channel: str = Form("evaluation"),
    ):
        """【AUTO-1】上传 CSV，返回 dataset_id。
        channel: 'evaluation'（默认，兼容旧任务）| 'review'（审核通道）"""
        await _validate_token(request)
        _validate_channel_automation(channel)

        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise HTTPException(400, "CSV 文件为空或无法解析")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise HTTPException(400, f"CSV 缺少必要列：{', '.join(sorted(missing))}")

        rows = list(reader)
        if not rows:
            raise HTTPException(400, "CSV 文件没有数据行")

        items = []
        for i, row in enumerate(rows, start=2):
            try:
                cluster_id = int(row["cluster_id"])
            except (ValueError, KeyError):
                raise HTTPException(400, f"第 {i} 行 cluster_id 不是有效整数")
            items.append({
                "user_id": row.get("user_id", "").strip(),
                "qualification_url": row.get("qualification_url", "").strip(),
                "trade_first_name": row.get("trade_first_name", "").strip(),
                "trade_second_name": row.get("trade_second_name", "").strip(),
                "cluster_id": cluster_id,
            })

        cluster_ids = {item["cluster_id"] for item in items if item["cluster_id"] != -1}
        dataset_id = str(uuid.uuid4())
        filename = file.filename or "automation_upload.csv"
        ds_name = name or filename

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO datasets (id, name, filename, total_rows, cluster_count, channel)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    dataset_id, ds_name, filename, len(items), len(cluster_ids), channel,
                )
                await conn.executemany(
                    """
                    INSERT INTO dataset_items
                        (dataset_id, user_id, qualification_url, trade_first_name, trade_second_name, cluster_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    [
                        (dataset_id, it["user_id"], it["qualification_url"],
                         it["trade_first_name"], it["trade_second_name"], it["cluster_id"])
                        for it in items
                    ],
                )
                # 审核通道上传后自动灼 cluster_pool（供抢单）
                if channel == "review":
                    valid = sorted(c for c in cluster_ids if c != -1)
                    if valid:
                        await conn.executemany(
                            """
                            INSERT INTO cluster_pool (dataset_id, cluster_id)
                            VALUES ($1, $2)
                            ON CONFLICT (dataset_id, cluster_id) DO NOTHING
                            """,
                            [(dataset_id, cid) for cid in valid],
                        )

        return {
            "dataset_id": dataset_id,
            "name": ds_name,
            "filename": filename,
            "total_rows": len(items),
            "cluster_count": len(cluster_ids),
            "channel": channel,
        }

    @automation.get("/datasets")
    async def list_datasets(request: Request, channel: Optional[str] = None):
        """【AUTO-2】列出数据集；channel 缺省返回所有通道，可选 evaluation / review。"""
        await _validate_token(request)
        if channel is not None:
            _validate_channel_automation(channel)
        pool = await get_pool()
        async with pool.acquire() as conn:
            if channel is None:
                rows = await conn.fetch(
                    """
                    SELECT id AS dataset_id, name, filename, total_rows, cluster_count, channel, created_at
                    FROM datasets
                    ORDER BY created_at DESC
                    """
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id AS dataset_id, name, filename, total_rows, cluster_count, channel, created_at
                    FROM datasets
                    WHERE channel = $1
                    ORDER BY created_at DESC
                    """,
                    channel,
                )
        return {"datasets": [
            {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in rows
        ]}

    @automation.get("/datasets/{dataset_id}/progress")
    async def get_progress(dataset_id: str, request: Request):
        """【AUTO-3】查询标注进度。"""
        await _validate_token(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            ds = await conn.fetchrow(
                "SELECT id, name, total_rows, cluster_count, channel, created_at FROM datasets WHERE id = $1",
                dataset_id,
            )
            if not ds:
                raise HTTPException(404, "数据集不存在")
            total_clusters = await conn.fetchval(
                "SELECT COUNT(DISTINCT cluster_id) FROM dataset_items WHERE dataset_id = $1 AND cluster_id != -1",
                dataset_id,
            ) or 0
            annotated_clusters = await conn.fetchval(
                "SELECT COUNT(*) FROM cluster_annotations WHERE dataset_id = $1",
                dataset_id,
            ) or 0

        pending = total_clusters - annotated_clusters
        percent = round(100.0 * annotated_clusters / total_clusters, 2) if total_clusters > 0 else 0.0
        return {
            "dataset_id": dataset_id,
            "name": ds["name"],
            "channel": ds["channel"],
            "total_rows": ds["total_rows"],
            "total_clusters": total_clusters,
            "annotated_clusters": annotated_clusters,
            "pending_clusters": pending,
            "progress_percent": percent,
            "is_completed": total_clusters > 0 and pending == 0,
            "created_at": ds["created_at"].isoformat() if ds["created_at"] else None,
        }

    @automation.get("/datasets/{dataset_id}/result")
    async def download_result(dataset_id: str, request: Request, force: bool = False):
        """【AUTO-4】下载标注结果 CSV。默认必须 100% 完成才能下载。"""
        await _validate_token(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            ds = await conn.fetchrow("SELECT id, name FROM datasets WHERE id = $1", dataset_id)
            if not ds:
                raise HTTPException(404, "数据集不存在")

            if not force:
                total_clusters = await conn.fetchval(
                    "SELECT COUNT(DISTINCT cluster_id) FROM dataset_items WHERE dataset_id = $1 AND cluster_id != -1",
                    dataset_id,
                ) or 0
                annotated_clusters = await conn.fetchval(
                    "SELECT COUNT(*) FROM cluster_annotations WHERE dataset_id = $1",
                    dataset_id,
                ) or 0
                if total_clusters == 0 or annotated_clusters < total_clusters:
                    raise HTTPException(
                        409,
                        f"标注未完成（{annotated_clusters}/{total_clusters}），不可下载；如需强制下载请传 force=true",
                    )

            rows = await conn.fetch(
                """
                SELECT
                    di.user_id,
                    di.qualification_url,
                    di.trade_first_name,
                    di.trade_second_name,
                    di.cluster_id,
                    ca.remark_first,
                    ca.remark_second,
                    COALESCE(ca.annotator_email, '') AS annotator_email,
                    CASE
                        WHEN il.qualification_url IS NOT NULL THEN '通过'
                        WHEN ca.remark_first = '不违规' THEN '通过'
                        ELSE '违规'
                    END AS image_label
                FROM dataset_items di
                LEFT JOIN cluster_annotations ca
                    ON ca.dataset_id = di.dataset_id AND ca.cluster_id = di.cluster_id
                LEFT JOIN item_labels il
                    ON il.dataset_id = di.dataset_id AND il.qualification_url = di.qualification_url
                WHERE di.dataset_id = $1
                ORDER BY di.cluster_id, di.id
                """,
                dataset_id,
            )

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["user_id", "qualification_url", "trade_first_name", "trade_second_name",
                        "cluster_id", "remark_first", "remark_second", "annotator_email", "image_label"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (v if v is not None else "") for k, v in dict(r).items()})

        content = output.getvalue()
        safe_name = ds["name"].replace(".csv", "").replace(" ", "_")
        filename = f"{safe_name}_annotated.csv"
        from urllib.parse import quote
        encoded_filename = quote(filename, safe="")

        return StreamingResponse(
            iter([content.encode("utf-8-sig")]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
        )

    @automation.delete("/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, request: Request):
        """【AUTO-5】删除数据集（级联清理）。"""
        await _validate_token(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM datasets WHERE id = $1", dataset_id)
        if result == "DELETE 0":
            raise HTTPException(404, "数据集不存在")
        return {"ok": True, "dataset_id": dataset_id}

    @automation.delete("/datasets_by_date/{biz_date}")
    async def delete_datasets_by_date(biz_date: str, request: Request, channel: Optional[str] = None):
        """【AUTO-5b】按日期批量删除数据集。
        biz_date 格式：YYYY-MM-DD（匹配 created_at 的日期部分）。
        可选 ?channel=evaluation|review 过滤只删某通道。
        返回删除的数量和被删的 dataset_id 列表。
        """
        await _validate_token(request)
        if channel is not None:
            _validate_channel_automation(channel)
        pool = await get_pool()
        async with pool.acquire() as conn:
            if channel is None:
                rows = await conn.fetch(
                    """
                    DELETE FROM datasets
                    WHERE created_at::date::text = $1
                    RETURNING id, name, channel
                    """,
                    biz_date,
                )
            else:
                rows = await conn.fetch(
                    """
                    DELETE FROM datasets
                    WHERE created_at::date::text = $1 AND channel = $2
                    RETURNING id, name, channel
                    """,
                    biz_date, channel,
                )
        return {
            "deleted_count": len(rows),
            "deleted": [
                {"dataset_id": str(r["id"]), "name": r["name"], "channel": r["channel"]}
                for r in rows
            ],
        }

    @automation.delete("/datasets_by_name")
    async def delete_datasets_by_name(request: Request, channel: Optional[str] = None):
        """【AUTO-5c】按 name 关键字模糊匹配批量删除数据集。
        Body: {"name_contains": "测试"}  或  {"name_contains": "联调"}
        可选 ?channel=xxx 过滤。
        """
        await _validate_token(request)
        body = await request.json()
        kw = (body or {}).get("name_contains", "").strip()
        if not kw or len(kw) < 2:
            raise HTTPException(400, "name_contains 至少 2 个字符")
        if channel is not None:
            _validate_channel_automation(channel)
        pool = await get_pool()
        async with pool.acquire() as conn:
            if channel is None:
                rows = await conn.fetch(
                    """
                    DELETE FROM datasets
                    WHERE name ILIKE '%' || $1 || '%'
                    RETURNING id, name, channel
                    """,
                    kw,
                )
            else:
                rows = await conn.fetch(
                    """
                    DELETE FROM datasets
                    WHERE name ILIKE '%' || $1 || '%' AND channel = $2
                    RETURNING id, name, channel
                    """,
                    kw, channel,
                )
        return {
            "deleted_count": len(rows),
            "keyword": kw,
            "deleted": [
                {"dataset_id": str(r["id"]), "name": r["name"], "channel": r["channel"]}
                for r in rows
            ],
        }

    return automation
