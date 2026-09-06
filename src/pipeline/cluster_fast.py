"""
对 parquet 文件的 embedding 进行聚类（通用版）

通用设计：
  - 只依赖 FAISS 索引（faiss_index.bin）和元数据（faiss_meta.pkl）
  - meta 中可包含任意字段，聚类结果仅在 meta 基础上追加 cluster_id 列
  - 可疑簇统计需要 "用于标识唯一身份的列"，默认用第一个 meta 字段，
    可通过 --id-col 指定；像素匹配需要图片 URL 列，可通过 --url-col 指定

依赖（.venv 中）：
  faiss-cpu, numpy, pandas, scikit-learn, hdbscan, tqdm, requests, opencv-python, pillow

支持四种聚类模式（--method 参数）：
  hdbscan     : HDBSCAN 密度聚类（默认）——自动发现簇数，无需预设 k
  kmeans      : FAISS 球面 K-Means ——速度最快，需要预设 k
  threshold   : 余弦相似度阈值图连通分量——专门用于找"几乎完全相同"的向量组
  pixel_match : 先 FAISS topk 粗筛候选对，再下载图片用 ORB+像素一致率精筛

用法示例：
  # HDBSCAN（默认）
  python cluster.py
  python cluster.py --method hdbscan --pca-dim 64 --min-cluster-size 3

  # K-Means，指定 k=500
  python cluster.py --method kmeans --k 500

  # 余弦阈值连通分量
  python cluster.py --method threshold --threshold 0.98 --top-k 10

  # 像素匹配聚类（需指定包含图片 URL 的列名）
  python cluster.py --method pixel_match --url-col qualification_url

  # 指定用于唯一身份统计的列（可疑簇分析）
  python cluster.py --id-col user_id

  --method pixel_match --url-col qualification_url --cosine-topk 5 --cosine-prefilter 0.98 --match-rate-threshold 0.95 --pixel-tolerance 10 --max-workers 128

python cluster_fast.py --method pixel_match --url-col qualification_url --method pixel_match --url-col qualification_url --max-long-side 512 --orb-features 800 --skip-align-cosine 0.99 --cosine-topk 5 --cosine-prefilter 0.98 --match-rate-threshold 0.95 --pixel-tolerance 10 --max-workers 256

输出：
  cluster_result.csv   : 每条 embedding 的聚类结果（meta 字段 + cluster_id）
  suspect_clusters.csv : 同一簇内有多个不同身份的可疑簇（需指定 --id-col）
"""

import argparse
import ast
import concurrent.futures
import hashlib
import json
import os
import pickle
import sys
import threading
import time
from collections import defaultdict
from io import BytesIO

import cv2
import faiss
import numpy as np
import pandas as pd
import requests
from PIL import Image
from tqdm import tqdm

# ── 路径配置 ───────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INDEX_FILE  = os.path.join(BASE_DIR, "faiss_index.bin")
DEFAULT_META_FILE   = os.path.join(BASE_DIR, "faiss_meta.pkl")
DEFAULT_OUTPUT_CSV  = os.path.join(BASE_DIR, "cluster_result.csv")
DEFAULT_SUSPECT_CSV = os.path.join(BASE_DIR, "suspect_clusters.csv")
EMBEDDING_COL = "embeds"


# ══════════════════════════════════════════════════════════════════════════════
# 直接读取 CSV / XLSX 输入并构建内存索引
# 说明：只改输入层，后续 FAISS / 聚类 / 像素匹配计算逻辑保持不变。
# ══════════════════════════════════════════════════════════════════════════════

def parse_embed_value(embed_val, expected_dim: int | None) -> np.ndarray | None:
    """
    将 CSV / XLSX / parquet 中的 embedding 解析为 L2 归一化 float32 向量。
    支持：
      - list / ndarray
      - JSON 数组字符串：[0.1, 0.2, ...]
      - Python list 字符串：['0.1', '0.2', ...]
      - 逗号分隔字符串：0.1,0.2,...
    """
    try:
        if embed_val is None:
            return None
        if isinstance(embed_val, float) and np.isnan(embed_val):
            return None

        if isinstance(embed_val, (list, np.ndarray)):
            arr = embed_val
        elif isinstance(embed_val, str):
            s = embed_val.strip()
            if not s:
                return None
            if s.startswith("[") and s.endswith("]"):
                try:
                    arr = json.loads(s)
                except Exception:
                    arr = ast.literal_eval(s)
            else:
                arr = s.split(",")
        else:
            return None

        vec = np.array(arr, dtype=np.float32)
        if vec.ndim != 1 or vec.shape[0] == 0:
            return None
        if expected_dim is not None and vec.shape[0] != expected_dim:
            return None

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec
    except Exception:
        return None


