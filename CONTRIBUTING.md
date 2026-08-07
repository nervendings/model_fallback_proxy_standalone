# 贡献方式

本项目的 `main` 分支仅接受 Pull Request（PR）合并：

- 禁止直接推送 `main`。
- PR 至少需要 1 个批准。
- 管理员同样受保护规则约束。
- 禁止强制推送和删除 `main`。
- 提交 PR 前运行 `scripts/quality/check_repo_quality.sh`。

建议流程：

```bash
git switch -c feature/<short-name>
scripts/quality/check_repo_quality.sh
git push -u origin feature/<short-name>
gh pr create --base main
```

不要提交真实 environment、token、cookie、状态文件、日志或本机路径。
