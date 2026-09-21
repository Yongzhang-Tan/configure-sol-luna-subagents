[English](README.md) | [简体中文](README.zh-CN.md)

# 配置 Astra → Luna 子代理

本仓库提供一个仅显式调用的 Codex skill 和一个小型、可移植的全局配置器。
默认配置为：

- T1/main：`gpt-6-astra`，medium 推理强度。
- T2/default、mapper、implementation worker：`gpt-5.6-luna`，max。
- T3/routine state checker：`gpt-5.6-luna`，max。
- `implementation_worker` 是唯一可写角色；mapper 和 routine checker 只读。

模型 registry 只选择 provider/model 和可用推理强度；sandbox、指令和权限仍由各
agent TOML 独立定义。安装器保留无关配置、角色、provider 定义、hooks、MCP 服务
和项目注册信息，不扫描或修改项目目录。

模型和推理强度是否可用取决于 Codex 客户端和账户。应用前请检查客户端实际支持的
模型与 effort；如果默认 profile 不可用，应先询问用户再选择替代项。静态配置解析
成功不等于付费 live model 测试，也不证明账户访问权限或计费可用。

## 安装 skill

现有可分享 URL 和 skill 入口保持不变：

`https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents`

一句话入口：

`$skill-installer 从 <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents> 安装，然后使用 $configure-sol-luna-subagents。`

也可以使用标准两步流程：

1. `$skill-installer 安装 <https://github.com/Yongzhang-Tan/configure-sol-luna-subagents/tree/main/skills/configure-sol-luna-subagents>`
2. `$configure-sol-luna-subagents`

skill 仍然是显式调用；安装会把 skill 文件放入客户端的 skill 位置，但不会应用
这套全局配置。

## 安全工作流

在 skill 目录运行脚本。可以通过环境变量或 `--codex-home` 指定 `CODEX_HOME`；
测试和示例应始终使用临时目录。

```bash
python scripts/configure.py              # preview；不写入
python scripts/configure.py audit        # 只读审计
python scripts/configure.py preview      # 只读解析后的计划
python scripts/configure.py apply --yes  # 明确确认后写入
python scripts/configure.py verify
```

显示 preview 后，应在写入前立即就这份精确计划取得用户确认。客户端有一到三个问题的
输入 UI 时，可用它询问偏好；否则使用普通对话。`--yes` 只能在用户确认后作为确认标志。
不带 `--yes` 时，`apply`/`sync` 会在交互终端询问确认；非交互进程会安全失败。
确认前只显示精确的 `CODEX_HOME`、解析后的 model/effort/provider、sandbox 和目标
文件名，不显示原始配置内容。

每次 apply 或 sync 都会创建按文件限定的 UTC 备份。回滚必须显式指定：

```bash
python scripts/configure.py rollback --backup ~/.codex/backups/configure-sol-luna-subagents/<UTC-timestamp>
```

回滚会恢复记录的文件范围，可能覆盖这些文件在安装后的修改；使用前应检查备份路径。

## Registry 与同步

安装会创建带 managed block 的 `model-tiers.toml` 和 `agent-tiers.toml`。已有 registry
不会被盲目覆盖；未管理的冲突、无效 TOML、缺失 marker 或角色文件歧义都会在写入前停止。
无关 tier 和 role 会保留。

可以编辑 bundle 中的 tier 文件。修改 managed tier 的 model 或 effort 后，先检查再同步：

```bash
python scripts/configure.py tiers list
python scripts/configure.py tiers check
python scripts/configure.py sync --yes
python scripts/configure.py verify
```

`sync` 从 registry 读取 model/effort 并物化到全局 config 和 managed agent，不重写 registry。
实际使用的 tier provider 必须与主线程的有效 provider 一致；既有 provider 定义和凭据
不会被修改。

## Profile 与兼容性

保留可选的旧版 profile：

```bash
python scripts/configure.py apply --profile sol-luna --yes
```

它继续使用 `gpt-5.6-sol`/max 和历史文件名
`sol_luna_code_mapper`/`sol_luna_implementation_worker`。这些是兼容名称，不是当前架构。
如果已有安装包含这些 managed 文件，默认 Astra profile 会原位升级它们并添加 routine
checker，不会创建第二个 writer。如果 canonical 与兼容文件冲突，安装器会停止并要求
明确处理。

## 范围与可选定制

安装器只写全局 Codex home：原生 config、全局 active `AGENTS.md`/`AGENTS.override.md`
区块、portable tier registry，以及 `~/.codex/agents/*.toml` 下的个人 managed agent。
Codex 也支持项目级 `.codex/agents/*.toml`；项目绑定或模板属于用户主动发起的可选任务，
本包不会自动发现或修改。

个人和项目 agent 位置，以及每个角色的 model、effort、sandbox 字段遵循[官方子代理配置文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

训练/远程作业监控节奏和 TensorBoard 指引不会作为全局策略自动安装。项目需要时应显式
加入项目指令。

本包不承诺普遍模型支持或固定额度节省；实际开销取决于任务路由、调用次数、账户限制和定价。
