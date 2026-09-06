# Contributing to Pro-Qual-Fraud-Eval

感谢关注本项目！本文档说明如何贡献代码、报告问题、以及本仓库的工程规范。

## 报告问题

请通过 [GitHub Issues](../../issues) 提交问题，附带：
- 复现步骤
- 期望行为 vs 实际行为
- 环境信息（Python 版本、Node 版本、操作系统）

## 提交 Pull Request

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/your-feature-name`
3. 提交改动，遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范
4. 推送到你的 Fork：`git push origin feature/your-feature-name`
5. 发起 Pull Request

## 工程规范

### Python 代码
- 使用 Python 3.10+
- 遵循 PEP 8 规范
- 所有 Python 文件必须能通过 `py_compile` 编译（CI 会检查）

### 前端代码
- React 18 + TypeScript + Tailwind CSS
- 提交前必须能通过 `npm run build`（CI 会检查）

### 敏感信息
- 严禁提交任何硬编码的密码、Token、内网域名、真实用户数据
- CI 会自动扫描敏感模式，触发即阻断合并

### 提交信息规范

```
<type>(<scope>): <subject>

例：
feat(pipeline): add borderline MAD threshold auto-tuning
fix(dashboard): correct violation ratio denominator
docs(readme): update quick start guide
```

Type 可选：`feat` · `fix` · `docs` · `style` · `refactor` · `test` · `chore`

## License

提交即代表你同意以 MIT License 授权你的贡献。
