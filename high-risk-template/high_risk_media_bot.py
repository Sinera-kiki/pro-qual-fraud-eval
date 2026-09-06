#!/usr/bin/env python3
"""高危模板机器人 — 资质造假聚簇结果 → 高危媒体库自动入库

四种入口取候选图：
  --from-cluster  聚簇结果 CSV（annotated 表 + 聚簇全表 + embedding 表）
  --from-online   在线 REDoc 表格 URL 列
  --from-xlsx     本地 xlsx 文件
  --urls-file     一行一个 URL 的文本文件

链路（每张候选图）：
  Step0 拉取候选池
  Step1 挑簇代表图（离簇中心 embedding 最近的那张；仅 --from-cluster 模式）
  Step2 VLM 剔纯电子版（口径：只 100% 确定的纯 PDF/白底截图/无拍摄痕迹）
  Step3 跨周期去重（POST isRepeated，同底版跳过）
  Step4 转存内部 COS（POST upload_file_by_url/image）
  Step5 入库（POST /api/thor/highriskmedia/sample）
  Step6 校验 + 明细写盘（+ 失败可 --rollback）

签发：SSO cookie 来自 os.path.expanduser(os.environ.get("SSO_COOKIE_FILE", "~/.config/pqfe/sso.json"))；VLM 复用 ps_detect/vlm_batch 判定思路。

固化规则（2026-09-02 用户拍板）：
  1) 代表图 = pixel 相似度中心图（embedding 最靠近簇质心）
  2) 代表图是电子版 → 整簇跳过
  3) VLM 严格度 = 只剔纯电子版（宁保留不误剔）
  4) 去重 = 跨周期（先调 isRepeated 对全库比对）

用法示例：
  # 从 W35 实锤簇入库
  python high_risk_media_bot.py --from-cluster \\
    --annotated pro_qual_cluster_review/w35/annotated_w35.csv \\
    --cluster-full pro_qual_cluster_review/w35/专业号资质聚簇_2026W35_0824-0830.csv \\
    --embedding pro_qual_cluster_review/w35/full_account_embedding.csv \\
    --week-tag W35 --dry-run

  # 从在线表格拉 URL 列
  python high_risk_media_bot.py --from-online <REDoc-sheet-url> --url-col 资质图URL
"""
from __future__ import annotations
import argparse
import base64
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

# ------------------------- 常量 -------------------------

SSO_PATH = os.path.expanduser(os.environ.get("SSO_COOKIE_FILE", "~/.config/pqfe/sso.json"))
BASE_RISK = "https://risk.example.com"
BASE_MOSS = "https://moss.example.com"
MITM_CA_PATH = os.environ.get("MITM_CA_PATH", "~/.config/pqfe/ca-cert.pem")

# SOP step7 全部枚举（已 2026-09-02 摸接口确认）
SOP_DEFAULTS = {
    "type": 3,                # 图片
    "match_type": 1,          # 同源图
    "business_type": 5,       # 商业化
    "primary_label": 185,     # 资质造假
    "secondary_label": 900372,# 官方主体资质模版
    "level": 20,              # 中
    "apply_way": 20,          # 待审
    "status": 1,              # 启用
    "store_type": 0,
    "auto_check_back": False, # 自动回查关闭
}

# 跨周期去重阈值：pixel 相似度 score >= 该阈值即视为同底版跳过
# （isRepeated 返回值 0~1，越高越像；实测同一张图 >=0.95，模板变体 >=0.85）
DEDUPE_SCORE_THRESHOLD = 0.85

# VLM 单张判定最长等待秒；超时视为 unknown，保留入库
VLM_TIMEOUT_S = 60

# VLM 与图片 CDN 均为网络 I/O，串行处理会让总耗时随图片数线性增长。
# 保持小并发以换取吞吐，不抢占 Pod 资源也避免打满 Runway 配额。
VLM_MAX_CONCURRENCY = 3
IMAGE_DOWNLOAD_MAX_CONCURRENCY = 8
VLM_RETRY_ATTEMPTS = 3

# 每批入库大小（POST /sample 是单条，控制并发防限流）
BATCH_SIZE = 20
BATCH_INTERVAL_S = 0.3

# remark_second 白名单：默认放开（None = 不过滤）。
# 记忆里"剔除二维码完全一致"是针对机审信号的规避，对入高危模板库来说，
# 二维码完全一致=同底版营业执照 恰恰是最标准的模板。留给用户看代表图后再定。
FAKE_ROOT_WHITELIST: set[str] | None = None


# ------------------------- 工具 -------------------------

def load_sso() -> dict:
    """加载 SSO 凭证；优先级：
    1. 环境变量 HRM_SSO_COOKIE（+ HRM_SSO_EMAIL / HRM_SSO_DISPLAY_NAME）
       —— platform 容器场景，由后端从访问者请求转发进来
    2. SSO_PATH JSON 文件 —— openclaw 主机场景
    """
    env_cookie = os.environ.get("HRM_SSO_COOKIE")
    if env_cookie:
        return {
            "cookie": env_cookie,
            "email": os.environ.get("HRM_SSO_EMAIL", ""),
            "display_name": os.environ.get("HRM_SSO_DISPLAY_NAME", ""),
        }
    with open(SSO_PATH) as f:
        d = json.load(f)
    return {
        "cookie": d["cookieHeader"],
        "email": d["user"]["email"],
        "display_name": d["user"]["displayName"],
    }