def _to_python_scalar(val):
    """将 pandas / numpy 类型转成可 pickle 的 Python 原生类型。"""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        if np.isnan(val):
            return None
        return float(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    if pd.isna(val) if not isinstance(val, (list, np.ndarray)) else False:
        return None
    return val


def _read_input_file(input_file: str, sheet: str | int | None = 0) -> pd.DataFrame:
    suffix = os.path.splitext(input_file)[1].lower()
    if suffix in [".csv", ".txt"]:
        return pd.read_csv(input_file)
    if suffix in [".xlsx", ".xlsm", ".xls"]:
        return pd.read_excel(input_file, sheet_name=sheet)
    if suffix in [".parquet", ".snappy.parquet"]:
        return pd.read_parquet(input_file)
    raise ValueError(f"不支持的输入文件类型: {suffix}，请使用 csv / xlsx / parquet")


def _read_sample_file(sample_file: str, sheet: str | int | None = 0) -> pd.DataFrame:
    """读取抽样用户表。支持 csv / xlsx / parquet。"""
    return _read_input_file(sample_file, sheet=sheet)


def load_tabular_input(input_file: str, sheet: str | int | None = 0):
    """从 CSV / XLSX / parquet 直接读取 embeds，构建 FAISS index、meta、matrix。"""
    print(f"\n{'='*60}")
    print("[1/4] 读取 CSV/XLSX 输入并构建 FAISS 索引")
    print(f"{'='*60}")
    print(f"  输入文件: {input_file}")
    t0 = time.time()

    df = _read_input_file(input_file, sheet=sheet)
    if EMBEDDING_COL not in df.columns:
        raise ValueError(f"输入文件缺少 '{EMBEDDING_COL}' 列，实际列: {list(df.columns)}")

    meta_cols = [c for c in df.columns if c != EMBEDDING_COL]
    print(f"  总行数     : {len(df):,}")
    print(f"  embedding列: {EMBEDDING_COL}")
    print(f"  meta列     : {meta_cols}")

    vectors: list[np.ndarray] = []
    meta: list[dict] = []
    skipped_empty = 0
    skipped_parse = 0
    inferred_dim: int | None = None

    for _, row in tqdm(df.iterrows(), total=len(df), desc="解析 embedding", unit="row"):
        val = row.get(EMBEDDING_COL)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            skipped_empty += 1
            continue

        vec = parse_embed_value(val, inferred_dim)
        if vec is None:
            skipped_parse += 1
            continue
        if inferred_dim is None:
            inferred_dim = vec.shape[0]
            print(f"\n  推断 embedding 维度: {inferred_dim}")

        vectors.append(vec)
        meta.append({c: _to_python_scalar(row[c]) for c in meta_cols})

    print("\n解析完成:")
    print(f"  有效向量数     : {len(vectors):,}")
    print(f"  跳过(空embeds) : {skipped_empty:,}")
    print(f"  跳过(解析失败) : {skipped_parse:,}")
    if not vectors:
        raise ValueError("没有有效 embedding，无法聚类")

    matrix = np.stack(vectors, axis=0).astype(np.float32)
    norms = np.linalg.norm(matrix[:min(10, len(matrix))], axis=1)
    print(f"  向量矩阵 shape : {matrix.shape}")
    print(f"  归一化验证     : {norms.round(4)}")

    index = faiss.IndexFlatIP(inferred_dim)
    index.add(matrix)
    print(f"  FAISS索引向量数: {index.ntotal:,}")
    print(f"  耗时           : {fmt_time(time.time()-t0)}")
    return index, meta, matrix


def expand_clusters_by_sample_users(
    df: pd.DataFrame,
    sample_files: list[str],
    sample_id_col: str,
    sample_group_col: str | None,
    sample_sheet: str | int | None,
    expanded_output: str,
    match_output: str,
) -> None:
    """
    基于抽样用户表扩展命中的有效簇。

    逻辑：
      1. 读取一个或多个抽样用户表，并按 user_id 去重
      2. 用抽样 user_id 匹配全量聚簇结果
      3. 只保留 cluster_id >= 0 的“聚簇成功”样本用户
      4. 将这些样本用户命中的 cluster_id 对应的同簇其他账号一并取出

    注意：这里只是聚类后的结果筛选/扩展，不改变任何聚类计算逻辑。
    """
    print(f"\n{'='*60}")
    print("[样本簇扩展] 读取抽样用户并扩展同簇账号")
    print(f"{'='*60}")

    if "user_id" not in df.columns:
        raise ValueError("聚簇结果中缺少 user_id 列，无法与抽样用户匹配")
    if "cluster_id" not in df.columns:
        raise ValueError("聚簇结果中缺少 cluster_id 列，无法判断有效簇")

    sample_frames = []
    for sample_file in sample_files:
        sample_df = _read_sample_file(sample_file, sheet=sample_sheet)
        if sample_id_col not in sample_df.columns:
            raise ValueError(f"抽样文件 {sample_file} 缺少 {sample_id_col} 列，实际列: {list(sample_df.columns)}")
        keep_cols = [sample_id_col]
        if sample_group_col and sample_group_col in sample_df.columns:
            keep_cols.append(sample_group_col)
        tmp = sample_df[keep_cols].copy()
        tmp = tmp.rename(columns={sample_id_col: "user_id"})
        if sample_group_col and sample_group_col in tmp.columns:
            tmp = tmp.rename(columns={sample_group_col: "sample_group"})
        else:
            tmp["sample_group"] = os.path.splitext(os.path.basename(sample_file))[0]
        tmp["sample_source_file"] = os.path.basename(sample_file)
        sample_frames.append(tmp)

    sample_all = pd.concat(sample_frames, ignore_index=True)
    sample_all["user_id"] = sample_all["user_id"].astype(str)
    sample_all = sample_all[sample_all["user_id"].notna() & (sample_all["user_id"].str.strip() != "")].copy()
    sample_all["user_id"] = sample_all["user_id"].str.strip()

    # user_id 去重，同时保留该用户命中的样本组信息
    sample_meta = (
        sample_all.groupby("user_id", as_index=False)
        .agg(
            sample_groups=("sample_group", lambda x: ",".join(sorted({str(v) for v in x if pd.notna(v)}))),
            sample_source_files=("sample_source_file", lambda x: ",".join(sorted({str(v) for v in x if pd.notna(v)}))),
        )
    )
    print(f"  抽样文件数          : {len(sample_files)}")
    print(f"  抽样原始行数        : {len(sample_all):,}")
    print(f"  抽样去重 user_id 数 : {len(sample_meta):,}")

    work_df = df.copy()
    work_df["user_id"] = work_df["user_id"].astype(str).str.strip()
    matched = work_df.merge(sample_meta, on="user_id", how="inner")
    matched.to_csv(match_output, index=False, encoding="utf-8-sig")
    print(f"  抽样账号匹配结果    : {len(matched):,} 行 -> {match_output}")

    matched_valid = matched[matched["cluster_id"] >= 0].copy()
    cluster_ids = sorted(matched_valid["cluster_id"].dropna().unique().tolist())
    print(f"  聚簇成功样本行数    : {len(matched_valid):,}")
    print(f"  命中的有效簇数      : {len(cluster_ids):,}")

    if not cluster_ids:
        empty = work_df.iloc[0:0].copy()
        empty.to_csv(expanded_output, index=False, encoding="utf-8-sig")
        print(f"  未命中有效簇，已输出空文件: {expanded_output}")
        return

    trigger_stats = (
        matched_valid.groupby("cluster_id", as_index=False)
        .agg(
            matched_sample_user_cnt=("user_id", "nunique"),
            matched_sample_groups=("sample_groups", lambda x: ",".join(sorted({g for v in x for g in str(v).split(",") if g}))),
            matched_sample_user_ids=("user_id", lambda x: ",".join(sorted(set(map(str, x))))),
        )
    )

    expanded = work_df[work_df["cluster_id"].isin(cluster_ids)].copy()
    expanded = expanded.merge(trigger_stats, on="cluster_id", how="left")
    expanded = expanded.merge(sample_meta, on="user_id", how="left")
    expanded["is_sample_user"] = expanded["sample_groups"].notna()

    # 排序：先按命中样本组、cluster_id，再把样本用户排在同簇前面
    expanded = expanded.sort_values(
        by=["matched_sample_groups", "cluster_id", "is_sample_user", "user_id"],
        ascending=[True, True, False, True],
    )
    expanded.to_csv(expanded_output, index=False, encoding="utf-8-sig")
    print(f"  同簇扩展结果        : {len(expanded):,} 行 -> {expanded_output}")
    print("  输出字段说明        : is_sample_user=True 表示该行账号来自抽样用户；False 表示同簇扩展账号")


# ══════════════════════════════════════════════════════════════════════════════
# 进度辅助：后台计时线程（用于无进度回调的耗时步骤）
# ══════════════════════════════════════════════════════════════════════════════

class TimerThread:
    """
    在后台每隔 interval 秒打印一次已等待时间，适用于无进度回调的黑盒函数。
    自动检测是否在 TTY 中：
      - TTY（终端交互）: 用 \\r 原地刷新 + 旋转符号
      - 非 TTY（日志文件）: 每行打印时间戳，方便 tail -f 追踪
    用法：
        with TimerThread("HDBSCAN 拟合中"):
            labels = clusterer.fit_predict(data)
    """
    def __init__(self, label: str = "运行中", interval: float = 5.0):
        self.label    = label
        self.interval = interval
        self._is_tty  = sys.stdout.isatty()
        self._stop    = threading.Event()
        self._t0      = None
        self._thread  = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        spinners = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"]
        idx = 0
        while not self._stop.is_set():
            elapsed = time.time() - self._t0
            m, s = divmod(int(elapsed), 60)
            if self._is_tty:
                spin = spinners[idx % len(spinners)]
                print(f"\r  {spin} {self.label} ... 已用时 {m:02d}:{s:02d}", end="", flush=True)
            else:
                ts = time.strftime("%H:%M:%S")
                print(f"  [{ts}] {self.label} ... 已用时 {m:02d}:{s:02d}", flush=True)
            idx += 1
            self._stop.wait(self.interval)
        elapsed = time.time() - self._t0
        m, s = divmod(int(elapsed), 60)
        if self._is_tty:
            print(f"\r  ✓ {self.label} 完成，耗时 {m:02d}:{s:02d}           ")
        else:
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] ✓ {self.label} 完成，耗时 {m:02d}:{s:02d}", flush=True)

    def __enter__(self):
        self._t0 = time.time()
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        return False


def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}" if m else f"{s}s"


# ══════════════════════════════════════════════════════════════════════════════
# 加载索引 + 重建向量矩阵
# ══════════════════════════════════════════════════════════════════════════════

def load_index_and_meta(index_file: str, meta_file: str):
    print(f"\n{'='*60}")
    print(f"[1/4] 加载 FAISS 索引")
    print(f"{'='*60}")
    t0 = time.time()
    index = faiss.read_index(index_file)
    with open(meta_file, "rb") as f:
        meta = pickle.load(f)
    print(f"  索引向量数 : {index.ntotal:,}")
    print(f"  元数据条数 : {len(meta):,}")
    print(f"  向量维度   : {index.d}")
    if meta:
        print(f"  meta 字段  : {list(meta[0].keys())}")
    print(f"  耗时       : {fmt_time(time.time()-t0)}")
    return index, meta


def reconstruct_matrix(index: faiss.Index) -> np.ndarray:
    """从 IndexFlatIP 一次性重建完整向量矩阵（reconstruct_n 最快）。"""
    print(f"\n{'='*60}")
    print(f"[2/4] 重建向量矩阵")
    print(f"{'='*60}")
    n, dim = index.ntotal, index.d
    print(f"  目标 shape : ({n:,} × {dim})")
    t0 = time.time()
    matrix = np.empty((n, dim), dtype=np.float32)
    with TimerThread("reconstruct_n", interval=3.0):
        index.reconstruct_n(0, n, matrix)
    print(f"  内存占用   : {matrix.nbytes / 1024**3:.2f} GB")
    print(f"  耗时       : {fmt_time(time.time()-t0)}")
    return matrix


# ══════════════════════════════════════════════════════════════════════════════
# 方法 1：HDBSCAN（默认）
# ══════════════════════════════════════════════════════════════════════════════

