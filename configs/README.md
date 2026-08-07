# 配置目录

`environment.example` 是脱敏模板，只能复制到用户配置目录后再填写真实上游地址或运行参数。

默认外置配置路径：

```text
$HOME/.config/codex-model-fallback-proxy-standalone/environment
```

真实环境文件不属于仓库，不应提交、打包或写入日志。systemd 通过 `EnvironmentFile` 读取它。
