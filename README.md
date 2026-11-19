# Token Meter

自托管的 OpenAI 兼容接口用量管理面板，内置官方 Codex CLI，支持 Responses API 和 Chat Completions。按周设置美元或 token 目标、支出上限和并发数，查看每笔结算、缓存命中率和每日用量。达标和请求错误可推送到 Bark。

## 使用 Compose 部署

在部署目录保存仓库根目录的 [compose.yaml](https://raw.githubusercontent.com/dongbo501/token-meter/main/compose.yaml)。使用容器管理面板时，可直接将文件内容粘贴到原来的 Compose 项目，保留已有的端口和数据卷设置。在命令行部署时执行以下命令。

```sh
docker compose up -d
docker compose exec panel cat /data/admin-password
```

Compose 从 GHCR 匿名拉取公开镜像 `ghcr.io/dongbo501/token-meter:latest`，Docker 会按主机架构选择 AMD64 或 ARM64。部署目录只需一个 Compose 文件。

镜像包含官方 `@openai/codex@0.169.1`，无需在宿主机安装 Node.js 或 Codex。新部署默认使用 Codex 模式；升级会保留已有部署的 Chat Completions 设置，可在面板切换。

打开 [本地管理面板](http://127.0.0.1:8080)，用户名为 `admin`，初始密码为上面 `cat` 命令的输出。更新时，在原项目中运行 `docker compose pull` 和 `docker compose up -d`。停用服务可运行 `docker compose down`。加上 `-v` 会同时删除配置、通知队列和账本。

## 使用 docker run 部署

在安装了 Docker 的主机上运行以下命令。

```sh
docker pull ghcr.io/dongbo501/token-meter:latest
docker run -d --name token-meter --restart unless-stopped \
  --stop-timeout 150 -p 127.0.0.1:8080:8080 \
  -v token-meter-data:/data ghcr.io/dongbo501/token-meter:latest
docker exec token-meter cat /data/admin-password
```

打开 [本地管理面板](http://127.0.0.1:8080)，用户名为 `admin`，密码为最后一条命令的输出。初次启动时计划处于暂停状态。

## 设置每周 600 美元目标

1. 在“运行设置”选择执行方式，填写供应商接口地址、API Key 和模型 ID。支持 Base URL、完整的 `/v1/responses` 或 `/v1/chat/completions` 地址。只允许编程客户端的接口选择“官方 Codex CLI”；允许脚本的接口可直接选择 Responses API 或 Chat Completions。后者需按模型选择 `max_tokens` 或 `max_completion_tokens`。
2. 根据供应商费率填写每百万 token 的普通输入、缓存输入和输出单价。选择“按美元金额”，每周目标填 `600`。最高支出由你决定，例如 `601`，给最后一笔请求留下预留空间。使用 token 模式时，分别填写目标用量和最高用量。
3. 设置并发数（1–32）和单次输出上限。保存后可点击“测试已保存接口”，只发送一笔请求，请求的输出上限为 32 token；完整输入也会计费，测试用量记入本周账本。在 Bark App 复制包含设备 Key 的推送地址，填入“Bark 通知”并保存。可点击“测试已保存地址”检查设备收件。
4. 回到“用量总览”点击“启动计划”。服务以设定并发持续执行，优先完成本周目标。达标后等待下周自动继续。手动暂停会停止派发新请求，并结算已发出的请求。

费率默认留空，必须按供应商实际价格填写后才能启动美元计划；程序不会猜测价格或自动启动 $600 的计划。Token 模式也提供金额估算；若将费率填 0，显示金额为 0，不代表供应商不收费。

### Codex 执行方式

容器通过官方 Codex CLI 生成独立的 Python 编程练习、示例和单元测试。每个任务使用独立临时目录和配置，不读取宿主机项目。CLI 使用随机临时令牌连接容器内部的本地转发器，真实供应商 Key 仅由转发器持有。转发器保留官方客户端发送的身份头，按完整请求重新预留预算，并设置 `max_output_tokens`。每个任务最多转发一次请求，关闭工具调用，生成的代码不执行。

Codex 模式会包含额外的客户端上下文，因此实际输入量高于页面中的编程题文本。完整请求无法装入剩余预算时会暂停；可以调整预算上限后重新启动。HTTP 错误和断流不会通过 CLI 隐式重发。

### 计量与缓存

- 每次请求以独立样本编号、随机数据和不同问题开头；Codex 模式的系统说明也从随机编号开始，以缩短公共前缀。实际缓存策略由供应商决定，无法保证零缓存。
- Responses / Codex 读取 `usage.input_tokens`、`output_tokens` 和 `input_tokens_details.cached_tokens`（包含推理 token 的输出总量）。Chat Completions 读取 `usage.prompt_tokens`、`completion_tokens` 和 `prompt_tokens_details.cached_tokens`，也支持 `prompt_cache_hit_tokens`。现金用量按普通输入、缓存输入和输出分别计算。没有缓存明细的请求按普通输入价估算，该部分不参与缓存命中率统计。模型输出内容不保留。
- 预留使用提示词字节数、消息封装余量和单次输出上限，并发请求共用同一份账本。接近上限时缩短请求、收紧输出。剩余空间仍不足时本周停止派发，并通过 Bark 提醒。若供应商忽略输出上限或实际用量超过预留，程序按实际用量结算并暂停；在途请求仍可能产生费用，预算不是供应商账单的硬封顶。面板的“达标”依据实际返回的 token 数和填写的费率；供应商账单及其最低消费认定以供应商为准。
- 账本记录此实例发起的请求。使用同一 API Key 的其他应用、供应商额外计价、隐藏输入、计费倍率或阶梯折扣可能造成账单差异。首次使用时可用小额目标和账单校准费率。
- 默认使用上海时区，每周一 00:00 开始新周期。跨周请求归入发起时的周期。重启沿用原账本与计划状态，停机错过的周不会补跑。

### Bark 通知与异常处理

- 自动推送仅包含本周目标完成和出错。启动、暂停、正常请求进度和跨周重置不会推送；“测试已保存地址”只在手动点击时发送测试通知。
- 每个周周期、模式和目标值首次达标时产生一条通知。每笔错误请求独立通知，包含请求编号和错误类别。推送使用 Bark 的 JSON `POST /push` 接口，支持官方服务器和带路径前缀的自建服务。
- 通知与请求结算在同一 SQLite 事务中入队。后台投递失败会退避重试，最长间隔一小时。重启后继续投递。网络确认中断时，设备可能收到重复推送。面板显示待投递数量及最近的投递结果。
- HTTP 429 根据 `Retry-After` 及指数退避自动重试。无效密钥、模型或参数会暂停计划。网络超时、用量缺失、服务器错误或请求中途重启时，保留预留额度并暂停。
- 打开“请求记录 → 仅待核对”，按供应商账单补录输入、缓存和输出 token。已确认未扣费的请求填三个 0。全部核对后可以重新启动。
- API Key 和 Bark 地址仅保存在服务端数据卷中，面板读取接口会隐藏这两项。修改接口地址时需重新提供该地址对应的 API Key。

## 日志与磁盘占用

- 不保存模型生成的正文，只保存请求状态、token 用量、估算金额和固定错误类别。
- 已结算和失败的请求明细默认保留 **14 天、最多 50,000 条**，每 5 分钟清理一次；清理间隔内可暂时超过条数限制。进行中和待核对的请求会保留，待核对状态会暂停计划。
- 预算使用独立的每日汇总，清理明细不会重置用量或使计划重复消耗。每日汇总每个活跃日期只占一行，长期保留。
- Bark 通知记录（包含未送达的旧通知）保留 30 天、最多 10,000 条；本周达标通知的去重记录额外保留。
- 定期执行 SQLite 增量空间回收和 WAL 截断。旧数据库在磁盘空间足够时自动迁移为空间可回收格式。数据卷可用空间低于 128 MiB 时暂停计划并报告错误；每 5 分钟检查，启动计划时也会检查。
- Compose 同时限制容器输出日志：单文件 10 MB，最多 3 个轮转文件。
- “请求记录”页显示保留规则和数据库当前占用。数据库没有承诺固定 MB 上限；其他应用写盘和未清理的备份不受本应用控制。

## 部署配置

接口、密钥、预算、并发数和 Bark 通知均在网页「运行设置」中填写并保存到数据卷。部署参数直接写在 `compose.yaml` 中，无需创建 `.env` 文件。

| Compose 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `ports` | `127.0.0.1:8080:8080` | 中间的端口号是宿主机的监听端口 |
| `environment.BILLING_TZ` | `Asia/Shanghai` | 与供应商账期一致的 IANA 时区 |
| `environment.WEEK_START` | `"0"` | 周起始日，0 为周一，6 为周日 |
| `environment.REQUEST_LOG_DAYS` | `"14"` | 已结算 / 失败明细保留天数 |
| `environment.REQUEST_LOG_MAX_ROWS` | `"50000"` | 已结算 / 失败明细最多保留条数 |

修改部署参数后运行 `docker compose up -d` 使配置生效。旧部署若自定义过端口或账期，更新前请将对应值填入 Compose 文件。管理员密码默认从数据卷内的 `/data/admin-password` 读取，首次启动时生成；原先通过 `ADMIN_PASSWORD` 指定过密码的部署，可在重新创建容器前将原密码保存到该文件以继续使用。

默认端口仅绑定本机，远程管理可使用 SSH 隧道或 HTTPS 反向代理。反向代理应保留原始 Host 以支持同源检查。API Key、Bark 设备 Key 和账本存在数据卷中，拥有数据卷读取权限的管理员可以读取这些凭据。备份前请先停容器，并保护备份文件。

## 开发与测试

需要 Python 3.11+ 和 IANA 时区数据。Python 服务仅使用标准库，前端静态资源随镜像提供。Codex 模式另需官方 CLI，可通过 `npm install -g @openai/codex@0.169.1` 安装；发布的镜像已内置。

```sh
python3 -m unittest discover -s tests -v
python3 server.py
```

本地启动默认绑定 127.0.0.1，密码文件位于 `data/admin-password`。测试使用本地模拟的模型接口与 Bark 服务。推送 main 后，GitHub Actions 运行账本、协议、真实 CLI 对本地模拟服务的集成测试，再发布 AMD64 / ARM64 的 `latest` 镜像。需要固定版本时，可使用镜像摘要或已发布的版本标签。