def cluster_hdbscan(
    matrix: np.ndarray,
    meta: list,
    pca_dim: int,
    min_cluster_size: int,
    min_samples: int,
) -> pd.DataFrame:
    """
    流程：
      Step A. PCA 降维（原始维度 → pca_dim），大幅减少 HDBSCAN 计算量
      Step B. L2 重归一化（欧氏距离 ≈ 余弦距离）
      Step C. HDBSCAN 聚类

    HDBSCAN 内部为 O(n log n) ~ O(n^2)，无进度回调，用 TimerThread 展示等待时间。
    """
    try:
        import hdbscan as hdbscan_lib
    except ImportError:
        print("[错误] 请安装 hdbscan：.venv/bin/pip install hdbscan")
        sys.exit(1)
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import normalize

    n, dim = matrix.shape
    print(f"\n{'='*60}")
    print(f"[3/4] HDBSCAN 聚类")
    print(f"{'='*60}")
    print(f"  数据量          : {n:,} 条")
    print(f"  原始维度        : {dim}")
    print(f"  PCA 目标维度    : {pca_dim}")
    print(f"  min_cluster_size: {min_cluster_size}")
    print(f"  min_samples     : {min_samples}")

    # ── Step A: PCA 降维 ────────────────────────────────────────────────────
    if pca_dim < dim:
        print(f"\n  ── Step A: PCA {dim} → {pca_dim} ──")
        t0 = time.time()
        pca = PCA(n_components=pca_dim, random_state=42)
        with TimerThread(f"PCA fit_transform", interval=3.0):
            reduced = pca.fit_transform(matrix).astype(np.float32)
        explained = pca.explained_variance_ratio_.sum()
        print(f"  解释方差比例    : {explained:.3f}  ({explained*100:.1f}%)")
        print(f"  PCA 耗时        : {fmt_time(time.time()-t0)}")
    else:
        print(f"\n  pca_dim({pca_dim}) >= dim({dim})，跳过 PCA")
        reduced = matrix.copy()

    # ── Step B: 重归一化 ────────────────────────────────────────────────────
    print(f"\n  ── Step B: L2 重归一化 ──")
    t0 = time.time()
    reduced = normalize(reduced, norm="l2")
    print(f"  重归一化耗时    : {fmt_time(time.time()-t0)}")

    # ── Step C: HDBSCAN 聚类 ────────────────────────────────────────────────
    print(f"\n  ── Step C: HDBSCAN 拟合 ──")
    print(f"  （HDBSCAN 无内部进度回调，以下为等待计时）")
    clusterer = hdbscan_lib.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",      # 归一化后欧氏 ≈ 余弦
        core_dist_n_jobs=-1,     # 使用全部 CPU 核
        approx_min_span_tree=True,
    )
    t0 = time.time()
    with TimerThread("HDBSCAN fit_predict", interval=5.0):
        labels = clusterer.fit_predict(reduced)
    hdbscan_time = time.time() - t0

    n_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    n_noise    = int((labels == -1).sum())
    print(f"\n  HDBSCAN 耗时    : {fmt_time(hdbscan_time)}")
    print(f"  发现簇数        : {n_clusters:,}")
    print(f"  噪声点（-1）    : {n_noise:,} ({n_noise/n*100:.1f}%)")
    print(f"  有效聚类点      : {n - n_noise:,} ({(n-n_noise)/n*100:.1f}%)")

    df = pd.DataFrame(meta)
    df["cluster_id"] = labels
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 方法 2：FAISS K-Means
# ══════════════════════════════════════════════════════════════════════════════

def cluster_kmeans(matrix: np.ndarray, meta: list, k: int, niter: int, seed: int) -> pd.DataFrame:
    """
    FAISS 球面 K-Means（向量已 L2 归一化，内积 = 余弦相似度）。
    FAISS verbose=True 会逐迭代打印 loss，进度清晰可见。
    """
    n, dim = matrix.shape
    print(f"\n{'='*60}")
    print(f"[3/4] FAISS K-Means 聚类")
    print(f"{'='*60}")
    print(f"  数据量   : {n:,} 条")
    print(f"  维度     : {dim}")
    print(f"  k        : {k}")
    print(f"  niter    : {niter}（每轮迭代下方打印一行 loss）")
    print(f"  nredo    : 3（取最优）")
    print()

    t0 = time.time()
    kmeans = faiss.Kmeans(
        dim, k,
        niter=niter,
        nredo=3,
        seed=seed,
        spherical=True,   # 球面 K-Means，适合余弦距离场景
        verbose=True,     # 每轮迭代打印 loss
    )
    kmeans.train(matrix)
    train_time = time.time() - t0

    print(f"\n  训练耗时 : {fmt_time(train_time)}")

    # 分配标签
    print("  分配标签中 ...", end="", flush=True)
    t1 = time.time()
    _, labels = kmeans.index.search(matrix, 1)
    labels = labels.flatten()
    print(f" 完成 ({fmt_time(time.time()-t1)})")

    n_clusters = len(set(labels))
    print(f"  实际簇数 : {n_clusters}（目标 k={k}）")

    df = pd.DataFrame(meta)
    df["cluster_id"] = labels
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 方法 3：余弦阈值图连通分量
# ══════════════════════════════════════════════════════════════════════════════

