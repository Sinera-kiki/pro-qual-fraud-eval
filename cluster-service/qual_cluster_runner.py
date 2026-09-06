"""qual-cluster-service 跑批执行器 —— 领任务 / 取数 / 聚簇 / 回传产物。

链路：
  claim 站点后端任务（声明支持 pool_match + self_cluster 两种模式）
  → 解析上传表格里的账户 ID
  → 走 Dataverse HiveSQL（DOWNLOAD 模式）取资质图 embedding
  → 调 cluster_fast.py threshold 方法聚簇
  → 结果表 + 聚簇网页作为产物回传，回写统计

启动：
  .venv/bin/python qual_cluster_runner.py
"""
from __future__ import annotations

import base64
import html
import io
import os
import re
import subprocess
import sys
import tempfile
import time

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# Pod 出口走自签证书代理，内网接口统一不校验 TLS
_orig_request = requests.request


def _patched_request(method, url, **kw):
    kw.setdefault("verify", False)
    return _orig_request(method, url, **kw)


requests.request = _patched_request
requests.get = lambda url, **kw: _patched_request("get", url, **kw)
requests.post = lambda url, **kw: _patched_request("post", url, **kw)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "~/.openclaw/workspace/skills/sql-development/scripts")
import run as dv          # noqa: E402  do_submit / do_status
import run_api as ra      # noqa: E402  nb_get / secload 链接解析

BASE = os.environ.get("CLUSTER_SERVICE_URL", "https://app.example.com/s/cluster-service")
TOKEN_PATH = os.environ.get("RUNNER_TOKEN_FILE", "~/.config/pqfe/runner.properties")
VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
CLUSTER_SCRIPT = os.path.join(HERE, "cluster_fast.py")
EMB_TABLE = "warehouse.app_pro_account_ind_qualification_embedding_df"
IDLE_SLEEP = 15
MODES = "pool_match,self_cluster"


