# Token Meter

自托管的 OpenAI 兼容接口用量管理面板。按周设置美元或 token 目标、支出上限和并发数，查看每笔结算、缓存命中率和每日用量。达标和请求错误可推送到 Bark。

## 使用 GitHub 镜像

需要 Docker。镜像支持 Linux AMD64 和 ARM64。私有镜像需要先用有包读取权限的 GitHub 账号登录 GHCR。已登录 GitHub CLI 且令牌具有 `read:packages` 或 `write:packages` 权限时，可以直接执行以下命令。

```sh
gh auth token | docker login ghcr.io -u dongbo501 --password-stdin
docker run -d --name token-meter --restart unless-stopped \
  --stop-timeout 150 -p 127.0.0.1:8080:8080 \
  -v token-meter-data:/data ghcr.io/dongbo501/token-meter:latest
docker exec token-meter cat /data/admin-password
```

打开 [本地管理面板](http://127.0.0.1:8080)，用户名为 `admin`，密码为最后一条命令的输出。初次启动时计划处于暂停状态。

### 使用仓库中的 Compose

```sh
gh repo clone dongbo501/token-meter
cd token-meter
docker compose up -d --build
docker compose exec panel cat /data/admin-password
```

更新到已发布的镜像时，运行 `docker compose pull` 和 `docker compose up -d --no-build`。停用服务可运行 `docker compose down`。加上 `-v` 会同时删除配置、通知队列和账本。

## 设置每周 600 美元目标

1. 在“运行设置”填写供应商接口地址、API Key 和实际可用的模型 ID。支持 Base URL 或完整的 Chat Completions 地址。按模型选择 `max_tokens` 或 `max_completion_tokens`。
2. 根据供应商费率填写每百万 token 的普通输入、缓存输入和输出单价。选择“按美元金额”，每周目标填 `600`。最高支出由你决定，例如 `601`，给最后一笔请求留下预留空间。使用 token 模式时，分别填写目标用量和最高用量。
3. 设置并发数和单次输出上限。在 Bark App 复制包含设备 Key 的推送地址，填入“Bark 通知”并保存。可点击“测试已保存地址”检查设备收件。
4. 回到“用量总览”点击“启动计划”。服务以设定并发持续执行，优先完成本周目标。达标后等待下周自动继续。手动暂停会停止派发新请求，并结算已发出的请求。

### 计量与缓存

- 每次请求以独立样本编号、随机生成的时间序列和分析问题开头，缩短公共前缀。实际缓存策略由供应商决定。
- 读取 Chat Completions 的 `usage.prompt_tokens`、`completion_tokens` 和 `prompt_tokens_details.cached_tokens`，也支持 `prompt_cache_hit_tokens`。现金用量按普通输入、缓存输入和输出分别计算。没有缓存明细的请求按普通输入价估算，该部分不参与缓存命中率统计。模型输出内容不保留。
- 预留使用提示词字节数、消息封装余量和单次输出上限，并发请求共用同一份账本。接近上限时缩短请求、收紧输出。剩余空间仍不足时本周停止派发，并通过 Bark 提醒。面板的“达标”依据实际返回的 token 数和填写的费率；供应商账单及其最低消费认定以供应商为准。
- 账本记录此实例发起的请求。使用同一 API Key 的其他应用、供应商额外计价、隐藏输入、计费倍率或阶梯折扣可能造成账单差异。首次使用时可用小额目标和账单校准费率。
- 默认使用上海时区，每周一 00:00 开始新周期。跨周请求归入发起时的周期。重启沿用原账本与计划状态，停机错过的周不会补跑。

### Bark 通知与异常处理

- 每个周周期、模式和目标值首次达标时产生一条通知。每笔错误请求独立通知，包含请求编号和错误类别。推送使用 Bark 的 JSON `POST /push` 接口，支持官方服务器和带路径前缀的自建服务。
- 通知与请求结算在同一 SQLite 事务中入队。后台投递失败会退避重试，最长间隔一小时。重启后继续投递。网络确认中断时，设备可能收到重复推送。面板显示待投递数量及最近的投递结果。
- HTTP 429 根据 `Retry-After` 及指数退避自动重试。无效密钥、模型或参数会暂停计划。网络超时、用量缺失、服务器错误或请求中途重启时，保留预留额度并暂停。
- 打开“请求记录 → 仅待核对”，按供应商账单补录输入、缓存和输出 token。已确认未扣费的请求填三个 0。全部核对后可以重新启动。
- API Key 和 Bark 地址仅保存在服务端数据卷中，面板读取接口会隐藏这两项。修改接口地址时需重新提供该地址对应的 API Key。

## 部署配置

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PANEL_PORT` | `8080` | Compose 的宿主机端口 |
| `ADMIN_PASSWORD` | 首次启动随机生成 | 设置后使用该值登录，否则读取数据卷内的 `admin-password` |
| `BILLING_TZ` | `Asia/Shanghai` | 与供应商账期一致的 IANA 时区 |
| `WEEK_START` | `0` | 周起始日，0 为周一，6 为周日 |

Compose 变量可写在仓库根目录的本地 `.env`。建议在首次运行前设定账期。默认端口仅绑定本机，远程管理可使用 SSH 隧道或 HTTPS 反向代理。反向代理应保留原始 Host 以支持同源检查。API Key、Bark 设备 Key 和账本存在数据卷中，拥有数据卷读取权限的管理员可以读取这些凭据。备份前请先停容器，并保护备份文件。

## 开发与测试

需要 Python 3.11+ 和 IANA 时区数据。应用仅使用 Python 标准库，前端静态资源随镜像提供。

```sh
python3 -m unittest discover -s tests -v
python3 server.py
```

本地启动默认绑定 127.0.0.1，密码文件位于 `data/admin-password`。测试使用本地模拟的模型接口与 Bark 服务。推送 main 后，GitHub Actions 运行测试并发布 `latest` 与 `sha-<短提交哈希>` 镜像标签。