def cluster_threshold(
    index: faiss.Index,
    matrix: np.ndarray,
    meta: list,
    threshold: float,
    top_k: int,
    batch_size: int,
) -> pd.DataFrame:
    """
    对每个向量 FAISS 检索 top-k 邻居，保留余弦 >= threshold 的边，
    用 Union-Find 求连通分量。孤立点 cluster_id = -1。
    """
    n = matrix.shape[0]
    print(f"\n{'='*60}")
    print(f"[3/4] 阈值图连通分量聚类")
    print(f"{'='*60}")
    print(f"  数据量        : {n:,} 条")
    print(f"  余弦阈值      : {threshold}")
    print(f"  top_k         : {top_k}")
    print(f"  batch_size    : {batch_size}")
    n_batches = (n + batch_size - 1) // batch_size
    est_secs  = n_batches * 0.01
    print(f"  批次数        : {n_batches}（预计 {fmt_time(est_secs)} ~ {fmt_time(est_secs*3)}）")

    # ── Union-Find ──────────────────────────────────────────────────────────
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # ── 分批 FAISS 检索，建边 ───────────────────────────────────────────────
    edge_count = 0
    t0 = time.time()
    with tqdm(
        total=n,
        desc="  FAISS 批量检索",
        unit="vec",
        bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        file=sys.stdout,
        dynamic_ncols=True,
        disable=not sys.stdout.isatty(),
    ) as pbar:
        is_tty = sys.stdout.isatty()
        report_every = max(1, n_batches // 20)
        for batch_idx, start in enumerate(range(0, n, batch_size)):
            end   = min(start + batch_size, n)
            batch = matrix[start:end]
            scores, indices = index.search(batch, top_k + 1)  # +1 含自身

            for i_local, (row_scores, row_indices) in enumerate(zip(scores, indices)):
                i_global = start + i_local
                for score, j in zip(row_scores, row_indices):
                    if j < 0 or j == i_global:
                        continue
                    if score >= threshold:
                        union(i_global, j)
                        edge_count += 1

            pbar.update(end - start)
            if not is_tty and (batch_idx % report_every == 0 or end == n):
                pct = end / n * 100
                elapsed = time.time() - t0
                ts = time.strftime("%H:%M:%S")
                print(f"  [{ts}] 批量检索 {end:,}/{n:,} ({pct:.0f}%) 已用时 {fmt_time(elapsed)}", flush=True)

    print(f"  检索+建边耗时 : {fmt_time(time.time()-t0)}")
    print(f"  有效边数      : {edge_count:,}（余弦 >= {threshold}）")

    # ── Union-Find 压缩 + 分配 cluster_id ───────────────────────────────────
    print("  Union-Find 路径压缩 ...", end="", flush=True)
    t1 = time.time()
    root_members: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        root_members[find(i)].append(i)

    sorted_roots = sorted(root_members.items(), key=lambda x: len(x[1]), reverse=True)
    labels = np.full(n, -1, dtype=np.int32)
    cluster_id = 0
    for root, members in sorted_roots:
        if len(members) >= 2:
            for m in members:
                labels[m] = cluster_id
            cluster_id += 1
    print(f" 完成 ({fmt_time(time.time()-t1)})")

    n_clusters = cluster_id
    n_isolated = int((labels == -1).sum())
    print(f"  连通分量数（size≥2）: {n_clusters:,}")
    print(f"  孤立点               : {n_isolated:,} ({n_isolated/n*100:.1f}%)")

    df = pd.DataFrame(meta)
    df["cluster_id"] = labels
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 方法 4：像素匹配聚类（FAISS topk 粗筛 + ORB对齐像素一致率精筛）
# ══════════════════════════════════════════════════════════════════════════════

# 解码信号量：限制同时解码的图片数，避免多线程同时解码大图导致 cgroup OOM
_DECODE_SEM = threading.Semaphore(int(os.environ.get("IMG_DECODE_CONCURRENCY", "6")))


def _load_image_from_url(url: str, timeout: int = 30,
                         max_long_side: int | None = None) -> np.ndarray | None:
    """
    下载图片并返回 uint8 numpy 数组（H×W×3，RGB）。
    max_long_side: 若指定，则等比缩放使最长边不超过该值，保持原始宽高比。
    失败返回 None。
    """
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        with _DECODE_SEM:
            img = Image.open(BytesIO(resp.content))
            # JPEG 可在解码阶段直接降采样（draft），大幅降低解码内存峰值
            if max_long_side is not None:
                try:
                    img.draft("RGB", (max_long_side, max_long_side))
                except Exception:
                    pass
            img = img.convert("RGB")
            if max_long_side is not None:
                w, h = img.size
                long_side = max(w, h)
                if long_side > max_long_side:
                    scale = max_long_side / long_side
                    new_w = max(1, int(w * scale))
                    new_h = max(1, int(h * scale))
                    img = img.resize((new_w, new_h), Image.BILINEAR)  # BILINEAR 速度快于 LANCZOS
            arr = np.array(img, dtype=np.uint8)
            img.close()
        return arr
    except Exception:
        return None


def _align_and_match(arr1: np.ndarray, arr2: np.ndarray,
                     tolerance: int,
                     orb_features: int,
                     skip_align: bool,
                     min_inliers: int = 12,
                     min_inlier_ratio: float = 0.25) -> tuple[float, str]:
    """
    可选 ORB+Homography 对齐后计算像素一致率。

    返回 (match_rate, align_status)
      align_status: "skipped"(余弦极高跳过对齐) / "aligned"(对齐成功)
                    / "align_failed"(特征不足或单应矩阵求解失败)

    准确率优先改动：
      - 对齐失败时不再静默退回"未对齐直接比像素"（会产生虚低的一致率、
        把真实复用图误判为不同），而是明确返回 align_failed 供上层单独统计。
      - 单应矩阵需满足最小内点数与内点占比，避免少量噪声特征凑出的伪对齐。
    """
    if skip_align:
        diff = np.abs(arr1.astype(np.int16) - arr2.astype(np.int16))
        return float(np.all(diff <= tolerance, axis=2).mean()), "skipped"

    gray1 = cv2.cvtColor(arr1, cv2.COLOR_RGB2GRAY)
    gray2 = cv2.cvtColor(arr2, cv2.COLOR_RGB2GRAY)

    orb = cv2.ORB_create(orb_features)
    kp1, des1 = orb.detectAndCompute(gray1, None)
    kp2, des2 = orb.detectAndCompute(gray2, None)

    if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
        return 0.0, "align_failed"

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if not matches:
        return 0.0, "align_failed"

    matches = sorted(matches, key=lambda x: x.distance)
    good_n = max(10, len(matches) // 3)
    matches = matches[:good_n]

    pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])
    if len(pts1) < 4 or len(pts2) < 4:
        return 0.0, "align_failed"
    H, mask = cv2.findHomography(pts2, pts1, cv2.RANSAC, 5.0)

    if H is None or mask is None:
        return 0.0, "align_failed"

    n_inliers = int(mask.sum())
    inlier_ratio = n_inliers / max(1, len(matches))
    if n_inliers < min_inliers or inlier_ratio < min_inlier_ratio:
        # 伪对齐：内点太少，单应矩阵不可信
        return 0.0, "align_failed"

    h, w = arr1.shape[:2]
    arr2 = cv2.warpPerspective(
        arr2, H, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )

    # 像素一致率：每通道差 <= tolerance 算一致（使用 int16 避免 uint8 溢出）
    diff = np.abs(arr1.astype(np.int16) - arr2.astype(np.int16))
    return float(np.all(diff <= tolerance, axis=2).mean()), "aligned"


def _borderline_discriminate(
    arr1: np.ndarray,
    arr2: np.ndarray,
    tolerance: int,
    orb_features: int,
    block: int = 32,
    min_bg_mad: float = 3.0,
) -> tuple[bool, float, float]:
    """
    borderline 二次判别：区分「同一母版 P 图换字」与「同版式真证（两次独立实拍）」。

    原理：
      - P 图同母版：背景/边框/相框/印章区域像素级重合 → 大量 32x32 块的差 异率≈0；
        差异像素集中在被替换的文字行 → 少数块贡献了绝大部分差异像素。
      - 真证独立实拍：光影、纸张噪声处处不同 → 近零块很少，差异弥散分布。

    判据（物理原理：两次独立实拍不可能产出逐像素一致的背景）：
      对齐后计算每个 32x32 块的连续差异幅度 MAD（不用容差阈值化）。
      取幅度最低的 60% 块作为"背景块"，要求背景块 MAD 中位数 < 3.0（v5：由 1.0 放宽）
      （即背景区域像素级一致 → 同一数字母版编辑产物）。
      真证独立实拍时，纸张噪声与光影差异会让背景块 MAD 明显大于 0。

    返回 (is_same_master, bg_mad_median, concentration)。
    对齐失败（ORB 特征不足等）返回 (False, -1, -1) —— 保守不合并。
    """
    gray1 = cv2.cvtColor(arr1, cv2.COLOR_RGB2GRAY)
    gray2 = cv2.cvtColor(arr2, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(orb_features)
    kp1, des1 = orb.detectAndCompute(gray1, None)
    kp2, des2 = orb.detectAndCompute(gray2, None)
    if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
        return False, -1.0, -1.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if not matches:
        return False, -1.0, -1.0
    matches = sorted(matches, key=lambda x: x.distance)
    good_n = max(10, len(matches) // 3)
    matches = matches[:good_n]
    pts1 = np.float32([kp1[m.queryIdx].pt for m in matches])
    pts2 = np.float32([kp2[m.trainIdx].pt for m in matches])
    H, mask = cv2.findHomography(pts2, pts1, cv2.RANSAC, 5.0)
    if H is None or mask is None or int(mask.sum()) < 12:
        return False, -1.0, -1.0
    h, w = arr1.shape[:2]
    arr2w = cv2.warpPerspective(arr2, H, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REPLICATE)
    # 连续差异幅度图（每像素三通道平均绝对差，不做容差阈值化）
    diff = np.abs(arr1.astype(np.int16) - arr2w.astype(np.int16)).mean(axis=2)

    bh, bw = h // block, w // block
    if bh < 2 or bw < 2:
        return False, -1.0, -1.0
    dm = diff[: bh * block, : bw * block].reshape(bh, block, bw, block)
    block_mad = dm.mean(axis=(1, 3)).flatten()

    # 背景块 = MAD 最低的 60%（排除文字区域的大差异块）
    n_bg = max(1, int(len(block_mad) * 0.6))
    bg_mad = np.sort(block_mad)[:n_bg]
    bg_mad_median = float(np.median(bg_mad))

    total = block_mad.sum()
    if total <= 0:
        return True, 0.0, 1.0
    k = max(1, int(len(block_mad) * 0.2))
    concentration = float(np.sort(block_mad)[-k:].sum() / total)

    # v6：不再用背景 MAD 做拒绝判据。理由——MAD 判的是「是否同一份数字文件」，
    # 而非「是否造假」：P 图打印后翻拍、或截图二次压缩，背景 MAD 同样会抬高，
    # 用它拒绝会把真造假漏掉。聚簇阶段只负责出候选，真假交给人工标注裁定，
    # 因此这一层偏召回。MAD 仍然计算并随日志输出，作为观测指标保留。
    is_same = True
    return is_same, bg_mad_median, concentration


def _prefetch_images(
    urls: list[str],
    needed_indices: set[int],
    max_workers: int,
    img_timeout: int,
    max_long_side: int | None,
) -> dict[int, np.ndarray | None]:
    """
    并发预下载所有需要的图片，返回 {idx: arr_or_None}。
    每个 URL 只下载一次，避免在 per-pair 逻辑中重复下载。
    """
    img_cache: dict[int, np.ndarray | None] = {}
    cache_lock = threading.Lock()

    def fetch(idx: int):
        url = urls[idx]
        arr = _load_image_from_url(url, timeout=img_timeout, max_long_side=max_long_side)
        with cache_lock:
            img_cache[idx] = arr

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch, idx) for idx in needed_indices]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # 传播异常（fetch 内部已 catch，不会抛）

    return img_cache


