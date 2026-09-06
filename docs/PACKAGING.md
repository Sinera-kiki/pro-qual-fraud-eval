# PACKAGING.md — 本仓库是怎么打包出来的

记录一次典型的"从内网脚本堆脱敏成公开仓库骨架"的完整流程，便于以后重跑或复用到其他项目。

## 起点：内网工作目录长什么样

原始工作目录里混着六类东西，需要区分对待：

| 类型 | 举例 | 是否进仓库 |
|---|---|---|
| 主脚本 | 聚簇 / 推送看板 / 周度自检等主流程脚本 | ✅ 进 |
| 历史版本 | 同一脚本的多个演进版本 v2 ~ v5 | ❌ 不进（Git 历史记录足够） |
| SQL 模板 | 取数模板、周度回补模板 | ✅ 进 |
| 周度产物 | 每周产出的目录、日跑目录（合计几百 MB） | ❌ 不进（数据） |
| 图片缓存 | 图片下载缓存（GB 级） | ❌ 不进（生成物） |
| 探查 tmp | 一次性排查的 tmp_*.py/sql/log/json | ❌ 不进（噪声） |
| 明细数据 | 含账户 ID 的 CSV / Excel | ❌ 不进（PI 敏感） |
| 虚拟环境 | `.venv/`, `__pycache__/` | ❌ 不进（重建即可） |

## 步骤 1：搭骨架

按功能模块拆四层目录：

```
src/
├── pipeline/     # 主管线
├── dashboard/    # 看板上报
├── backfill/     # 回补 + 自检
└── utils/        # 汇总绘图
sql/              # 取数模板
scripts/          # 编排 shell
tests/            # 回归测试
docs/             # 文档
```

历史版本处理原则：**只保留当前基线**（README 里已经写明算法要点，历史迭代靠 Git commit history 记录，不用把废弃版本永久放在 `legacy/`）。

## 步骤 2：脱敏扫描

要脱敏的六类字面量：

1. **内网域名 / URL**：公司二级域、内部看板路径
2. **数据仓库表前缀**：数仓 catalog 名、业务库名、DWD/DWS/ODS 前缀
3. **邮箱 / 内部工号 / 明文 access token**
4. **文档系统的资产 ID**（32 位 hex 一类）
5. **代码里硬编码的密钥字面量**
6. **具体业务名词与人名**（行业名、协作者姓名、周数编号）

用 `grep -rE` 对每一类跑一遍，看命中面覆盖到哪些文件。

## 步骤 3：批量替换

写一个 `scrub.py` 一次性处理所有类型：

```python
LITERAL = [
    # 内网 URL → 通用占位
    ("<内网二级域>",                    "example.com"),
    # SSO 硬编码路径 → 环境变量
    ("<绝对路径>/sso.json",             "${SSO_COOKIE_FILE}"),
    # 明文 token → 环境变量
    ("<硬编码 token>",                  'os.environ.get("DASH_PUSH_TOKEN", "")'),
    # 数据仓库表前缀 → 抽象命名
    ("<数仓 catalog>.<内部表>",         "warehouse.dws_xxx_df"),
    # 内部平台代号
    ("<内部平台名>",                    "Dashboard"),
]

REGEX = [
    (re.compile(r"\b[0-9a-f]{32}\b"),                       "<REDOC_SHORTCUT_ID>"),
    (re.compile(r"\b[a-zA-Z][\w.+-]*@<内网邮箱域>\b"),       "user@example.com"),
    (re.compile(r"\b1[3-9]\d{9}\b"),                        "1XXXXXXXXXX"),
    (re.compile(r"\bW\d{2}\b"),                             "W<week>"),
]
```

> ⚠️ 这份对照表在真实运行时要填入具体字面量。**这份文档故意不写死原始字符串**，避免把原本要脱敏掉的东西又原样贴回文档里。真实的 `scrub.py` 只在内网工作目录使用，不进公开仓库。

### 三个容易踩的坑

1. **长字符串先替换**。业务库名 `redxxx_a.dws_xxx_df` 要先于短前缀 `redxxx_a.` 处理，否则会被截半。
2. **替换后可能破坏语法**。原代码硬编码 token 时没有 `import os`，脱敏成 `os.environ.get(...)` 就必须补上 import。
3. **替换后可能破坏字符串**。硬编码路径 `open("/home/xxx/sso.json")` 换成占位 shell 变量语法是坏的——Python 不认。必须写成：

```python
open(os.path.expanduser(os.environ.get("SSO_COOKIE_FILE", "~/.config/pqfe/sso.json")))
```

## 步骤 4：脱敏后的语法验证

```bash
find src tests -name '*.py' -print0 | xargs -0 -I{} \
  python3 -c "import py_compile; py_compile.compile('{}', doraise=True)"
```

所有 Python 必须能编译通过。这一步能一次性抓出所有因替换导致的语法破坏。

## 步骤 5：注释里的口径细节

代码脱敏容易，**docstring 里的业务细节容易漏**。要抹掉：

- 具体行业名 → 抽象为 `SPECIAL_INDUSTRY_A`
- 具体周数 → `W<week>`
- 具体接口版本号、协作者姓名、内部平台版本演进日志
- 内部平台代号 → 通用词（如 "标注平台" 改成 "annotation platform"）

要保留：

- 算法思路（DHash + 像素比对 + borderline 判别）
- 公式推导（样本量、置信度）
- 设计原因（为什么用背景 MAD、为什么不合并孤立簇）

原则：**能让外部读者读懂算法即可，让他们复用具体业务口径反而多余**。

## 步骤 6：工程配置

- `README.md`：项目概述 + 目录结构 + 主要工作流 + 每个脚本的命令行示例
- `LICENSE`：MIT
- `.gitignore`：排除数据、缓存、密钥
- `.env.example`：环境变量样板
- `requirements.txt`：Python 依赖

## 步骤 7：git init & 打包

```bash
cd project-name/
git init -b main
git add -A
git commit -m "Initial commit: project scaffolding"

# 打 tar.gz（避免 __pycache__）
cd ..
tar --exclude='__pycache__' -czf project-name.tar.gz project-name
```

## 步骤 8：最终脱敏复查

推送到公开仓库之前，再跑一次完整的敏感扫描，确认零残留。**这一步至少要跑两次**——第一次可能会漏文档自身；把误报（如脱敏文档里描述过程时不得不引用的模式）显式加入 allowlist，第二次跑到真正的净空。

无输出即安全。这个正则可以固化成一个 `pre-push` git hook，防止后续 commit 意外带回内部信息。

## 最终产物（初版）

> 注：下表为 2026-09-01 初次打包时的规模。后续已扩展为多模块仓库（新增聚类跑批服务、高危模板入库、归因脚本等），规模不再适用，仅作为工程记录保留。

| 项 | 数量 |
|---|---|
| Python 脚本 | 20 |
| SQL 模板 | 2 |
| Shell 脚本 | 2 |
| 工程配置 | 6 (README/LICENSE/gitignore/.env.example/requirements.txt/PACKAGING.md) |
| 归档大小 | ~200 KB (tar.gz) |
