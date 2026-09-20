# Sub2API Account Optimizer

[![CI](https://github.com/hzh6767/sub2api-account-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/hzh6767/sub2api-account-optimizer/actions/workflows/ci.yml)

Sub2API Account Optimizer 是一个面向 Sub2API 的 OpenAI 账号健康探测与调度优化器。
它优先分析真实请求的首字延迟、错误率和超时率，仅在样本不足或账号异常时执行低成本的
指定账号探测，再通过防抖、冷却和所有权规则平滑调整 `priority`、`load_factor` 与
`schedulable`。

项目定位是“可审计、可回滚、默认只读”的运维组件：先 dry-run 观察结果，再按需开启主动探测
和调度写入，不要求修改账号凭据或把账号 ID 写死在配置中。

> [!IMPORTANT]
> 默认配置不会修改账号，也不会主动发起上游请求。请先阅读兼容性说明并完成 dry-run。
> 本项目当前兼容 Sub2API `0.2.5`，补丁用于增加安全的 `mode=optimizer` 最小输出探测。


## 能做什么

- 动态读取一个或多个 OpenAI 分组，不写死账号 ID。
- 分组、分模型统计最近 24 小时真实流量的 P50/P90 TTFT、失败率和超时率。
- 真实流量充足时跳过主动测速；低样本、新账号、异常账号按自适应周期探测。
- 通过 Sub2API 指定账号测试端点命中目标账号，首个有效文本 Token 到达后关闭流。
- 真实流量与主动探测按 `70% / 30%` 合成评分，不混合不同模型的原始延迟。
- 将排名缓慢映射到 `priority=1..4`、`load_factor=5..15`，并受真实并发上限约束。
- 单次故障不下线账号，连续故障达到时间跨度后才处理；429 只记为临时限流。
- 只恢复由优化器明确停用的账号，管理员手动停用的账号不会被自动恢复。
- 提供单实例锁、轮次超时、健康检查、JSON 审计日志、状态查看和回滚。

详细状态机、评分规则和回滚边界见 [DESIGN.md](DESIGN.md)。

## 兼容性

仓库中的 [`patches/sub2api-0.2.5-optimizer.patch`](patches/sub2api-0.2.5-optimizer.patch)
基于以下版本生成：

- 上游项目：[Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)
- 版本：`0.2.5`（`backend/cmd/server/VERSION`）
- 补丁范围：管理员账号测试 SSE 的 `mode=optimizer` 兼容层


补丁为 `/api/v1/admin/accounts/:id/test` 增加 `mode: optimizer` 安全握手和最小输出逻辑。
不要把该补丁直接用于其他版本；升级 Sub2API 后应重新审查并移植补丁。

## 安全默认值

三个开关默认全部为 `false`：

```dotenv
OPTIMIZER_APPLY_ENABLED=false
OPTIMIZER_ACTIVE_PROBES_ENABLED=false
OPTIMIZER_TARGETED_TEST_SAFE=false
```

- `APPLY_ENABLED=false`：禁止更改调度字段和账号状态。
- `ACTIVE_PROBES_ENABLED=false`：只分析真实流量，不产生主动测试费用。
- `TARGETED_TEST_SAFE=false`：未确认运行端点支持安全握手时，禁止指定账号探测。

管理员 API Key 只通过只读 Docker secret 挂载。日志不会保存 Key、Cookie、账号凭据或
完整请求内容。日志会包含账号 ID、账号名称和运行指标，应按运维数据限制访问。

## 快速开始：只读 Dry-run

以下示例假设 Sub2API 位于 `/opt/sub2api`，本项目位于
`/opt/sub2api/account-optimizer`。路径和网络名均可通过环境变量调整。

1. 确认 Sub2API 版本：

   ```sh
   cd /opt/sub2api
   git rev-parse HEAD
   # 预期版本：0.2.5

   ```

2. 检查并应用补丁：

   ```sh
   git apply --check account-optimizer/patches/sub2api-0.2.5-optimizer.patch
   git apply account-optimizer/patches/sub2api-0.2.5-optimizer.patch
   docker build -t sub2api:0.2.5-account-optimizer .

   ```

3. 构建优化器，不启动正式服务：

   ```sh
   docker compose --env-file /opt/sub2api/.env \
     -f /opt/sub2api/account-optimizer/compose.yml build optimizer
   ```

4. 运行只读分析：

   ```sh
   docker compose --env-file /opt/sub2api/.env \
     -f /opt/sub2api/account-optimizer/compose.yml \
     run --rm optimizer --dry-run
   ```

此时不会主动测速，也不会修改任何账号。报告写入：

- `logs/latest.json`
- `logs/history.jsonl`
- `backups/deployment-baseline.json`

## 验证指定账号探测

先部署已打补丁的 Sub2API 镜像，并在 Sub2API 中创建一个仅供优化器使用的管理员 API
Key。将 Key 写入 `secrets/admin-api-key`，文件权限限制为容器用户可读：

```sh
chown 10001:10001 /opt/sub2api/account-optimizer/secrets/admin-api-key
chmod 400 /opt/sub2api/account-optimizer/secrets/admin-api-key
```

在隔离或低峰环境中，以 `APPLY_ENABLED=false`、另外两个开关为 `true` 运行一轮 dry-run。
确认报告中的探测命中指定 `account_id`、返回 `mode=optimizer`、回显请求模型，并在首个
文本 Token 后结束。此步骤会产生少量上游请求费用，但不会修改调度字段。

## 正式启用

只有在补丁、探测握手、数据库查询、回滚基线和 dry-run 排名全部验证后，才把
`activation.env.example` 另存为 `activation.env` 并开启三个开关：

```dotenv
OPTIMIZER_APPLY_ENABLED=true
OPTIMIZER_ACTIVE_PROBES_ENABLED=true
OPTIMIZER_TARGETED_TEST_SAFE=true
```

Sub2API 的 `openai_advanced_scheduler_enabled` 也必须通过官方管理员接口启用。优化器在
正式写入前会检查该开关，关闭时拒绝修改账号。

启动每 10 分钟一次的轻量循环：

```sh
docker compose --env-file /opt/sub2api/.env \
  --env-file /opt/sub2api/account-optimizer/activation.env \
  -f /opt/sub2api/account-optimizer/compose.yml \
  --profile optimizer up -d optimizer
```

“每 10 分钟运行”不等于每 10 分钟测试所有账号。真实样本充足的正常账号会跳过主动
测速；其他账号受 15 分钟到 2 小时的自适应间隔限制。

## 常用命令

| 操作 | 命令参数 | 说明 |
| --- | --- | --- |
| 只读预览 | `--dry-run` | 读取遥测并计算预计变化，不写账号 |
| 执行一轮 | `--once` | 按当前启用开关执行一次 |
| 查看状态 | `--status` | 输出 `logs/latest.json` |
| 守护运行 | `--daemon` | 按配置间隔持续运行 |
| 账号字段回滚 | `--rollback` | 恢复 `activation-baseline.json` |

例如：

```sh
docker compose --env-file /opt/sub2api/.env \
  --env-file /opt/sub2api/account-optimizer/activation.env \
  -f /opt/sub2api/account-optimizer/compose.yml \
  run --rm optimizer --status
```

停止服务：

```sh
docker compose --env-file /opt/sub2api/.env \
  --env-file /opt/sub2api/account-optimizer/activation.env \
  -f /opt/sub2api/account-optimizer/compose.yml \
  --profile optimizer stop optimizer
```

## 主要配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `OPTIMIZER_GROUP_IDS` | `7,59` | 逗号分隔的 OpenAI 分组 ID |
| `OPTIMIZER_LOOP_INTERVAL_SECONDS` | `600` | 轻量调度循环间隔 |
| `OPTIMIZER_ROUND_TIMEOUT_SECONDS` | `480` | 单轮最长执行时间 |
| `OPTIMIZER_PROBE_MODEL_PREFERENCE` | 示例模型列表 | 普通文本探测模型允许列表 |
| `SUB2API_DOCKER_NETWORK` | `sub2api_sub2api-network` | 外部 Docker 网络 |
| `SUB2API_MODEL_PRICING_SOURCE` | `../data/model_pricing.json` | Sub2API 模型价格文件 |
| `SUB2API_ADMIN_BASE_URL` | `http://sub2api:8080` | 容器内管理员 API 地址 |
| `OPTIMIZER_ADMIN_API_KEY_SOURCE` | 空占位文件 | 宿主机管理员 API Key 文件 |

数据库和 Redis 连接变量沿用常见的 `POSTGRES_*`、`REDIS_*` 配置。`load_factor` 永远不会
替代或修改账号的 `concurrency`。

## 费用估算

报告中的 `estimated_daily_probe_usage` 会列出预计每日主动请求次数、输入/输出 Token 和
基于当前模型价格目录计算的费用。实际请求数通常远低于“账号数 x 144”，因为真实样本
充足时会跳过探测，稳定账号还会把主动测试间隔延长到 2 小时。

## 回滚

仅恢复优化器拥有的账号调度字段：

```sh
docker compose --env-file /opt/sub2api/.env \
  --env-file /opt/sub2api/account-optimizer/activation.env \
  -f /opt/sub2api/account-optimizer/compose.yml \
  run --rm optimizer --rollback
```

完整部署回滚脚本还会停用优化器、关闭高级调度器、恢复原 Sub2API 镜像并删除专用管理员
Key。它要求显式传入不可变备份目录，防止误用旧备份：

```sh
ACTIVATION_BACKUP=/opt/sub2api/backups/account-optimizer-activation-YYYYMMDDTHHMMSSZ \
  /opt/sub2api/account-optimizer/scripts/full-rollback.sh
```

运行前请审查脚本，并确保备份目录至少包含 `sub2api-image.txt`，格式为：

```text
<original-image-id> <original-image-reference>
```

## 开发与测试

```sh
python -m pip install -r requirements.txt ruff
ruff check optimizer tests
python -m unittest discover -s tests -v
```

当前测试覆盖主动探测握手、真实流量跳过、失败与恢复阈值、管理员手动停用保护、排名
防抖、并发上限、超时后状态持久化、日志脱敏和回滚编码。

## 已知限制

- 历史使用记录没有工具调用标记，因此无法百分之百排除历史纯工具调用请求。
- 主动探测依赖本仓库补丁提供的 `mode: optimizer` 握手。
- 高级调度器权重按 Sub2API `0.2.5` 的运行配置和接口能力处理，修改前应先完成备份与验证。
- 本项目不会替你判断某个第三方上游的服务条款或账号共享限制。

## English Summary

This project is a safety-first OpenAI account health and slow baseline scheduler optimizer for
Sub2API. It combines 24-hour real-traffic TTFT/error telemetry with low-token targeted probes,
keeps groups and models isolated, caps load factor by real concurrency, and applies changes only
after stability, cooldown, and ownership checks. All mutation and active-probe gates are disabled
by default. The included Sub2API patch is ported and reviewed against version `0.2.5`.

## License

本项目使用 [GNU Lesser General Public License v3.0](LICENSE)。补丁包含对 Sub2API
LGPL-3.0 代码的修改，来源与提交信息见 [NOTICE](NOTICE)。