def cluster_pixel_match(
    index: faiss.Index,
    matrix: np.ndarray,
    meta: list,
    url_col: str,
    cosine_topk: int,
    cosine_prefilter: float,
    match_rate_threshold: float,
    pixel_tolerance: int,
    img_timeout: int,
    max_workers: int,
    batch_size: int,
    max_long_side: int | None = None,
    skip_align_cosine: float = 0.99,
    cluster_merge_cosine: float | None = 0.97,
    borderline_low: float = 0.90,
    orb_features: int = 1500,
) -> pd.DataFrame:
    """
    三阶段聚类：
      Stage 1. FAISS 余弦相似度检索每个节点的 top-k 邻居（粗筛），
               收集所有候选边 (i, j)（去重）。
      Stage 1.5. 并发预下载所有候选节点的图片（每张图只下载一次）。
      Stage 2. 对每条候选边从缓存取图 → 可选 ORB 对齐 → 像素一致率精筛，
               保留 match_rate >= match_rate_threshold 的边。
      Stage 3. Union-Find 求连通分量，孤立点 cluster_id = -1。

    速度优化：
      - max_long_side   : 预下载时等比缩放（最长边限制），大幅降低像素比较开销
      - skip_align_cosine: 余弦 >= 此值的对直接跳过 ORB 对齐（省去 Homography）
      - orb_features    : 减少 ORB 特征点数（默认 1500，原为 5000）
      - 图片预缓存       : 每张图只下载一次，消除重复下载
    """
    n = matrix.shape[0]
    V_all = matrix  # 已 L2 归一化的全量向量矩阵
    print(f"\n{'='*60}")
    print(f"[3/4] 像素匹配聚类（FAISS topk + ORB对齐像素一致率）")
    print(f"{'='*60}")
    print(f"  数据量              : {n:,} 条")
    print(f"  URL 列名            : {url_col}")
    print(f"  FAISS top-k         : {cosine_topk}")
    print(f"  余弦预过滤阈值      : {cosine_prefilter}")
    print(f"  像素一致率阈值      : {match_rate_threshold}")
    print(f"  像素容差 tolerance  : ±{pixel_tolerance}")
    print(f"  图片下载并发数      : {max_workers}")
    print(f"  FAISS batch_size    : {batch_size}")
    resize_str = f"最长边≤{max_long_side}px（等比）" if max_long_side else "原始分辨率"
    print(f"  图片缩放尺寸        : {resize_str}")
    print(f"  跳过ORB对齐阈值     : 余弦 >= {skip_align_cosine}")
    print(f"  ORB 特征点数        : {orb_features}")

    # 检查 URL 列是否存在
    if not meta or url_col not in meta[0]:
        available = list(meta[0].keys()) if meta else []
        print(f"  [错误] meta 中不存在列 '{url_col}'")
        print(f"  可用字段: {available}")
        print(f"  请通过 --url-col 指定包含图片 URL 的字段名")
        df = pd.DataFrame(meta)
        df["cluster_id"] = -1
        return df

    # ── Stage 1: FAISS 粗筛，收集候选边 + 记录每对对应的余弦分数 ────────────
    print(f"\n  ── Stage 1: FAISS 余弦 top-{cosine_topk} 粗筛 ──")
    # candidate_pairs: (i, j) -> 最大余弦分数（用于判断是否跳过 ORB）
    candidate_pairs: dict[tuple[int, int], float] = {}
    t0 = time.time()
    n_batches = (n + batch_size - 1) // batch_size
    is_tty = sys.stdout.isatty()
    report_every = max(1, n_batches // 20)

    with tqdm(
        total=n,
        desc="  FAISS 批量检索",
        unit="vec",
        bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        file=sys.stdout,
        dynamic_ncols=True,
        disable=not is_tty,
    ) as pbar:
        for batch_idx, start in enumerate(range(0, n, batch_size)):
            end = min(start + batch_size, n)
            batch = matrix[start:end]
            scores, indices = index.search(batch, cosine_topk + 1)  # +1 含自身

            for i_local, (row_scores, row_indices) in enumerate(zip(scores, indices)):
                i_global = start + i_local
                for score, j in zip(row_scores, row_indices):
                    if j < 0 or j == i_global:
                        continue
                    if score < cosine_prefilter:
                        continue
                    pair = (min(i_global, int(j)), max(i_global, int(j)))
                    # 同一对可能被两个方向都检索到，保留较大余弦值
                    if pair not in candidate_pairs or score > candidate_pairs[pair]:
                        candidate_pairs[pair] = float(score)

            pbar.update(end - start)
            if not is_tty and (batch_idx % report_every == 0 or end == n):
                ts = time.strftime("%H:%M:%S")
                print(f"  [{ts}] FAISS 检索 {end:,}/{n:,} ({end/n*100:.0f}%)"
                      f" 候选对: {len(candidate_pairs):,}", flush=True)

    candidate_list = sorted(candidate_pairs.keys())
    print(f"  FAISS 耗时          : {fmt_time(time.time()-t0)}")
    print(f"  候选边数（去重）    : {len(candidate_list):,}")

    if not candidate_list:
        print("  [警告] 没有候选对，所有节点标记为 -1")
        df = pd.DataFrame(meta)
        df["cluster_id"] = -1
        return df

    # ── Stage 1.5: 预下载所有涉及节点的图片 ────────────────────────────────
    urls = [m.get(url_col, "") for m in meta]
    needed_indices: set[int] = set()
    for i, j in candidate_list:
        needed_indices.add(i)
        needed_indices.add(j)

    n_needed = len(needed_indices)
    print(f"\n  ── Stage 1.5: 图片预下载（{n_needed:,} 张，并发={max_workers}）──")
    t_fetch = time.time()

    # 磁盘缓存：图片解码后存 .npy 文件，img_cache 只存路径，避免全量图片驻留内存导致 OOM
    #
    # 缓存 key 必须基于 URL 内容而非行号（idx）：行号只在单次运行的输入文件内有意义，
    # 日跑场景下同一 idx 每天对应不同图片，用 idx 命名会把 A 账号的图当成 B 账号的图
    # 复用（断点续传逻辑「文件存在即命中」），导致聚簇结果静默错误。
    # 同时把 max_long_side 纳入 key，避免换分辨率参数后复用到旧尺寸的缓存。
    _disk_cache_dir = os.path.join(BASE_DIR, "img_disk_cache")
    os.makedirs(_disk_cache_dir, exist_ok=True)
    _cache_ver = f"L{max_long_side}" if max_long_side else "Lorig"

    def _cache_path(url: str) -> str:
        digest = hashlib.sha1(f"{_cache_ver}|{url}".encode("utf-8")).hexdigest()
        # 两级目录打散，避免单目录下十万级文件影响 inode 查找性能
        sub = os.path.join(_disk_cache_dir, digest[:2])
        os.makedirs(sub, exist_ok=True)
        return os.path.join(sub, f"{digest}.npy")

    img_cache: dict[int, str | None] = {}
    fetch_lock = threading.Lock()
    fetch_done = [0]
    cache_hits = [0]

    def fetch_one(idx: int):
        path = _cache_path(urls[idx])
        if os.path.exists(path):
            # 断点续传：上次运行已缓存（跨天复用同一张图）
            with fetch_lock:
                img_cache[idx] = path
                fetch_done[0] += 1
                cache_hits[0] += 1
                done = fetch_done[0]
        else:
            arr = _load_image_from_url(urls[idx], timeout=img_timeout, max_long_side=max_long_side)
            if arr is not None:
                np.save(path, arr)
            else:
                path = None
            del arr
            with fetch_lock:
                img_cache[idx] = path
                fetch_done[0] += 1
                done = fetch_done[0]
        if not is_tty and done % max(1, n_needed // 20) == 0:
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] 预下载 {done:,}/{n_needed:,} ({done/n_needed*100:.0f}%)", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        fs = [executor.submit(fetch_one, idx) for idx in needed_indices]
        with tqdm(
            total=n_needed,
            desc="  预下载图片",
            unit="张",
            bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            file=sys.stdout,
            dynamic_ncols=True,
            disable=not is_tty,
        ) as pbar_fetch:
            for f in concurrent.futures.as_completed(fs):
                f.result()
                pbar_fetch.update(1)

    n_ok    = sum(1 for v in img_cache.values() if v is not None)
    n_fail  = n_needed - n_ok
    n_hit   = cache_hits[0]
    n_dl    = n_needed - n_hit
    print(f"  预下载耗时          : {fmt_time(time.time()-t_fetch)}")
    print(f"  缓存命中 / 新下载   : {n_hit:,} / {n_dl:,}")
    print(f"  成功 / 失败         : {n_ok:,} / {n_fail:,}")

    # 下载失败率护栏：容器出网走 MITM 代理，未设置 REQUESTS_CA_BUNDLE 时
    # requests 会对所有 https 报 SSLError，而异常被 catch 成 None——
    # 表现为「全部下载失败但进程正常退出、聚簇结果一片空」的假成功。
    # 这里显式判定并中止，避免错误结果流向下游标注平台。
    # 注：高缓存命中场景下 n_dl 可能只有个位数，此时失败率没有统计意义
    # （4/4 失败=100% 但整体可用率仍 99.9%），需同时看整体可用率再决定是否中止。
    MIN_DL_FOR_RATE = 50          # 新下载不足此数不单独判定失败率
    total_ok_rate = (n_ok + n_hit) / n_needed if n_needed else 1.0
    if n_dl >= MIN_DL_FOR_RATE or total_ok_rate < 0.9:
        fail_rate_new = (n_fail / n_dl) if n_dl else 0.0
        if fail_rate_new >= 0.5:
            print(
                f"\n  [致命] 新下载图片失败率 {fail_rate_new*100:.1f}%"
                f"（{n_fail:,}/{n_dl:,}），远超正常水平。"
            )
            print("         常见原因：未设置 REQUESTS_CA_BUNDLE 指向 MITM 代理 CA 证书。")
            print("         请确认运行时带上："
                  "REQUESTS_CA_BUNDLE=${HOME}/.openclaw/mitm-proxy/ca-cert.pem")
            raise RuntimeError(
                f"图片下载失败率过高（{n_fail}/{n_dl}），已中止以避免产出错误聚簇结果"
            )
        if fail_rate_new >= 0.1:
            print(f"  [警告] 新下载图片失败率 {fail_rate_new*100:.1f}%，请关注图片链路健康度")

    # ── Stage 2: 从缓存取图 → 像素匹配 ────────────────────────────────────
    print(f"\n  ── Stage 2: 像素匹配（并发={max_workers}）──")
    valid_edges: list[tuple[int, int]] = []
    skipped_no_img = 0
    t1 = time.time()

    def _load_cached(idx: int):
        p = img_cache.get(idx)
        if p is None:
            return None
        try:
            return np.load(p)
        except Exception:
            return None

    def process_pair(pair: tuple[int, int]) -> tuple[int, int, float, str]:
        i, j = pair
        arr1 = _load_cached(i)
        arr2 = _load_cached(j)
        if arr1 is None or arr2 is None:
            return i, j, 0.0, "no_image"

        # 余弦相似度极高时跳过 ORB 对齐
        cosine_score = candidate_pairs[pair]
        skip_align = cosine_score >= skip_align_cosine

        # 尺寸不一致时用等比 resize 对齐尺寸（原实现用左上角裁剪，
        # 会让同一张图因高宽比不同而内容错位、一致率虚低 → 漏判复用）
        if arr1.shape != arr2.shape:
            h, w = arr1.shape[:2]
            arr2 = cv2.resize(arr2, (w, h), interpolation=cv2.INTER_AREA)

        rate, align_status = _align_and_match(
            arr1, arr2,
            tolerance=pixel_tolerance,
            orb_features=orb_features,
            skip_align=skip_align,
        )
        return i, j, rate, align_status

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_pair, p): p for p in candidate_list}
        done_count = 0
        total_pairs = len(candidate_list)

        with tqdm(
            total=total_pairs,
            desc="  像素匹配",
            unit="对",
            bar_format="  {l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            file=sys.stdout,
            dynamic_ncols=True,
            disable=not is_tty,
        ) as pbar2:
            report_every2 = max(1, total_pairs // 20)
            n_align_failed = 0
            for future in concurrent.futures.as_completed(futures):
                i, j, rate, align_status = future.result()
                done_count += 1
                if align_status == "no_image":
                    skipped_no_img += 1
                elif align_status == "align_failed":
                    n_align_failed += 1
                if rate >= match_rate_threshold:
                    valid_edges.append((i, j))

                pbar2.update(1)
                if not is_tty and (done_count % report_every2 == 0 or done_count == total_pairs):
                    ts = time.strftime("%H:%M:%S")
                    print(
                        f"  [{ts}] 像素匹配 {done_count:,}/{total_pairs:,}"
                        f" ({done_count/total_pairs*100:.0f}%)"
                        f" 有效边: {len(valid_edges):,}",
                        flush=True,
                    )

    print(f"  像素匹配耗时        : {fmt_time(time.time()-t1)}")
    print(f"  图片缺失/跳过       : {skipped_no_img:,}")
    print(f"  ORB对齐失败(未判定) : {n_align_failed:,}")
    print(f"  有效边（match_rate>={match_rate_threshold}）: {len(valid_edges):,}")

    # ── Stage 3: Union-Find 求连通分量 ──────────────────────────────────────
    print(f"\n  ── Stage 3: Union-Find 连通分量 ──")
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, j in valid_edges:
        union(i, j)

    # ── Stage 3.5: 簇间补边（v3 新增）─────────────────────────────────────
    # 背景：Stage1 的 top-k 粗筛存在"邻居名额饱和"——同一模板的图越多，
    # 每张图的 top-k 名额越容易被相似度略高的其他变体占满，导致同模板的
    # 两个子群（如 W<week> 簇3/簇280）从未生成候选边，像素比对根本没跑。
    # 这里在连通分量形成后，对代表向量余弦足够高的簇对取"最像的成员对"
    # 直接补一次像素比对，通过则合并两簇。
    n_cluster_merges = 0
    n_borderline_tried = 0
    n_borderline_merged = 0
    if cluster_merge_cosine is not None:
        print(f"\n  ── Stage 3.5: 簇间补边（代表余弦 >= {cluster_merge_cosine}）──")
        t25 = time.time()
        # 先收集当前连通分量（size>=2）
        _root_members: dict[int, list[int]] = defaultdict(list)
        for i in range(n):
            _root_members[find(i)].append(i)
        comp = [m for m in _root_members.values() if len(m) >= 2]
        if len(comp) >= 2:
            # 每个簇的归一化代表向量（成员均值）
            reps = []
            for members in comp:
                v = V_all[members].mean(axis=0)
                v = v / (np.linalg.norm(v) or 1.0)
                reps.append(v)
            R = np.stack(reps)
            rep_sim = R @ R.T
            n_cand = 0
            merge_pairs = []
            for a in range(len(comp)):
                for b in range(a + 1, len(comp)):
                    if rep_sim[a, b] < cluster_merge_cosine:
                        continue
                    n_cand += 1
                    # 簇内最像的成员对
                    Va = V_all[comp[a]]
                    Vb = V_all[comp[b]]
                    S = Va @ Vb.T
                    ia, ib = np.unravel_index(S.argmax(), S.shape)
                    if S[ia, ib] < cluster_merge_cosine:
                        continue
                    merge_pairs.append((comp[a][ia], comp[b][ib], float(S[ia, ib])))
            print(f"  候选簇对（代表余弦达标）: {n_cand:,}，成员对过筛: {len(merge_pairs):,}")

            def _load25(idx: int):
                p = img_cache.get(idx)
                if p is None:
                    return None
                try:
                    return np.load(p)
                except Exception:
                    return None

            for i, j, cos_ij in merge_pairs:
                arr1 = _load25(i)
                arr2 = _load25(j)
                if arr1 is None or arr2 is None:
                    continue
                if arr1.shape != arr2.shape:
                    h, w = arr1.shape[:2]
                    arr2 = cv2.resize(arr2, (w, h), interpolation=cv2.INTER_AREA)
                rate, _status = _align_and_match(
                    arr1, arr2,
                    tolerance=pixel_tolerance,
                    orb_features=orb_features,
                    skip_align=False,
                )
                if rate >= match_rate_threshold:
                    if find(i) != find(j):
                        union(i, j)
                        n_cluster_merges += 1
                elif rate >= borderline_low:
                    # borderline 二次判别（v4 新增）：
                    # 0.90 <= rate < 0.95 区间真假混杂——同一母版 P 图换字（差异集中在
                    # 文字行、背景像素级重合）与同版式真证（两次独立实拍、差异弥散）
                    # 都会落在这里。用差异像素的空间分布做判别：P 图同母版应有大量
                    # 近零差异块 + 差异高度集中于少数块；真证差异弥散于整面。
                    ok, bg_mad, conc = _borderline_discriminate(
                        arr1, arr2,
                        tolerance=pixel_tolerance,
                        orb_features=orb_features,
                    )
                    n_borderline_tried += 1
                    if ok:
                        if find(i) != find(j):
                            union(i, j)
                            n_cluster_merges += 1
                            n_borderline_merged += 1
                            print(f"    [borderline合并] rate={rate:.4f} 背景MAD={bg_mad:.2f} 集中度={conc:.2f}")
                        n_cluster_merges += 1
        print(f"  簇间合并次数        : {n_cluster_merges:,}")
        print(f"  borderline判别      : 尝试 {n_borderline_tried:,} 次，通过 {n_borderline_merged:,} 次")
        print(f"  Stage 3.5 耗时     : {fmt_time(time.time()-t25)}")

    t2 = time.time()
    root_members: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        root_members[find(i)].append(i)

    sorted_roots = sorted(root_members.items(), key=lambda x: len(x[1]), reverse=True)
    labels = np.full(n, -1, dtype=np.int32)
    cluster_id = 0
    for root, members in sorted_roots:
        if len(members) >= 2:
            for m in members:
                labels[m] = cluster_id
            cluster_id += 1

    n_clusters = cluster_id
    n_isolated = int((labels == -1).sum())
    print(f"  Union-Find 耗时     : {fmt_time(time.time()-t2)}")
    print(f"  连通分量数（size≥2）: {n_clusters:,}")
    print(f"  孤立点              : {n_isolated:,} ({n_isolated/n*100:.1f}%)")

    df = pd.DataFrame(meta)
    df["cluster_id"] = labels
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 聚类结果分析 & 输出
# ══════════════════════════════════════════════════════════════════════════════

def analyze_and_save(
    df: pd.DataFrame,
    output_csv: str,
    suspect_csv: str,
    id_col: str | None = None,
    min_suspect_size: int = 2,
) -> None:
    """
    通用版结果分析。

    df 包含任意 meta 字段 + cluster_id 列。
    若指定 id_col（唯一身份列），则额外输出可疑簇（同一簇多个不同身份）。
    """
    print(f"\n{'='*60}")
    print(f"[4/4] 结果分析 & 保存")
    print(f"{'='*60}")

    # 完整结果落盘
    t0 = time.time()
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    print(f"  完整结果已保存 : {output_csv}  ({len(df):,} 行, 耗时 {fmt_time(time.time()-t0)})")
    print(f"  列名           : {list(df.columns)}")

    # ── 簇统计 ──────────────────────────────────────────────────────────────
    valid = df[df["cluster_id"] >= 0]
    total_valid = len(valid)
    n_noise = len(df) - total_valid

    print(f"\n  ── 聚类概览 ──")
    print(f"  总向量数         : {len(df):,}")
    print(f"  有效聚类点       : {total_valid:,} ({total_valid/len(df)*100:.1f}%)")
    print(f"  噪声/孤立点(-1)  : {n_noise:,} ({n_noise/len(df)*100:.1f}%)")

    if total_valid == 0:
        print("  [警告] 没有有效聚类点，请调整参数后重试")
        return

    # 通用簇大小统计（只依赖 cluster_id）
    cluster_sizes = valid.groupby("cluster_id").size().rename("cluster_size").reset_index()
    n_clusters = len(cluster_sizes)
    print(f"  有效簇数         : {n_clusters:,}")
    print(f"  平均簇大小       : {total_valid/n_clusters:.1f}")
    print(f"  最大簇大小       : {cluster_sizes['cluster_size'].max()}")

    print(f"\n  ── 簇大小分布 ──")
    bins       = [2, 3, 5, 10, 20, 50, 100, float("inf")]
    labels_bins = ["2", "3-4", "5-9", "10-19", "20-49", "50-99", "≥100"]
    for lo, hi, lbl in zip(bins[:-1], bins[1:], labels_bins):
        cnt = ((cluster_sizes["cluster_size"] >= lo) & (cluster_sizes["cluster_size"] < hi)).sum()
        bar = "█" * min(cnt, 40)
        print(f"    size {lbl:>6}: {cnt:>6} 个簇  {bar}")

    # ── 可疑簇：需要 id_col ─────────────────────────────────────────────────
    if id_col is None:
        print(f"\n  [提示] 未指定 --id-col，跳过可疑簇分析")
        print(f"         可通过 --id-col <列名> 指定唯一身份列以输出可疑簇")
    elif id_col not in df.columns:
        print(f"\n  [警告] 指定的 --id-col '{id_col}' 不存在于结果中，跳过可疑簇分析")
        print(f"         可用列: {list(df.columns)}")
    else:
        print(f"\n  ── 可疑簇（同一簇 ≥{min_suspect_size} 个不同 {id_col}）──")

        # 通用聚合：cluster_size + n_unique_ids + 各列汇总
        agg_dict = {
            "cluster_size": (id_col, "count"),
            f"n_unique_{id_col}": (id_col, "nunique"),
        }
        cluster_stats = (
            valid.groupby("cluster_id")
            .agg(**agg_dict)
            .reset_index()
            .sort_values("cluster_size", ascending=False)
        )

        suspect = (
            cluster_stats[cluster_stats[f"n_unique_{id_col}"] >= min_suspect_size]
            .sort_values([f"n_unique_{id_col}", "cluster_size"], ascending=False)
            .copy()
        )
        suspect.to_csv(suspect_csv, index=False, encoding="utf-8-sig")

        print(f"  可疑簇数         : {len(suspect):,}")
        print(f"  可疑簇已保存     : {suspect_csv}")

        if len(suspect) > 0:
            print(f"\n  TOP 15 可疑簇（按共用 {id_col} 数排序）:")
            id_unique_col = f"n_unique_{id_col}"
            print(f"  {'cluster_id':>10} {'n_unique':>10} {'size':>6}")
            print("  " + "─" * 40)
            for _, row in suspect.head(15).iterrows():
                print(f"  {int(row['cluster_id']):>10} {int(row[id_unique_col]):>10} {int(row['cluster_size']):>6}")

    print(f"\n{'='*60}")
    print("完成！")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="对 parquet embedding 进行聚类（通用版，只需包含 embeds 列）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python cluster.py                                         # HDBSCAN 默认参数
  python cluster.py --method hdbscan --min-cluster-size 5  # 调大最小簇
  python cluster.py --method kmeans --k 500                # K-Means k=500
  python cluster.py --method threshold --threshold 0.98    # 高相似度分组

  # 像素匹配（基础用法）
  python cluster.py --method pixel_match --url-col qualification_url --id-col user_id

  # 像素匹配（速度优化：缩小分辨率 + 更大并发 + 更少 ORB 特征点）
  python cluster.py --method pixel_match --url-col qualification_url \\
      --max-long-side 512 --max-workers 64 --orb-features 500 --skip-align-cosine 0.97
        """,
    )

    # 公共参数
    parser.add_argument(
        "--input-file",
        default=None,
        help="直接读取 CSV/XLSX/parquet 输入文件；文件需包含 embeds 列。传入后不再需要 --index/--meta",
    )
    parser.add_argument(
        "--sheet",
        default=0,
        help="读取 Excel 时的 sheet 名或序号（默认第一个 sheet）。CSV/parquet 会忽略该参数",
    )
    parser.add_argument(
        "--sample-file",
        action="append",
        default=None,
        help="抽样用户表，可重复传入多个文件。聚类完成后会按抽样 user_id 命中有效簇，并扩展同簇账号",
    )
    parser.add_argument(
        "--sample-id-col",
        default="user_id",
        help="抽样用户表中的用户ID列名（默认 user_id）",
    )
    parser.add_argument(
        "--sample-group-col",
        default="sample_group",
        help="抽样用户表中的样本组列名（默认 sample_group；不存在时使用文件名）",
    )
    parser.add_argument(
        "--sample-sheet",
        default=0,
        help="读取 Excel 抽样用户表时的 sheet 名或序号（默认第一个 sheet）",
    )
    parser.add_argument(
        "--sample-expanded-output",
        default=os.path.join(BASE_DIR, "sample_expanded_clusters.csv"),
        help="抽样命中簇扩展后的输出 CSV",
    )
    parser.add_argument(
        "--sample-match-output",
        default=os.path.join(BASE_DIR, "sample_matched_rows.csv"),
        help="抽样 user_id 与全量聚簇结果直接匹配的明细输出 CSV",
    )
    parser.add_argument("--index",   default=DEFAULT_INDEX_FILE,  help="FAISS 索引路径")
    parser.add_argument("--meta",    default=DEFAULT_META_FILE,   help="元数据 pkl 路径")
    parser.add_argument("--output",  default=DEFAULT_OUTPUT_CSV,  help="完整聚类结果 CSV")
    parser.add_argument("--suspect", default=DEFAULT_SUSPECT_CSV, help="可疑簇 CSV")
    parser.add_argument(
        "--id-col",
        default=None,
        help="meta 中用于唯一身份的列名（用于可疑簇分析），不指定则跳过可疑簇输出",
    )
    parser.add_argument(
        "--min-users", type=int, default=2,
        help="可疑簇内最少不同身份数（默认 2）",
    )
    parser.add_argument(
        "--method",
        choices=["hdbscan", "kmeans", "threshold", "pixel_match"],
        default="hdbscan",
        help="聚类方法（默认 hdbscan）",
    )

    # HDBSCAN 参数
    g1 = parser.add_argument_group("HDBSCAN 参数（--method hdbscan，默认）")
    g1.add_argument("--pca-dim",          type=int, default=64,
                    help="PCA 降维维度（默认 64；设为 0 跳过 PCA）")
    g1.add_argument("--min-cluster-size", type=int, default=3,
                    help="最小簇大小，越小发现越多小簇（默认 3）")
    g1.add_argument("--min-samples",      type=int, default=2,
                    help="核心点判定邻居数，越大越保守（默认 2）")

    # K-Means 参数
    g2 = parser.add_argument_group("K-Means 参数（--method kmeans）")
    g2.add_argument("--k",     type=int, default=500, help="聚类数 k（默认 500）")
    g2.add_argument("--niter", type=int, default=30,  help="迭代次数（默认 30）")
    g2.add_argument("--seed",  type=int, default=42,  help="随机种子（默认 42）")

    # Threshold 参数
    g3 = parser.add_argument_group("阈值连通分量参数（--method threshold）")
    g3.add_argument("--threshold",  type=float, default=0.98,
                    help="余弦相似度阈值，越高越严格（默认 0.98）")
    g3.add_argument("--top-k",      type=int,   default=10,
                    help="每个向量检索 top-k 邻居（默认 10）")
    g3.add_argument("--batch-size", type=int,   default=2048,
                    help="FAISS 批量检索 batch size（默认 2048）")

    # Pixel Match 参数
    g4 = parser.add_argument_group("像素匹配聚类参数（--method pixel_match）")
    g4.add_argument("--url-col",              default=None,
                    help="meta 中包含图片 URL 的列名（pixel_match 必填）")
    g4.add_argument("--cosine-topk",          type=int,   default=20,
                    help="Stage1 FAISS 每节点检索 top-k 邻居（默认 20）")
    g4.add_argument("--cosine-prefilter",     type=float, default=0.80,
                    help="Stage1 余弦预过滤阈值，低于此值的候选对直接丢弃（默认 0.80）")
    g4.add_argument("--match-rate-threshold", type=float, default=0.85,
                    help="Stage2 像素一致率阈值，高于此值才建边（默认 0.85）")
    g4.add_argument("--pixel-tolerance",      type=int,   default=10,
                    help="像素值容差：每通道差值 <= tolerance 算一致（默认 10）")
    g4.add_argument("--img-timeout",          type=int,   default=30,
                    help="图片下载超时秒数（默认 30）")
    g4.add_argument("--max-workers",          type=int,   default=16,
                    help="图片下载+匹配并发线程数（默认 16）")
    g4.add_argument("--pm-batch-size",        type=int,   default=2048,
                    help="Stage1 FAISS 批量检索 batch size（默认 2048）")
    # ── 速度优化参数 ──
    g4.add_argument(
        "--max-long-side",
        type=int, default=None, metavar="PX",
        help="预下载时等比缩放图片，使最长边不超过 PX 像素（例如 --max-long-side 512），"
             "大幅加速像素比较，宽高比保持不变；不指定则保持原始分辨率",
    )
    g4.add_argument(
        "--skip-align-cosine",
        type=float, default=0.99,
        help="余弦相似度 >= 此值时跳过 ORB+Homography 对齐，直接比像素"
             "（默认 0.99；设为 1.1 则永不跳过）",
    )
    g4.add_argument(
        "--orb-features",
        type=int, default=1500,
        help="ORB 检测的最大特征点数（默认 1500；越小越快，越大对齐越准）",
    )
    # ── v3 新增：簇间补边参数 ──
    g4.add_argument(
        "--cluster-merge-cosine",
        type=float, default=0.97,
        help="Stage 3.5 簇间补边：代表向量余弦 >= 此值的簇对，取最像成员对补跑像素比对，通过则合并（默认 0.97）",
    )
    g4.add_argument(
        "--no-cluster-merge",
        action="store_true",
        help="关闭 Stage 3.5 簇间补边（行为退回 v2）",
    )
    g4.add_argument(
        "--borderline-low",
        type=float, default=0.90,
        help="borderline 二次判别下限：像素一致率落在 [此值, match_rate_threshold) 的簇对走空间集中度判别（默认 0.90；设为 1.0 关闭）",
    )

    args = parser.parse_args()

    # pixel_match 模式下校验 --url-col
    if args.method == "pixel_match" and not args.url_col:
        parser.error("--method pixel_match 需要通过 --url-col 指定图片 URL 所在的列名")

    total_t0 = time.time()
    print(f"\n聚类方法: {args.method.upper()}")
    print(f"开始时间: {time.strftime('%H:%M:%S')}")

    # Step 1 & 2: 加载输入
    # - 传 --input-file：直接从 CSV/XLSX/parquet 读取 embeds，构建内存索引
    # - 不传：保持原逻辑，从 faiss_index.bin + faiss_meta.pkl 加载
    if args.input_file:
        sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
        index, meta, matrix = load_tabular_input(args.input_file, sheet=sheet)
    else:
        index, meta = load_index_and_meta(args.index, args.meta)
        matrix = reconstruct_matrix(index)

    # Step 3: 聚类
    if args.method == "hdbscan":
        pca_dim = args.pca_dim if args.pca_dim > 0 else matrix.shape[1]
        df = cluster_hdbscan(
            matrix, meta,
            pca_dim=pca_dim,
            min_cluster_size=args.min_cluster_size,
            min_samples=args.min_samples,
        )
    elif args.method == "kmeans":
        df = cluster_kmeans(matrix, meta, k=args.k, niter=args.niter, seed=args.seed)
    elif args.method == "threshold":
        df = cluster_threshold(
            index, matrix, meta,
            threshold=args.threshold,
            top_k=args.top_k,
            batch_size=args.batch_size,
        )
    else:  # pixel_match
        df = cluster_pixel_match(
            index, matrix, meta,
            url_col=args.url_col,
            cosine_topk=args.cosine_topk,
            cosine_prefilter=args.cosine_prefilter,
            match_rate_threshold=args.match_rate_threshold,
            pixel_tolerance=args.pixel_tolerance,
            img_timeout=args.img_timeout,
            max_workers=args.max_workers,
            batch_size=args.pm_batch_size,
            max_long_side=args.max_long_side,
            skip_align_cosine=args.skip_align_cosine,
            cluster_merge_cosine=(None if args.no_cluster_merge else args.cluster_merge_cosine),
            borderline_low=args.borderline_low,
            orb_features=args.orb_features,
        )

    # Step 4: 分析 & 保存
    analyze_and_save(
        df,
        output_csv=args.output,
        suspect_csv=args.suspect,
        id_col=args.id_col,
        min_suspect_size=args.min_users,
    )

    if args.sample_file:
        sample_sheet = int(args.sample_sheet) if str(args.sample_sheet).isdigit() else args.sample_sheet
        expand_clusters_by_sample_users(
            df=df,
            sample_files=args.sample_file,
            sample_id_col=args.sample_id_col,
            sample_group_col=args.sample_group_col,
            sample_sheet=sample_sheet,
            expanded_output=args.sample_expanded_output,
            match_output=args.sample_match_output,
        )

    total_elapsed = time.time() - total_t0
    print(f"总耗时: {fmt_time(total_elapsed)}")
