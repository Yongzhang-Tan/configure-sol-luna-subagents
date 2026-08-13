[English](README.md) | [简体中文](README.zh-CN.md)

# 配置 Sol → Luna 子代理

本仓库提供一个跨 Linux、macOS 和 Windows 的 Codex skill，用于建立简洁的
全局子代理基线：主线程使用 Sol，两个职责明确的子代理使用 Luna。它只修改
全局 Codex home，不扫描或修改项目目录、项目指令、skills、hooks、MCP
服务器、provider、信任设置、审批或 sandbox 设置。

推荐使用 Python 3.11 或更高版本，因为它自带标准库 `tomllib`。Python 3.10
在已经安装 `tomli` 时也受支持。skill 不会自动安装依赖；如果两者都不可用，
请报告缺少 TOML 解析器。
模型访问权限必须已经由 Codex 客户端提供。

## 安装与使用

最终 skill URL：

`https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents`

### 一句话入口

向 Codex 发送：

`$skill-installer 从 <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents> 安装 configure-sol-luna-subagents，并立即按其 SKILL.md 完成全局配置。`

这个入口依赖当前 Codex 客户端在同一轮中安装后立即发现并读取新的
`SKILL.md`。如果客户端延迟发现新 skill，请使用标准两步入口。

### 标准两步入口

第一轮：

`$skill-installer 安装 <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents>`

下一轮：

`$configure-sol-luna-subagents`

skill 默认执行全局安装。它优先读取环境变量 `CODEX_HOME`，否则使用
`~/.codex`。调用本身已授权这项精确的全局配置变更，因此不会再次询问确认。
如果发现无效 TOML、两个命名空间 agent 的未管理同名文件、损坏的 managed
block，或其他无法安全合并的歧义，则会停止而不会猜测修改。

安装成功后请新开 Codex 会话或重启客户端，使全局配置生效。已有会话不会
追溯加载新的 agent 配置。

## 配置结果

- 主线程：`gpt-5.6-sol`，最大推理强度。
- `sol_luna_code_mapper`：`gpt-5.6-luna`，medium，只读。
- `sol_luna_implementation_worker`：`gpt-5.6-luna`，max，workspace-write。
- 任意时刻最多一个 implementation writer。
- Luna worker 可以针对具体缺陷或失败验证做一次定向修正；仍不能完成时，
  回到 Sol 主线程复核、重新规划或完成任务。
- 子代理不会因此获得继续委派、访问远程系统、安装包或执行破坏性操作的权限。

原生配置还会移除活动 `[agents].max_threads` 旧键，并设置
`max_depth = 1` 和 `max_concurrent_threads_per_session = 3`。其他无关配置会保留。

## 手动命令

在 skill 目录执行：

```bash
python scripts/configure.py audit
python scripts/configure.py apply --run-codex
python scripts/configure.py verify
python scripts/configure.py rollback --backup ~/.codex/backups/configure-sol-luna-subagents/<UTC-timestamp>
```

`apply --run-codex` 写入前会创建带 UTC 时间戳、按文件限定的备份，并在同一事务内
运行 `codex features list`；如果客户端校验失败会自动回滚。`rollback` 只恢复备份
manifest 列出的文件，并删除本次安装新建的文件。`verify` 只做最终静态检查，
不会启动模型任务。

See [English documentation](README.md) for the English version.