def session_with_sso(sso: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Cookie": sso["cookie"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    if os.path.exists(MITM_CA_PATH):
        s.verify = MITM_CA_PATH
    return s


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level:5s} {msg}", flush=True)


def parse_embedding(s: str) -> list[float] | None:
    """embedding csv 的 embeds 列是 JSON 字符串；有的 legacy 数据带引号"""
    if not s or s.lower() in ("nan", "null", "none"):
        return None
    try:
        v = json.loads(s)
        if isinstance(v, list) and v and isinstance(v[0], (int, float)):
            return [float(x) for x in v]
    except Exception:
        pass
    return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def centroid(vecs: list[list[float]]) -> list[float]:
    if not vecs:
        return []
    dim = len(vecs[0])
    c = [0.0] * dim
    for v in vecs:
        for i in range(dim):
            c[i] += v[i]
    n = len(vecs)
    return [x / n for x in c]


# ------------------------- 入口：候选拉取 -------------------------

def load_from_cluster(
    annotated_csv: str,
    cluster_full_csv: str,
    embedding_csv: str,
) -> list[dict]:
    """从聚簇产物拉每个实锤簇的代表图候选池。

    返回 [{cluster_id, user_id, qualification_url, trade_first_name, trade_second_name}...]
    只包含"实锤造假簇"的**所有图**（不仅是标注的 141 行）——挑代表在整簇里挑。
    """
    log(f"读取 annotated: {annotated_csv}")
    annot = _read_csv(annotated_csv)
    log(f"读取 cluster_full: {cluster_full_csv}")
    full = _read_csv(cluster_full_csv)

    # 找实锤簇
    fake_cids = set()
    for r in annot:
        if r.get("remark_first") == "实锤造假":
            if FAKE_ROOT_WHITELIST is None or r.get("remark_second") in FAKE_ROOT_WHITELIST:
                fake_cids.add(str(r.get("cluster_id")))

    log(f"实锤簇 cluster_id ({len(fake_cids)}个): {sorted(fake_cids, key=lambda x: (len(x), x))}")

    # 从全簇表拉这些簇的所有账号图（挑代表在整簇里挑）
    cands = []
    for r in full:
        if str(r.get("cluster_id")) in fake_cids:
            cands.append({
                "cluster_id": str(r["cluster_id"]),
                "user_id": r["user_id"],
                "qualification_url": r["qualification_url"],
                "trade_first_name": r.get("trade_first_name", ""),
                "trade_second_name": r.get("trade_second_name", ""),
            })
    log(f"候选池（实锤簇内所有图）: {len(cands)} 行")
    return cands


def load_from_online(sheet_url: str, url_col: str = "资质图URL") -> list[dict]:
    """在线 REDoc 直读暂未接入 HTTP API；请在页面导出后上传 xlsx/csv。

    原先依赖 shell 中的 `hi` CLI，Platform 容器不具备该依赖且会触发 subprocess
    安全扫描，因此明确禁用该路径，不再静默降级。
    """
    raise SystemExit("暂不支持 REDoc 在线表格直读，请导出 xlsx/csv 后上传")


def load_from_xlsx(xlsx_path: str, url_col: str = "资质图URL") -> list[dict]:
    """从本地 xlsx 读一列 URL"""
    try:
        import openpyxl
    except ImportError:
        raise SystemExit("需要 openpyxl：pip install openpyxl")
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    cands = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c or "") for c in rows[0]]
        try:
            col_idx = header.index(url_col)
        except ValueError:
            log(f"sheet '{ws.title}' 没有列 '{url_col}'，跳过", "WARN")
            continue
        for row in rows[1:]:
            if col_idx < len(row) and row[col_idx]:
                url = str(row[col_idx]).strip()
                if url.startswith("http"):
                    cands.append({"qualification_url": url})
    log(f"从 xlsx {xlsx_path} 读到 {len(cands)} 个 URL")
    return cands


def load_from_urls_file(path: str) -> list[dict]:
    cands = []
    with open(path) as f:
        for line in f:
            u = line.strip()
            if u.startswith("http"):
                cands.append({"qualification_url": u})
    log(f"从 URL 文件读到 {len(cands)} 个 URL")
    return cands


def _read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_from_input_csv(csv_path: str, tag_prefix: str = "upload") -> list[dict]:
    """读后端预处理的 input.csv —— 每行是一张候选图。
    列约定：qualification_url(必填), user_id(选), trade_first_name(选), trade_second_name(选)
    每张图直接生成一个 rep（不聚簇），cluster_id = {tag_prefix}-{idx}
    """
    reps = []
    for idx, row in enumerate(_read_csv(csv_path)):
        url = (row.get("qualification_url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        reps.append({
            "cluster_id": f"{tag_prefix}-{idx}",
            "user_id": (row.get("user_id") or "").strip(),
            "qualification_url": url,
            "trade_first_name": (row.get("trade_first_name") or "").strip() or None,
            "trade_second_name": (row.get("trade_second_name") or "").strip() or None,
            "_pick_reason": "upload-single",
            "_cluster_size": 1,
            "_ranked_urls": [url],
        })
    log(f"从 input.csv 读到 {len(reps)} 张候选图（不聚簇，每张一个 rep）")
    return reps


# ------------------------- Step1 挑代表图 -------------------------

