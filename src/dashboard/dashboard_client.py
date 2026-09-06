#!/usr/bin/env python3
"""
资质造假审核工作流看板 · 看板上报客户端

用法（作为库）：
    from dashboard_client import DashboardClient
    dc = DashboardClient(biz_date="2026-08-04")
    dc.run_report(status="fetching")
    dc.step_start("fetch")
    ...
    dc.step_finish("fetch", metrics={...})

用法（命令行自测）：
    python3 dashboard_client.py --selftest

鉴权：写接口走内网免鉴权（API v1.0 2026-08-05 起）。
      读接口走 SSO，自动从 ${SSO_COOKIE_FILE:-~/.config/pqfe/sso.json} 注入 cookie。
      文档保留了 HMAC 作为未来升级预留；若服务端重新启用，
      设置环境变量 DASH_HMAC_SECRET 或写入同目录 .push_secret 即自动带上签名。
"""
import os
import sys
import json
import time
import hmac
import hashlib
import pathlib
from datetime import datetime, timezone

import requests

BASE = "https://dashboard.example.com/pro-qual-eval"
SECRET_FILE = pathlib.Path(__file__).with_name(".push_secret")

STEP_SEQ = {"fetch": 1, "cluster": 2, "upload": 3, "notify": 4, "await_review": 5}
STEP_NAME = {"fetch": "取数", "cluster": "聚簇", "upload": "上传标注平台",
             "notify": "通知", "await_review": "等待人工审核"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_secret():
    """可选：服务端若重新启用 HMAC，从环境变量或本地文件读密钥。没有则返回 None。"""
    s = os.environ.get("DASH_HMAC_SECRET")
    if s:
        return s.strip()
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    return None


def _sso_cookie():
    path = os.path.expanduser(
        os.environ.get("SSO_COOKIE_FILE", "~/.config/pqfe/sso.json"))
    try:
        return json.load(open(path))["cookieHeader"]
    except Exception:
        return None


class DashboardClient:
    def __init__(self, biz_date, run_id=None, version=1, secret=None,
                 base=BASE, verbose=True):
        self.biz_date = biz_date
        self.run_id = run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.version = version
        self.base = base
        self.secret = secret if secret is not None else _load_secret()
        self.verbose = verbose
        self._started = {}
        self.sess = requests.Session()
        ck = _sso_cookie()
        if ck:
            self.sess.headers["Cookie"] = ck

    # ---------- 底层 ----------
    def _sign(self, path):
        """内网免鉴权；仅当本地配置了 secret 时才附带 HMAC 头（向前兼容）。"""
        if not self.secret:
            return {}
        ts = str(int(time.time()))
        sig = hmac.new(self.secret.encode("utf-8"),
                       f"{ts}{path}".encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return {"X-DASH-Timestamp": ts, "X-DASH-Signature": sig}

    def _post(self, path, json_body=None, files=None, data=None):
        headers = self._sign(path)
        r = self.sess.post(f"{self.base}{path}", json=json_body,
                           files=files, data=data, headers=headers, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {r.status_code} {r.text[:300]}")
        out = r.json()
        if self.verbose:
            print(f"  ✓ {path} {json.dumps(out, ensure_ascii=False)}")
        return out

    # ---------- 台账 ----------
    def run_report(self, status, dataset_id=None, dataset_name=None,
                   total_rows=None, cluster_count=None,
                   uploaded_at=None, annotated_at=None, note=""):
        body = {
            "biz_date": self.biz_date,
            "run_id": self.run_id,
            "version": self.version,
            "status": status,
            "note": note,
        }
        for k, v in [("dataset_id", dataset_id), ("dataset_name", dataset_name),
                     ("total_rows", total_rows), ("cluster_count", cluster_count),
                     ("uploaded_at", uploaded_at), ("annotated_at", annotated_at)]:
            if v is not None:
                body[k] = v
        return self._post("/api/dash/run-report", json_body=body)

    # ---------- 节点 ----------
    def step_start(self, step_key, metrics=None, note=""):
        self._started[step_key] = (time.time(), _now_iso())
        return self._post("/api/dash/step-report", json_body={
            "biz_date": self.biz_date, "run_id": self.run_id,
            "step_seq": STEP_SEQ[step_key], "step_key": step_key,
            "step_name": STEP_NAME[step_key], "status": "running",
            "started_at": self._started[step_key][1],
            "metrics": metrics or {}, "note": note,
        })

    def step_finish(self, step_key, metrics=None, status="success", note=""):
        t0, started_at = self._started.get(step_key, (time.time(), _now_iso()))
        return self._post("/api/dash/step-report", json_body={
            "biz_date": self.biz_date, "run_id": self.run_id,
            "step_seq": STEP_SEQ[step_key], "step_key": step_key,
            "step_name": STEP_NAME[step_key], "status": status,
            "started_at": started_at, "finished_at": _now_iso(),
            "duration_ms": int((time.time() - t0) * 1000),
            "metrics": metrics or {}, "note": note,
        })

    def step_fail(self, step_key, err, metrics=None):
        return self.step_finish(step_key, metrics=metrics,
                                status="failed", note=str(err)[:500])

    # ---------- 产物 ----------
    def upload_artifact(self, step_key, filepath, filename=None,
                        content_type="text/csv", max_mb=100):
        p = pathlib.Path(filepath)
        if not p.exists():
            raise FileNotFoundError(p)
        size_mb = p.stat().st_size / 1024 / 1024
        if size_mb > max_mb:
            raise RuntimeError(
                f"{p.name} 为 {size_mb:.0f}MB，超过 {max_mb}MB 上限，请先瘦身")
        with open(p, "rb") as f:
            return self._post("/api/dash/artifacts",
                              data={"biz_date": self.biz_date,
                                    "run_id": self.run_id,
                                    "step_key": step_key,
                                    "filename": filename or p.name,
                                    "content_type": content_type},
                              files={"file": f})

    # ---------- 标注进度 ----------
    def annotation_progress(self, dataset_id, annotated, total,
                            finished=False, annotated_at=None):
        return self._post("/api/dash/annotation-progress", json_body={
            "biz_date": self.biz_date, "run_id": self.run_id,
            "dataset_id": dataset_id,
            "annotated_cluster_count": annotated,
            "total_cluster_count": total,
            "finished": bool(finished),
            "annotated_at": annotated_at if finished else None,
        })

    # ---------- 读接口 ----------
    def get_run(self, biz_date=None):
        r = self.sess.get(f"{self.base}/api/dashboard/runs",
                          params={"biz_date": biz_date or self.biz_date},
                          timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


def selftest():
    """连通性自测：读接口 + 五节点写入 + 产物 + 进度回写，全部打到测试 biz_date。"""
    TEST_DATE = "2099-01-01"
    print("1) 读接口")
    ck = _sso_cookie()
    r = requests.get(f"{BASE}/api/dashboard/biz-dates?limit=5",
                     headers={"Cookie": ck} if ck else {}, timeout=30)
    print(f"   HTTP {r.status_code} {r.text[:200]}")

    print("2) 写接口（内网免鉴权）")
    dc = DashboardClient(biz_date=TEST_DATE, run_id="selftest_run")
    dc.run_report(status="fetching", note="连通性自测")

    print("3) 五节点打点")
    for key, m in [("fetch", {"input_rows": 1, "week_user_count": 1}),
                   ("cluster", {"valid_cluster": 1}),
                   ("upload", {"rows": 1, "cluster_count": 1}),
                   ("notify", {"chat": "selftest"}),
                   ("await_review", {"annotated": 0, "total": 1, "progress": 0})]:
        dc.step_start(key)
        dc.step_finish(key, metrics=m,
                       status="running" if key == "await_review" else "success")

    print("4) 产物上传")
    tmp = pathlib.Path("/tmp/_dash_selftest.csv")
    tmp.write_text("user_id,cluster_id\n1,0\n")
    dc.upload_artifact("fetch", tmp, filename="selftest.csv")
    tmp.unlink(missing_ok=True)

    print("5) 标注进度回写")
    dc.annotation_progress(dataset_id="selftest-uuid-0001", annotated=1,
                           total=1, finished=False)

    print("6) 回读校验")
    got = dc.get_run(TEST_DATE)
    if not got:
        print("   ✗ 回读为空")
        return 1
    print(f"   run.status={got['run']['status']} "
          f"steps={len(got.get('steps', []))} "
          f"artifacts={len(got.get('artifacts', []))}")
    print(f"   dataset_id 回读值={got['run'].get('dataset_id')!r}")
    print("✓ 全链路自测通过")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