def load_token() -> str:
    env_tok = os.environ.get("WORKFLOW_RUNNER_TOKEN")
    if env_tok:
        return env_tok
    p = os.path.expanduser(TOKEN_PATH)
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line.startswith("token="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("runner token 未找到: " + TOKEN_PATH)


TOKEN = load_token()
HDR = {"X-Qc-Runner-Token": TOKEN}


def api(path, method="post", **kw):
    kw.setdefault("timeout", 120)
    return requests.request(method, BASE + path, headers=HDR, **kw)


def event(tid, stage, msg):
    try:
        api(f"/api/runner/{tid}/event", json={"stage": stage, "message": msg[:500]})
    except Exception as e:
        print(f"[warn] event 上报失败: {e}", file=sys.stderr, flush=True)


def finish_failed(tid, msg):
    print(f"[task {tid}] FAILED: {msg}", flush=True)
    event(tid, "runner", msg)
    try:
        api(f"/api/runner/{tid}/result", json={"status": "failed", "detail": msg[:500]})
    except Exception as e:
        print(f"[warn] result 回写失败: {e}", file=sys.stderr, flush=True)


def get_url(url):
    try:
        r = requests.get(url, timeout=600)
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        r = requests.get(url, timeout=600, verify=False)
        r.raise_for_status()
        return r.content


def run_sql(sql, timeout=1200):
    """提交 HiveSQL（DOWNLOAD 模式），返回 DataFrame。"""
    sub = dv.do_submit(sql, language="HiveSQL", mode="DOWNLOAD")
    if sub.get("auth_failed"):
        reason = (sub.get("auth_result") or {}).get("failure_reason") or "无权限"
        raise RuntimeError(f"SQL 权限校验不通过: {reason[:200]}")
    if sub.get("error"):
        raise RuntimeError(f"SQL 提交失败: {str(sub.get('error_msg'))[:200]}")
    msg_id, task_id, cell_id = sub["msgId"], sub["taskId"], sub["cellId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = dv.do_status(msg_id) or {}
        cell = st.get("cellExecuteState") or ""
        state = st.get("queryState") or ""
        if cell in ("FAILED", "CANCELED") or state in ("FAILED", "CANCELED"):
            raise RuntimeError(f"SQL 执行失败: {st.get('error') or cell or state}")
        if cell == "SUCCESS" or state == "FINISHED":
            break
        time.sleep(6)
    else:
        raise RuntimeError("SQL 执行超时（>20min）")
    resp = ra.nb_get(
        f"/api/notebook/execute/history/getQueryResult/{task_id}/{cell_id}/{msg_id}")
    data = resp.get("data") or {}
    dl = data.get("dataList") or []
    notice = None
    if dl:
        notice = dl[0].get("Notice") or dl[0].get("notice")
    if notice:
        notice = ra.resolve_secload_in_text(notice)
        m = re.search(r"https?://\S+", notice)
        if m:
            content = get_url(m.group(0).rstrip(".,;)'\""))
            return pd.read_csv(io.BytesIO(content))
    if data.get("success") and dl:
        return pd.DataFrame(dl)
    raise RuntimeError(f"未获取到下载链接: {(notice or str(data)[:200])}")


_DTM_CACHE = {"val": None, "ts": 0.0}


def get_latest_dtm():
    """先单独查最新分区（自带分区过滤），避免子查询触发全表扫描拦截。"""
    now = time.time()
    if _DTM_CACHE["val"] and now - _DTM_CACHE["ts"] < 3600:
        return _DTM_CACHE["val"]
    from datetime import date, timedelta
    lows = [(date.today() - timedelta(days=12)).strftime("%Y-%m-%d"),
            (date.today() - timedelta(days=12)).strftime("%Y%m%d")]
    val = None
    for low in lows:
        df = run_sql(f"SELECT MAX(dtm) AS max_dtm FROM {EMB_TABLE} WHERE dtm >= '{low}'")
        if not df.empty and df["max_dtm"].notna().any():
            val = str(df["max_dtm"].dropna().iloc[0]).strip()
            break
    if not val:
        raise RuntimeError("无法获取 embedding 表最新分区 dtm")
    _DTM_CACHE.update(val=val, ts=now)
    return val


def fetch_embeddings(ids, pool=None):
    """目标账户（+历史池）的资质图 embedding。"""
    latest = "'" + get_latest_dtm() + "'"
    frames = []
    ids = [str(i).strip() for i in ids if str(i).strip()]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        inlist = ",".join("'%s'" % x.replace("'", "''") for x in chunk)
        sql = (f"SELECT user_id, qualification_url, max(embeds) AS embeds "
               f"FROM {EMB_TABLE} WHERE dtm = {latest} AND user_id IN ({inlist}) "
               f"GROUP BY user_id, qualification_url")
        frames.append(run_sql(sql))
    if pool:
        sql = (f"SELECT user_id, qualification_url, max(embeds) AS embeds "
               f"FROM {EMB_TABLE} WHERE dtm = {latest} "
               f"AND settle_date BETWEEN '{pool[0]}' AND '{pool[1]}' "
               f"GROUP BY user_id, qualification_url")
        frames.append(run_sql(sql))
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return pd.DataFrame(columns=["user_id", "qualification_url", "embeds"])
    return pd.concat(frames, ignore_index=True)


def read_table(content: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".tsv"):
        return pd.read_csv(io.BytesIO(content), sep="\t", dtype=str)
    if name.endswith(".csv"):
        try:
            return pd.read_csv(io.BytesIO(content), dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(content), dtype=str, encoding="gbk")
    return pd.read_excel(io.BytesIO(content), dtype=str)


def cluster_threshold(in_csv, out_csv, sus_csv):
    cmd = [VENV_PY, CLUSTER_SCRIPT, "--method", "threshold",
           "--input-file", in_csv, "--output", out_csv, "--suspect", sus_csv,
           "--id-col", "user_id", "--url-col", "qualification_url",
           "--threshold", "0.98", "--top-k", "10"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError("聚簇脚本失败: " + (p.stderr or p.stdout)[-400:])


GALLERY_CSS = ("body{font:13px/1.5 -apple-system,'PingFang SC',sans-serif;margin:24px;"
               "color:#222}h1{font-size:20px}h2{font-size:15px;margin:26px 0 8px}"
               ".meta{color:#888;font-size:12px}.g{display:flex;flex-wrap:wrap;gap:8px}"
               "figure{margin:0;width:150px;border:1px solid #e5e5e5;border-radius:8px;"
               "overflow:hidden;background:#fff}figure.target{border:2px solid #635bff}"
               "img{width:100%;height:150px;object-fit:cover;display:block}"
               "figcaption{font-size:11px;color:#666;padding:4px 6px;"
               "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}")


def build_gallery(res: pd.DataFrame, title: str, mode: str) -> bytes:
    clusters: dict = {}
    for _, r in res.iterrows():
        try:
            cid = int(float(r["cluster_id"]))
        except Exception:
            continue
        if cid < 0:
            continue
        clusters.setdefault(cid, []).append(r)
    order = sorted(clusters, key=lambda c: -len(clusters[c]))
    parts = [f"<html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
             f"<style>{GALLERY_CSS}</style></head><body>",
             f"<h1>{html.escape(title)}</h1>",
             f"<div class='meta'>模式={html.escape(mode)} · 有效簇 {len(order)} 个 · "
             f"生成于 {time.strftime('%Y-%m-%d %H:%M')} · 紫边为清单目标账户</div>"]
    if not order:
        parts.append("<p>无有效簇（全部账户未成簇或无资质图）。</p>")
    for cid in order[:200]:
        rows = clusters[cid]
        n_users = len({str(r["user_id"]) for r in rows})
        parts.append(f"<h2>簇 {cid} · {len(rows)} 图 · {n_users} 账户</h2><div class='g'>")
        for r in rows[:80]:
            cls = "target" if str(r.get("is_target")) == "1" else "pool"
            url = html.escape(str(r.get("qualification_url") or ""), quote=True)
            uid = html.escape(str(r.get("user_id") or ""))
            parts.append(f"<figure class='{cls}'><img loading='lazy' src='{url}'>"
                         f"<figcaption>{uid}</figcaption></figure>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "\n".join(parts).encode("utf-8")


def upload_artifact(tid, kind, filename, content: bytes) -> str:
    r = api(f"/api/runner/{tid}/artifact",
            json={"kind": kind, "filename": filename,
                  "content_b64": base64.b64encode(content).decode()})
    r.raise_for_status()
    return r.json()["url"]


def process(task: dict, table_b64: str):
    tid = task["id"]
    mode = task.get("cluster_mode") or "pool_match"
    print(f"[task {tid}] start mode={mode} title={task.get('title')}", flush=True)
    if task.get("id_type") != "uid":
        finish_failed(tid, "机审订单号（audit_order）到账户的映射尚未接入跑批，请改用 user_id 清单提交")
        return
    col = task.get("id_column")
    df = read_table(base64.b64decode(table_b64), task.get("source_ref") or "upload.xlsx")
    if col not in df.columns:
        finish_failed(tid, f"清单缺少 ID 列「{col}」，实际列: {list(df.columns)[:8]}")
        return
    ids = sorted({str(x).strip() for x in df[col].dropna() if str(x).strip()})
    if not ids:
        finish_failed(tid, f"ID 列「{col}」没有有效值")
        return
    if len(ids) > 20000:
        finish_failed(tid, f"ID 数过多（{len(ids)}），请拆分清单后重试（上限 2 万）")
        return
    event(tid, "map", f"解析清单得到 {len(ids)} 个账户 ID")

    pool = None
    if mode == "pool_match":
        pool = (task.get("pool_start"), task.get("pool_end"))
        if not pool[0] or not pool[1]:
            finish_failed(tid, "历史图池匹配任务缺少时间窗")
            return
        event(tid, "fetch", f"取 embedding：目标账户 + 历史池 {pool[0]}~{pool[1]}")
    else:
        event(tid, "fetch", f"取 {len(ids)} 个上传账户的 embedding（自聚簇）")
    emb = fetch_embeddings(ids, pool=pool)
    if emb.empty:
        finish_failed(tid, "目标账户均无 embedding（无资质图或分区无数据），无法聚簇")
        return
    emb["user_id"] = emb["user_id"].astype(str).str.strip()
    emb = emb[emb["embeds"].notna() & (emb["embeds"].astype(str).str.strip() != "")]
    target_set = set(ids)
    emb["is_target"] = emb["user_id"].isin(target_set).astype(int)
    n_have = emb.loc[emb["is_target"] == 1, "user_id"].nunique()
    event(tid, "fetch", f"embedding 取数完成：共 {len(emb)} 行，目标账户有图 {n_have}/{len(ids)}")
    if n_have == 0:
        finish_failed(tid, "目标账户均无 embedding（无资质图或分区无数据），无法聚簇")
        return
    if mode == "pool_match" and n_have < len(ids):
        event(tid, "fetch", f"注意：{len(ids) - n_have} 个目标账户无资质图，将计入「无图」")

    workdir = tempfile.mkdtemp(prefix=f"qc_run_{tid}_")
    in_csv = os.path.join(workdir, "input.csv")
    out_csv = os.path.join(workdir, "cluster_result.csv")
    sus_csv = os.path.join(workdir, "suspect_clusters.csv")
    emb.to_csv(in_csv, index=False)
    event(tid, "cluster", f"开始 threshold 聚簇（余弦 0.98）：{len(emb)} 条向量")
    cluster_threshold(in_csv, out_csv, sus_csv)
    res = pd.read_csv(out_csv, dtype={"user_id": str})
    res["user_id"] = res["user_id"].astype(str).str.strip()

    if "is_target" in res.columns:
        tgt = res[res["is_target"].astype(int) == 1]
    else:
        tgt = res
    have = set(tgt["user_id"])
    clustered = {u for u, c in zip(tgt["user_id"], tgt["cluster_id"])
                 if int(float(c)) != -1}
    stats = {
        "n_accounts": len(target_set),
        "n_clustered": len(clustered),
        "n_unclustered": len(have) - len(clustered),
        "n_no_emb": len(target_set) - len(have),
    }
    n_valid = int((res["cluster_id"].astype(float) >= 0).sum() and
                  res.loc[res["cluster_id"].astype(float) >= 0, "cluster_id"].nunique())
    event(tid, "cluster", f"聚簇完成：有效簇 {n_valid} 个；目标聚上簇 {stats['n_clustered']}、"
                          f"未聚上 {stats['n_unclustered']}、无图 {stats['n_no_emb']}")

    csv_bytes = res.to_csv(index=False).encode("utf-8-sig")
    page_bytes = build_gallery(res, task.get("title") or f"任务 {tid}", mode)
    event(tid, "build", "上传产物：结果表 + 聚簇网页")
    upload_artifact(tid, "csv", f"task{tid}_cluster_result.csv", csv_bytes)
    upload_artifact(tid, "page", f"task{tid}_cluster_page.html", page_bytes)

    r = api(f"/api/runner/{tid}/result",
            json={"status": "done", "detail": None, **stats})
    r.raise_for_status()
    print(f"[task {tid}] done {stats}", flush=True)


def claim():
    r = requests.post(BASE + "/api/runner/claim",
                      headers={**HDR, "X-Qc-Runner-Modes": MODES}, timeout=60)
    return r.json()


def main():
    print(f"[runner] started backend={BASE} modes={MODES}", flush=True)
    while True:
        try:
            data = claim()
        except Exception as e:
            print(f"[warn] claim 失败: {e}", file=sys.stderr, flush=True)
            time.sleep(IDLE_SLEEP)
            continue
        if not data.get("ok"):
            time.sleep(IDLE_SLEEP)
            continue
        task = data["task"]
        try:
            process(task, data.get("table_content_b64") or "")
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                finish_failed(task["id"],
                              f"跑批异常: {type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
        time.sleep(2)


if __name__ == "__main__":
    main()
