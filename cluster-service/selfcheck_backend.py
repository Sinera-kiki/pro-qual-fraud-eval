#!/usr/bin/env python3
"""后端静态自查：psycopg3 用法与常见崩点，部署前必跑。

这些检查每一条都对应一次真实的线上 500（纯文本 Internal Server Error，
平台不给堆栈，只能靠静态规则提前拦住）：

A1 事务块内禁止 conn.commit()   —— psycopg3 ProgrammingError（实际踩过）
A2 禁止 psycopg2 的 conn.lobject() —— psycopg3 已移除（实际踩过）
A3 代码里用到的模块必须已 import  —— base64 漏 import（实际踩过）
A4 CORS 不得使用通配符           —— 安全扫描告警（实际踩过）
A5 requirements 必须含 Form/File 依赖 python-multipart（实际踩过，Pod 起不来）
A6 /health 必须挂在顶层且不碰 DB —— 探活失败会整体 502
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

APP = Path(__file__).parent / "app.py"
REQ = Path(__file__).parent / "requirements.txt"

results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(f'[{"PASS" if ok else "FAIL"}] {name}' + (f" — {detail}" if detail else ""))
    return ok


src = APP.read_text(encoding="utf-8")
lines = src.splitlines()
tree = ast.parse(src)


# ── A1 事务块内的 commit ────────────────────────────────────────────
class TxnCommitVisitor(ast.NodeVisitor):
    def __init__(self):
        self.bad = []

    def visit_With(self, node):
        is_txn = any(
            isinstance(it.context_expr, ast.Call)
            and isinstance(it.context_expr.func, ast.Attribute)
            and it.context_expr.func.attr == "transaction"
            for it in node.items
        )
        if is_txn:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "commit"):
                    self.bad.append(sub.lineno)
        self.generic_visit(node)


v = TxnCommitVisitor()
v.visit(tree)
check("A1 事务块内无 conn.commit()", not v.bad,
      f"违规行号 {v.bad}" if v.bad else "")

# ── A2 psycopg2 遗留 API ────────────────────────────────────────────
lo_hits = [i + 1 for i, l in enumerate(lines)
           if re.search(r"\.lobject\s*\(", l) and not l.strip().startswith("#")]
check("A2 无 psycopg2 的 .lobject()（psycopg3 已移除）", not lo_hits,
      f"违规行号 {lo_hits}" if lo_hits else "")

# ── A3 使用的模块都已 import ────────────────────────────────────────
imported = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        for a in n.names:
            imported.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.ImportFrom):
        for a in n.names:
            imported.add(a.asname or a.name)

WATCH = {"base64", "io", "json", "os", "re", "psycopg", "traceback", "csv", "time"}
used_missing = []
for mod in WATCH:
    # 顶层 import 缺失但代码里有 mod.xxx 调用
    if mod not in imported and re.search(rf"\b{mod}\.\w+", src):
        # 允许函数内局部 import
        if not re.search(rf"^\s+import {mod}\b", src, re.M):
            used_missing.append(mod)
check("A3 使用到的模块均已 import", not used_missing,
      f"缺失 {used_missing}" if used_missing else "")

# ── A4 CORS 通配符 ──────────────────────────────────────────────────
wildcard = bool(re.search(r'allow_origins\s*=\s*\[\s*["\']\*["\']', src))
check("A4 CORS 未使用通配符 *", not wildcard,
      "allow_origins=['*'] 会触发安全扫描告警" if wildcard else "")

# ── A5 requirements 依赖 ────────────────────────────────────────────
req = REQ.read_text(encoding="utf-8") if REQ.exists() else ""
needs_multipart = bool(re.search(r"\b(Form|File)\s*\(", src))
has_multipart = "python-multipart" in req
check("A5 用了 Form/File 则 requirements 含 python-multipart",
      (not needs_multipart) or has_multipart,
      "缺 python-multipart，fastapi 导入即崩、Pod 起不来 502"
      if needs_multipart and not has_multipart else "")

# ── A6 /health 顶层且不碰 DB ────────────────────────────────────────
health_fn = None
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef):
        for d in n.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr == "get" and d.args
                    and isinstance(d.args[0], ast.Constant)
                    and d.args[0].value == "/health"):
                health_fn = n
health_clean = health_fn is not None and not any(
    isinstance(s, ast.Call) and isinstance(s.func, ast.Name)
    and s.func.id == "_get_db_conn" for s in ast.walk(health_fn))
check("A6 /health 存在且不依赖 DB", health_clean,
      "探活不能依赖 DB，否则 DB 抖动导致整体 502")

n_fail = sum(1 for ok, _, _ in results if not ok)
print(f'\n=== {len(results)-n_fail}/{len(results)} 通过 ===')
if n_fail:
    print("❌ 有 FAIL，禁止部署")
sys.exit(1 if n_fail else 0)
