# 项目开发约定

## 目录职责

- `src/model_fallback_proxy/`：运行时代码，按 Python package 导入。
- `tests/`：标准库 `unittest` 测试。
- `scripts/runtime/`：本地和 systemd 共用的启动入口。
- `scripts/quality/`：语法、测试、差异和敏感信息门禁。
- `configs/`：脱敏配置模板，不放真实环境文件。
- `systemd/`：用户级 systemd unit 模板。
- `docs/`：迁移和维护文档。

## 常用验证

```bash
scripts/quality/check_repo_quality.sh
```

服务启动必须通过 `scripts/runtime` 或 `python -m model_fallback_proxy...`，不要恢复根目录脚本入口。真实上游地址、飞书参数和 token 只允许存在于用户配置目录。