def pick_representatives(cands: list[dict], embedding_csv: str) -> list[dict]:
    """按簇分组，挑离质心最近的那张作为代表图。

    embedding_csv 里 (user_id, qualification_url) 唯一确定一张图的向量。
    """
    log(f"读取 embedding: {embedding_csv}")
    # 只加载 candidates 涉及的 uid+url，节省内存
    wanted = {(r["user_id"], r["qualification_url"]) for r in cands}
    emb_map: dict[tuple[str, str], list[float]] = {}
    with open(embedding_csv, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (r["user_id"].strip('"'), r["qualification_url"].strip('"'))
            if key in wanted:
                v = parse_embedding(r["embeds"])
                if v:
                    emb_map[key] = v
    log(f"embedding 命中: {len(emb_map)}/{len(wanted)}")

    # 按 cluster_id 分组
    groups: dict[str, list[dict]] = {}
    for r in cands:
        groups.setdefault(r["cluster_id"], []).append(r)

    reps = []
    for cid, items in groups.items():
        # 只保留有 embedding 的
        with_emb = [(r, emb_map.get((r["user_id"], r["qualification_url"]))) for r in items]
        with_emb = [(r, v) for r, v in with_emb if v]
        if not with_emb:
            log(f"cluster {cid}: 没有任何图有 embedding，跳过", "WARN")
            continue
        vecs = [v for _, v in with_emb]
        c = centroid(vecs)
        # 找离质心最近（cosine 最大）
        best = max(with_emb, key=lambda x: cosine(x[1], c))
        best_row = dict(best[0])
        best_row["_pick_reason"] = f"cluster {cid} centroid pick from {len(items)}"
        best_row["_cluster_size"] = len(items)
        # 备选：按距离质心降序留 top3，以便代表图被剔电子版时 fallback（本项目不 fallback 按用户规则）
        ranked = sorted(with_emb, key=lambda x: -cosine(x[1], c))
        best_row["_ranked_urls"] = [r["qualification_url"] for r, _ in ranked[:5]]
        reps.append(best_row)

    log(f"挑出代表图: {len(reps)} 张（对应 {len(reps)} 个簇）")
    return reps


# ------------------------- Step2 VLM 剔电子版 -------------------------

def build_vlm_prompt(today: str) -> str:
    return f"""你是资质图审核专家。请判定这张图片**是否为纯电子版**。

【当前实际日期是 {today}】
【判定标准】仅当以下**全部**特征命中时才判为「纯电子版」：
  1. 图片背景是纯白色或纯色（无纸张纤维、无光影、无拍摄环境）
  2. 图片没有任何拍摄痕迹（无手指、无桌面、无阴影、无反光、无角度倾斜）
  3. 图片明显是 PDF 截图 / 电子证书截屏 / 系统导出的电子版式

【重要】
- 高仿电子版式但**明显是纸质翻拍**（能看到纸张质感/角度/光影）→ 不算电子版
- 模棱两可、看不清的 → 不算电子版（保留原则：宁保留不误剔）
- 只有 100% 确定是电子版才判 true

严格输出 JSON，不要任何多余文字：
{{
  "is_electronic": true/false,
  "confidence": "high"/"medium"/"low",
  "reason": "一句话说明判定依据"
}}
"""


def load_vlm_verdicts(state_dir: Path) -> dict:
    """从 batches.json 里读 VLM 判定结果，返回 {url: verdict}"""
    p = state_dir / "batches.json"
    if not p.exists():
        return {}
    batches = json.loads(p.read_text())
    out = {}
    for b in batches:
        vlm = b.get("vlm_result") or []
        for i, row in enumerate(b.get("rows", [])):
            r = next((x for x in vlm if x.get("idx") == i + 1), None) or {}
            if r:
                out[row["qualification_url"]] = r
    return out


def filter_electronic(reps: list[dict], verdicts: dict, strict: bool) -> tuple[list[dict], list[dict]]:
    """过滤纯电子版；用户规则：代表图是电子版 → 整簇跳过。

    verdicts: {url: {is_electronic, confidence, reason}}
    strict=True 时把 confidence=low 也当电子版；默认 strict=False（只剔 100% 确定的纯电子版）
    """
    passed = []
    skipped = []
    for r in reps:
        v = verdicts.get(r["qualification_url"], {})
        r["_vlm_verdict"] = v
        is_e = v.get("is_electronic", False)
        conf = v.get("confidence", "").lower()
        should_skip = is_e and (strict or conf == "high")
        if should_skip:
            r["_skip_reason"] = f"代表图纯电子版 [{conf}] {v.get('reason','')}→ 整簇跳过"
            skipped.append(r)
            log(f"跳过 cluster {r.get('cluster_id')}: {r['_skip_reason']}", "WARN")
        else:
            passed.append(r)
    log(f"VLM 剔电子版: 通过 {len(passed)} / 跳过 {len(skipped)}（strict={strict}）")
    return passed, skipped


def build_batches_for_vlm(reps: list[dict], out_dir: Path, batch_size: int = 10):
    """把代表图分批写 batches.json 供 image 工具批量判定"""
    out_dir.mkdir(parents=True, exist_ok=True)
    batches = []
    for i in range(0, len(reps), batch_size):
        chunk = reps[i:i + batch_size]
        batches.append({
            "idx": len(batches),
            "rows": [{
                "cluster_id": r.get("cluster_id"),
                "user_id": r.get("user_id"),
                "qualification_url": r["qualification_url"],
                "trade_first_name": r.get("trade_first_name", ""),
            } for r in chunk],
            "vlm_result": None,
        })
    p = out_dir / "batches.json"
    p.write_text(json.dumps(batches, ensure_ascii=False, indent=2))
    log(f"生成 {len(batches)} 批 VLM 判定任务 → {p}")
    log(f"下一步：用 image 工具逐批判定，把 vlm_result 回写到 batches.json；")
    log(f"       每批 prompt 参考 out_dir/prompt.txt；")
    log(f"       返回结构 {{idx, is_electronic:bool, confidence:high|medium|low, reason:str}}")
    (out_dir / "prompt.txt").write_text(build_vlm_prompt(datetime.now().strftime("%Y年%m月%d日")))


# ------------------------- Auto VLM（可选） -------------------------
# Platform Guard 容器只能通过平台注入的 ai.properties 调用 Runway；不能复用
# OpenClaw 进程里的模型网关地址或鉴权。文本模型由 ai.api_key 在网关侧绑定，
# 支持视觉输入的 Bedrock 模型会对图片做电子版资质图判定。


def _load_ai_props(path: str = "ai.properties") -> dict[str, str]:
    props: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return props


def _local_media_type(local_path: str) -> str:
    suffix = Path(local_path).suffix.lower()
    return {
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/jpeg")


# 允许下载图片的 CDN 域（防 file:// / SSRF；semgrep B310 的真实防护）
ALLOWED_IMAGE_HOSTS = (
    "cdn.example.com",
    "cdn.example.com",
    "cdn.example.com",
    "picasso-private-1251524319.cos.ap-shanghai.myqcloud.com",
    "risk-private-1251524319.cos.ap-shanghai.myqcloud.com",
)


def _is_allowed_image_url(url: str) -> bool:
    """只放行 https + 白名单主机。阻断 file:// / ftp:// / 任意外部域。"""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)
    except Exception:
        return False


def download_reps_locally(reps: list[dict], cache_dir: Path) -> None:
    """把每个 rep 下到本地 cache_dir/{sha256}.jpg，回填 rep['_local']。

    安全处理：
    - 文件名指纹用 SHA-256（非密码学用途，仅作为稳定映射，但统一避免 MD5/SHA1 告警）
    - HTTP 请求改用 requests（而非 urllib），避免 file:// scheme 风险告警
    - URL 已经 _is_allowed_image_url 白名单预检（scheme + host）
    """
    import hashlib
    import requests
    cache_dir.mkdir(parents=True, exist_ok=True)
    def download_one(r: dict) -> tuple[str | None, str | None]:
        url = r["qualification_url"]
        # 仅用于把 URL 映射成稳定的本地缓存文件名（非密码学用途）。统一用 SHA-256，避免扫描器告警。
        digest = hashlib.sha256(url.encode()).hexdigest()[:32]
        dst = cache_dir / f"{digest}.jpg"
        if dst.exists() and dst.stat().st_size > 0:
            return str(dst), None
        # 域白名单预检：只允许公司图片 CDN，阻断 file:// 与任意外部主机
        if not _is_allowed_image_url(url):
            return None, "URL 不在图片 CDN 白名单"
        try:
            # requests 不支持 file:// scheme，与 URL 白名单双保险；始终校验证书。
            # 在 Pod 的代理环境中使用受管 CA，其他环境走系统 CA。
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
                verify=MITM_CA_PATH if os.path.exists(MITM_CA_PATH) else True,
            )
            resp.raise_for_status()
            dst.write_bytes(resp.content)
            return str(dst), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    ok = fail = 0
    workers = min(IMAGE_DOWNLOAD_MAX_CONCURRENCY, max(1, len(reps)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="img-download") as pool:
        futures = {pool.submit(download_one, r): r for r in reps}
        for future in as_completed(futures):
            r = futures[future]
            try:
                local, error = future.result()
            except Exception as e:
                local, error = None, f"{type(e).__name__}: {e}"
            r["_local"] = local
            if local:
                ok += 1
            else:
                fail += 1
                log(f"下载失败 cid={r.get('cluster_id')}: {error}", "WARN")
    log(f"下载完成: {ok} 成功 / {fail} 失败")


def vlm_classify_one(local_path: str, prompt: str) -> dict:
    """单张图片通过 Runway Bedrock 视觉模型判定。

    失败 / 无法解析 时返回 unknown（is_electronic=False, confidence=low），
    上游 filter_electronic 默认只剔 confidence=high 的，因此 unknown 会保留入库。
    """
    try:
        b64 = base64.b64encode(Path(local_path).read_bytes()).decode()
    except Exception as e:
        return {"is_electronic": False, "confidence": "low", "reason": f"read_local_fail: {e}"}
    ai = _load_ai_props()
    ai_base_url = ai.get("ai.base_url")
    ai_api_key = ai.get("ai.api_key")
    if not (ai_base_url and ai_api_key):
        return {
            "is_electronic": False,
            "confidence": "low",
            "reason": "vlm_error: runway_ai_not_configured",
        }
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _local_media_type(local_path),
                    "data": b64,
                },
            },
            {"type": "text", "text": prompt},
        ]}],
    }
    import requests
    last_error = "unknown"
    for attempt in range(VLM_RETRY_ATTEMPTS):
        try:
            resp = requests.post(
                f"{ai_base_url.rstrip('/')}/bedrock_runtime/model/invoke",
                json=body,
                headers={"token": ai_api_key, "Content-Type": "application/json"},
                timeout=VLM_TIMEOUT_S,
                verify=MITM_CA_PATH if os.path.exists(MITM_CA_PATH) else True,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"http_{resp.status_code}"
                if attempt < VLM_RETRY_ATTEMPTS - 1:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                return {"is_electronic": False, "confidence": "low", "reason": f"vlm_error: {last_error}"}
            resp.raise_for_status()
            data = resp.json()
            if data.get("Code") or data.get("Error"):
                return {"is_electronic": False, "confidence": "low", "reason": "vlm_error: runway_business_error"}
            content = data["content"][0]["text"].strip()
            break
        except requests.RequestException as e:
            last_error = type(e).__name__
            if attempt < VLM_RETRY_ATTEMPTS - 1:
                time.sleep(1.2 * (attempt + 1))
                continue
            return {"is_electronic": False, "confidence": "low", "reason": f"vlm_error: {last_error}"}
        except Exception as e:
            return {"is_electronic": False, "confidence": "low", "reason": f"vlm_error: {type(e).__name__}"}
    # 抠 JSON。模型可能返回带围栏的 ```json ... ``` 或纯 JSON。
    txt = content
    if "```" in txt:
        # 拿最长的 ``` 块
        parts = txt.split("```")
        # 找形如 json xxx 或直接 { 的段
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                txt = p; break
    # 兜底找 { ... }
    if not txt.startswith("{"):
        l = txt.find("{"); r = txt.rfind("}")
        if l >= 0 and r > l:
            txt = txt[l:r+1]
    try:
        obj = json.loads(txt)
        return {
            "is_electronic": bool(obj.get("is_electronic", False)),
            "confidence": str(obj.get("confidence", "low")).lower(),
            "reason": str(obj.get("reason", ""))[:200],
        }
    except Exception as e:
        return {"is_electronic": False, "confidence": "low", "reason": f"parse_fail: {content[:100]}"}


def auto_run_vlm(state_dir: Path) -> None:
    """自动跑 VLM：下载图 → 逐张判定 → 回写 batches.json。"""
    batches_path = state_dir / "batches.json"
    reps_path = state_dir / "reps.json"
    if not batches_path.exists() or not reps_path.exists():
        raise SystemExit(f"缺少 {batches_path} 或 {reps_path}，先跑 build_batches_for_vlm")

    reps = json.loads(reps_path.read_text())
    download_reps_locally(reps, state_dir / "img_cache")
    # 回写 reps.json（含 _local）
    reps_path.write_text(json.dumps(reps, ensure_ascii=False, indent=2))
    url_to_local = {r["qualification_url"]: r.get("_local") for r in reps}

    batches = json.loads(batches_path.read_text())
    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = build_vlm_prompt(today)

    tasks = []
    for bi, b in enumerate(batches):
        for i, row in enumerate(b.get("rows", []), start=1):
            local = url_to_local.get(row["qualification_url"])
            tasks.append((bi, i, row, local))

    total_rows = len(tasks)
    log(f"开始自动 VLM 判定：{total_rows} 张（Runway Bedrock 视觉模型，并发={VLM_MAX_CONCURRENCY}）")
    results_by_task: dict[tuple[int, int], dict] = {}
    done = elec = 0
    workers = min(VLM_MAX_CONCURRENCY, max(1, total_rows))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vlm") as pool:
        futures = {
            pool.submit(vlm_classify_one, local, prompt): (bi, i, row)
            for bi, i, row, local in tasks if local
        }
        for bi, i, row, local in tasks:
            if not local:
                results_by_task[(bi, i)] = {
                    "idx": i, "is_electronic": False,
                    "confidence": "low", "reason": "no_local_image",
                }
        for future in as_completed(futures):
            bi, i, row = futures[future]
            try:
                v = future.result()
            except Exception as e:
                v = {"is_electronic": False, "confidence": "low", "reason": f"vlm_error: {type(e).__name__}"}
            v["idx"] = i
            results_by_task[(bi, i)] = v
            done += 1
            if v.get("is_electronic") and v.get("confidence") == "high":
                elec += 1
            log(f"  [{done}/{total_rows}] batch{bi} idx{i} cid={row.get('cluster_id')} "
                f"→ electronic={v['is_electronic']} conf={v['confidence']} — {v['reason'][:60]}")

    for bi, b in enumerate(batches):
        b["vlm_result"] = [
            results_by_task[(bi, i)]
            for i, _row in enumerate(b.get("rows", []), start=1)
        ]
    batches_path.write_text(json.dumps(batches, ensure_ascii=False, indent=2))
    log(f"✓ 自动 VLM 完成：{elec}/{total_rows} 判为纯电子版（high 置信）")


# ------------------------- Step3 去重 -------------------------

def dedupe_against_library(reps: list[dict], sess: requests.Session) -> tuple[list[dict], list[dict]]:
    """跨周期去重：调 isRepeated 对全库比对。

    注意：isRepeated 需要**内部 COS URL**，所以要先转存。
    这里改成：先转存 → 再去重（Step3 和 Step4 合并处理）。
    """
    # 该函数实际执行放在 upload_and_dedupe 里合并
    return reps, []


def upload_to_cos(sess: requests.Session, external_url: str) -> dict:
    """外链转存到内部 COS。返回 {url, success, err}"""
    r = sess.post(
        f"{BASE_MOSS}/fe_api/moss/common/upload_file_by_url/image",
        json={
            "originUrl": [external_url],
            "uploadOption": {
                "urlSuffix": "",
                "filePath": "risk-black",
                "maxFileSize": 31457280,
                "isPrivate": True,
            },
        },
        timeout=60,
    )
    r.raise_for_status()
    d = r.json()
    inner = d.get("data", {}).get("data", [{}])[0]
    return {
        "cos_url": inner.get("url"),
        "success": inner.get("success", False),
        "err": inner.get("errorMsg", ""),
        "raw": d,
    }


def is_repeated(sess: requests.Session, cos_url: str) -> dict:
    """跨周期去重：返回 {duplicate: bool, top_score: float, similar: [...]}"""
    r = sess.post(
        f"{BASE_RISK}/api/thor/highriskmedia/batch/isRepeated",
        json={
            "type": 3,
            "store_type": 0,
            "url_list": [{"url": cos_url}],
            "image_type": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    items = list(d.get("data", {}).values())
    similars = items[0] if items else []
    if not similars:
        return {"duplicate": False, "top_score": 0.0, "similar": []}
    top = max(similars, key=lambda s: s.get("score", 0.0))
    return {
        "duplicate": top.get("score", 0.0) >= DEDUPE_SCORE_THRESHOLD,
        "top_score": top.get("score", 0.0),
        "similar": similars[:5],
    }


# ------------------------- Step5 入库 -------------------------

def submit_sample(
    sess: requests.Session,
    cos_url: str,
    file_name: str,
    remark: str,
) -> dict:
    """POST /sample 单条新增"""
    payload = {
        **SOP_DEFAULTS,
        "url": cos_url,
        "file_name": file_name,
        "remark": remark,
        "url_list": [cos_url],
        "file_name_list": [file_name],
        "video_id_list": [None],
        "processed_images": [],
        "enable_time_range": [None, None],
        "tag_id_list": [],
        "tag_id": None,
        "pic_id": None,
        "video_id": "",
        "effect_scope": None,
        "event_id_list": [],
        "score": None,
        "partial_task_edit": False,
        "status_repository": 0,
        "edit": False,
    }
    r = sess.post(
        f"{BASE_RISK}/api/thor/highriskmedia/sample",
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def verify_and_get_id(sess: requests.Session, file_name: str) -> dict | None:
    """按 file_name 精确查列表拿 id + pic_id"""
    r = sess.post(
        f"{BASE_RISK}/api/thor/highriskmedia/samplelist/query",
        json={
            "file_name_like": file_name,
            "business_types": [5],
            "page_size": 5,
            "page_no": 1,
        },
        timeout=30,
    )
    r.raise_for_status()
    items = r.json().get("data", {}).get("items", [])
    for it in items:
        if it.get("file_name") == file_name:
            return {"id": it["id"], "pic_id": it.get("pic_id")}
    return None


def delete_sample(sess: requests.Session, sample_id: int) -> bool:
    r = sess.delete(
        f"{BASE_RISK}/api/thor/highriskmedia/sample?id={sample_id}",
        timeout=30,
    )
    return r.status_code == 200 and r.json().get("success")


# ------------------------- 主链路 -------------------------

def gen_file_name(week_tag: str, cluster_id: str | None, user_id: str, seq: int) -> str:
    """命名规则：{yyyymmdd}_{week_tag}_c{cid}_{u短}_{seq}

    upload run 的 cluster_id 形如 "upload-西安黑产模版-17"，与 week_tag 完全重复，
    这种情况只保留末尾的序号（c17），避免 file_name 里 tag 出现两遍。
    """
    ymd = datetime.now().strftime("%Y%m%d")
    parts = [ymd]
    if week_tag:
        parts.append(week_tag)
    if cluster_id is not None:
        cid = str(cluster_id)
        # 去掉与 week_tag 重复的前缀（upload run 场景）
        if week_tag and cid.startswith(week_tag + "-"):
            cid = cid[len(week_tag) + 1:]
        parts.append(f"c{cid}")
    if user_id:
        parts.append(user_id[-6:])
    parts.append(str(seq).zfill(3))
    return "_".join(parts)


def gen_remark(rep: dict, week_tag: str) -> str:
    """备注字段一律留空（2026-09-03 用户要求）。
    原来会自动拼 "W35 簇#6(size=40) 房地产 uid=xxxxxx"，对上传批次没意义且冗余。
    归属信息保留在 file_name 里（已含日期 + 周期/批次 tag + 序号），无需重复。
    """
    return ""


def process_one(
    rep: dict,
    sess: requests.Session,
    week_tag: str,
    seq: int,
    dry_run: bool,
) -> dict:
    """一条候选图完整走完 Step3~5，返回执行结果"""
    result = {
        **rep,
        "step": "start",
        "cos_url": None,
        "duplicate": None,
        "dedupe_score": None,
        "sample_id": None,
        "pic_id": None,
        "file_name": None,
        "error": None,
    }
    ext_url = rep["qualification_url"]
    file_name = gen_file_name(
        week_tag=week_tag,
        cluster_id=rep.get("cluster_id"),
        user_id=rep.get("user_id", ""),
        seq=seq,
    )
    remark = gen_remark(rep, week_tag)
    result["file_name"] = file_name
    result["remark"] = remark

    try:
        # Step4 转存
        result["step"] = "upload"
        up = upload_to_cos(sess, ext_url)
        if not up["success"]:
            result["error"] = f"upload failed: {up['err']}"
            return result
        result["cos_url"] = up["cos_url"]

        # Step3 去重
        result["step"] = "dedupe"
        dd = is_repeated(sess, up["cos_url"])
        result["duplicate"] = dd["duplicate"]
        result["dedupe_score"] = dd["top_score"]
        result["similar_preview"] = dd["similar"][:2]
        if dd["duplicate"]:
            result["error"] = f"跨周期重复（top_score={dd['top_score']:.3f} >= {DEDUPE_SCORE_THRESHOLD}）"
            return result

        if dry_run:
            result["step"] = "dry-run-ok"
            return result

        # Step5 入库
        result["step"] = "submit"
        _ = submit_sample(sess, up["cos_url"], file_name, remark)
        # 服务端可能返回 success=false 但实际写入了，必须回读
        time.sleep(0.5)
        v = verify_and_get_id(sess, file_name)
        if v:
            result["sample_id"] = v["id"]
            result["pic_id"] = v["pic_id"]
            result["step"] = "done"
        else:
            result["error"] = "submit 后回读列表未找到该 file_name"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def rollback(sess: requests.Session, results: list[dict]):
    """删除所有本次入库的记录"""
    ids = [r["sample_id"] for r in results if r.get("sample_id")]
    log(f"回滚 {len(ids)} 条入库记录")
    for sid in ids:
        ok = delete_sample(sess, sid)
        log(f"  DELETE id={sid} -> {'OK' if ok else 'FAIL'}")


def write_report(results: list[dict], skipped_clusters: list[dict], out_path: str):
    rows = []
    for r in results:
        rows.append({
            "cluster_id": r.get("cluster_id", ""),
            "user_id": r.get("user_id", ""),
            "trade_first_name": r.get("trade_first_name", ""),
            "external_url": r.get("qualification_url", ""),
            "cos_url": r.get("cos_url") or "",
            "file_name": r.get("file_name") or "",
            "sample_id": r.get("sample_id") or "",
            "pic_id": r.get("pic_id") or "",
            "duplicate": r.get("duplicate", ""),
            "dedupe_score": r.get("dedupe_score", ""),
            "step": r.get("step", ""),
            "error": r.get("error") or "",
            "remark": r.get("remark", ""),
        })
    for r in skipped_clusters:
        rows.append({
            "cluster_id": r.get("cluster_id", ""),
            "user_id": r.get("user_id", ""),
            "trade_first_name": r.get("trade_first_name", ""),
            "external_url": r.get("qualification_url", ""),
            "cos_url": "",
            "file_name": "",
            "sample_id": "",
            "pic_id": "",
            "duplicate": "",
            "dedupe_score": "",
            "step": "skipped",
            "error": r.get("_skip_reason", ""),
            "remark": "",
        })
    fields = list(rows[0].keys()) if rows else []
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log(f"明细写入 {out_path}（{len(rows)} 行）")


# ------------------------- CLI -------------------------

def _load_reps(args) -> list[dict]:
    """五个入口的统一入口逻辑"""
    if args.from_cluster:
        for k in ("annotated", "cluster_full", "embedding"):
            if not getattr(args, k.replace("-", "_"), None):
                raise SystemExit(f"--from-cluster 需要 --{k}")
        cands = load_from_cluster(args.annotated, args.cluster_full, args.embedding)
        return pick_representatives(cands, args.embedding)
    if getattr(args, "from_input_csv", None):
        # 后端 upload 入口：一个 csv 承载所有候选（URL 必填，user_id/行业选填）
        # 每张图直接生成一个 rep，不聚簇（每张都要过 VLM）
        prefix = getattr(args, "input_prefix", None) or "upload"
        return load_from_input_csv(args.from_input_csv, tag_prefix=prefix)
    if args.from_online:
        return load_from_online(args.from_online, args.url_col)
    if args.from_xlsx:
        return load_from_xlsx(args.from_xlsx, args.url_col)
    if args.from_urls:
        return load_from_urls_file(args.from_urls)
    raise SystemExit(
        "必须指定 --from-cluster/--from-input-csv/--from-online/--from-xlsx/--from-urls 之一"
    )


def cmd_prepare(args):
    """阶段1：挑代表图 + 写 batches.json 供 VLM 判定

    带 --auto-vlm 时：自动下载代表图 → 走内网 VLM endpoint 逐张判定 → 回写 batches.json，
    prepare 一步产出可直接 submit 的状态。
    """
    reps = _load_reps(args)
    if not reps:
        log("候选池为空", "WARN")
        return
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "reps.json").write_text(json.dumps(reps, ensure_ascii=False, indent=2))
    build_batches_for_vlm(reps, state_dir, batch_size=args.batch_size)
    if getattr(args, "auto_vlm", False):
        auto_run_vlm(state_dir)
        log(f"✓ prepare + auto-vlm 完成。可直接运行 submit")
    else:
        log(f"✓ prepare 完成。请对 {state_dir}/batches.json 各批填 vlm_result 后运行 submit")


def cmd_submit(args, sso_override: dict | None = None):
    """阶段2：读 batches.json 的判定结果 → 剔电子版整簇 → 转存 → 去重 → 入库。

    sso_override 供同进程的 FastAPI 后端安全传入当前认证用户登录态，避免把 Cookie
    放进 subprocess 的参数或环境变量。
    """
    state_dir = Path(args.state_dir)
    reps_path = state_dir / "reps.json"
    if not reps_path.exists():
        raise SystemExit(f"未找到 {reps_path}，请先跑 prepare")
    reps = json.loads(reps_path.read_text())
    verdicts = load_vlm_verdicts(state_dir)
    log(f"载入 VLM 判定 {len(verdicts)} 条")

    if args.skip_vlm:
        log("--skip-vlm 生效：不剔电子版，全部候选进入入库流程", "WARN")
        passed, skipped = reps, []
    else:
        passed, skipped = filter_electronic(reps, verdicts, strict=args.strict_vlm)

    sso = sso_override or load_sso()
    log(f"登录态: {sso['display_name']} <{sso['email']}>")
    sess = session_with_sso(sso)

    log(f"开始入库 {len(passed)} 张代表图（dry_run={args.dry_run}）")
    results = []
    for i, r in enumerate(passed, start=1):
        log(f"[{i}/{len(passed)}] cluster={r.get('cluster_id','-')} url={r['qualification_url'][:80]}")
        result = process_one(r, sess, args.week_tag, seq=i, dry_run=args.dry_run)
        results.append(result)
        status = "✅" if result["step"] in ("done", "dry-run-ok") else "❌"
        log(f"  {status} step={result['step']} sample_id={result.get('sample_id')} err={result.get('error')}")
        if i % BATCH_SIZE == 0:
            time.sleep(BATCH_INTERVAL_S)

    ok = [r for r in results if r["step"] in ("done", "dry-run-ok")]
    dup = [r for r in results if r["error"] and "重复" in r["error"]]
    err = [r for r in results if r["error"] and "重复" not in r["error"]]
    log(f"完成: {len(ok)} 成功 / {len(dup)} 跨周期重复跳过 / {len(err)} 失败 / {len(skipped)} 整簇电子版跳过")

    out_path = args.out or (state_dir / "report.csv")
    write_report(results, skipped, str(out_path))

    if err and args.rollback_on_error:
        rollback(sess, ok)


def cmd_rollback(args):
    """从 report.csv 读所有 sample_id 一键删除（应急）"""
    sso = load_sso()
    sess = session_with_sso(sso)
    ids = []
    with open(args.report, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("sample_id") and r["sample_id"] != "":
                ids.append(int(r["sample_id"]))
    log(f"待删除 {len(ids)} 条")
    for sid in ids:
        ok = delete_sample(sess, sid)
        log(f"  DELETE id={sid} -> {'OK' if ok else 'FAIL'}")


def main():
    ap = argparse.ArgumentParser(description="高危模板机器人")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # 共享参数（数据源）
    def add_src(p):
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--from-cluster", action="store_true", help="从聚簇结果拉候选")
        src.add_argument("--from-input-csv", metavar="CSV",
                         help="从后端预处理的 input.csv 拉候选（upload 入口用；每张图一个 rep 不聚簇）")
        src.add_argument("--from-online", metavar="SHEET_URL", help="从在线 REDoc 表格拉 URL 列")
        src.add_argument("--from-xlsx", metavar="XLSX_PATH", help="从本地 xlsx 拉 URL 列")
        src.add_argument("--from-urls", metavar="URLS_FILE", help="从文本文件拉 URL（每行一个）")
        p.add_argument("--annotated", help="annotated_wXX.csv (--from-cluster)")
        p.add_argument("--cluster-full", help="聚簇全表 csv (--from-cluster)")
        p.add_argument("--embedding", help="full_account_embedding.csv (--from-cluster)")
        p.add_argument("--url-col", default="资质图URL", help="URL 所在列名")
        p.add_argument("--input-prefix", help="input-csv 模式下生成 cluster_id 的前缀（默认 upload）")

    # prepare
    pp = sub.add_parser("prepare", help="挑代表图 → 写 batches.json 供 VLM 判定")
    add_src(pp)
    pp.add_argument("--state-dir", required=True, help="工作目录，如 runs/w35")
    pp.add_argument("--batch-size", type=int, default=10, help="每批多少张给 VLM")
    pp.add_argument("--auto-vlm", action="store_true",
                    help="自动下载代表图 + 走内网 VLM endpoint 判定并回写 batches.json（免手动）")

    # submit
    ps = sub.add_parser("submit", help="消费 VLM 结果 → 剔电子版 → 入库")
    ps.add_argument("--state-dir", required=True, help="prepare 阶段的目录")
    ps.add_argument("--week-tag", default="", help="标记归属周期，如 W35（写入 file_name/remark）")
    ps.add_argument("--dry-run", action="store_true", help="全流程但不真正 POST /sample")
    ps.add_argument("--skip-vlm", action="store_true", help="跳过 VLM 电子版剔除（应急）")
    ps.add_argument("--strict-vlm", action="store_true", help="严格模式：confidence=medium 也当电子版")
    ps.add_argument("--out", help="report csv 路径（默认 state_dir/report.csv）")
    ps.add_argument("--rollback-on-error", action="store_true", help="任意非重复失败即回滚已入库项")

    # rollback
    pr = sub.add_parser("rollback", help="从 report.csv 一键删除所有入库记录")
    pr.add_argument("--report", required=True)

    args = ap.parse_args()
    if args.cmd == "prepare":
        cmd_prepare(args)
    elif args.cmd == "submit":
        cmd_submit(args)
    elif args.cmd == "rollback":
        cmd_rollback(args)


if __name__ == "__main__":
    main()
