# 模型自选 · 每日限额（astrbot_plugin_model_quota，金额制）

让用户用指令自选对话的 AI 大模型，并按**金额**做每日限额：
配置用**美元**，展示给用户看的是**人民币**（汇率可配）。

## 安装

1. 把 `astrbot_plugin_model_quota` 文件夹放入 `AstrBot/data/plugins/`；
2. 重启 AstrBot（或在 WebUI 插件页重载）；
3. 在 WebUI「插件」中找到本插件，先配置各模型的**单次调用单价（美元）**，再设限额。

计费方式：按次计费。一次唤起 AI 的对话 = 1 次调用 = 扣一次单价。
单价可以手填，也可以直接用内置的 **OpenCode Go 价目预设**自动换算
（每月额度 ÷ 每月预估请求数），无需逐个填。单价为 0 的模型视为免费。

### 内置单价预设（`pricing_preset: opencode_go`，默认开启）

按 [OpenCode Go 官方价目](https://opencode.ai/docs/zh-cn/go/) 换算成「单次对话」成本，
覆盖 Kimi K3、GPT 5.6 Luna、GLM-5.x、Qwen3.x、DeepSeek V4 系列、Grok 4.x、
MiniMax、MiMo、Hy、LongCat、Muse Spark、Space Bunny Free 等全部在售模型。
按模型名自动匹配（大小写/空格/`opencode-go/` 前缀都能认），例如：

| 模型 | 每月额度 | 单次单价（低谷） |
|---|---|---|
| Kimi K3 | $15 | $0.0306 |
| GLM-5.2 / GLM-5.1 | $60 | $0.0140 |
| Qwen3.8 Max | $15 | $0.0185 |
| DeepSeek V4 Pro | $15 | $0.00288 |
| DeepSeek V4.1 Flash | $60 | $0.00046 |
| GPT 5.6 Luna | $15 | $0.0015 |
| MiMo-V2.6-Flash | $60 | $0.0004 |
| Space Bunny Free | 限时免费 | $0（免费） |

### 峰谷定价（`peak_pricing_enabled`，默认开启）

官方仅 DeepSeek 系列有峰谷，**峰时价格为低谷的 2×**：

- 峰时：周一至周五 `01:00-04:00`、`06:00-10:00`（UTC，即北京时间 `09:00-12:00`、`14:00-18:00`）
- 低谷：其余时段 + 整个周末

插件在每次调用前按当时的时段取价并计费，卡片与列表会显示
`谷时（峰时 2×）` / `峰时 2×（当前）`。想按北京时间填区间，
把 `peak_timezone_offset` 设成 `8`、`peak_windows` 写成 `["09:00-12:00","14:00-18:00"]` 即可。
自建模型要参与峰谷，写进 `peak_models`；也可用 `model_peak_prices_usd` 直接指定峰时单价。

## 用户指令

| 指令 | 说明 |
|---|---|
| `/model`（别名 `/模型`） | 开放自选的模型列表（带单价）、当前模型（`*`）、自己今日剩余额度 |
| `/model list` | 同上 |
| `/model use <序号\|ID>` | 切换当前对话的模型，例：`/model use 2`（序号按开放列表数） |
| `/model 2`、`/model <ID>` | 快捷切换写法 |
| `/model me` | 只看自己今日剩余额度（图片卡） |

**群聊规则**：群里**只有管理员能切换**模型（AstrBot 管理员判定，`event.is_admin()`）；
普通群成员可查看列表、可查自己的剩余额度。私聊谁都可以切换。

切换按当前会话生效（私聊按人，群聊按整群）；消费按人统计，群里各人分开算。

## `/quota` 显示什么样（额度图片卡）

`/quota`（别名 `/额度`、`/限额`）默认发一张深色图片卡：

![剩余额度卡片示例](assets/quota_card.png)

卡片内容：标题「剩余额度」+ 用户名、副标题、更新/重置时间、
逐行「正常模型名 + 进度条 + 已用百分比」，有总池的模型在同一块内多一条
浅绿色总池进度条，总池花费与个人花费写在同一行，
最后再有一行「个人总额」；
用完的行标红并带 `※ 已用完`，卡片底部统一提示重置规则。

需要 Pillow + 中文字体（Windows 开箱即用；Linux 需 Noto/wqy）。
没有时自动回退下面的文本版（`quota_render` 可强制 `image`/`text`/`auto`）：

```text
📊 我今日剩余额度（1$≈¥7.20）：
- Kimi K3 ██ 14% 已花 ¥0.50/¥3.60（35 次）剩 ¥3.10
- GPT 5.6 Luna ██████ 30% 已花 ¥2.16/¥7.20（6 次）剩 ¥5.04 · 总池已花 ¥46.80/¥144.00（剩 ¥97.20）
- Space Bunny Free：免费·不限（已用 12 次）
💰 个人总额 ████ 18% 已花 ¥2.66/¥14.40 剩 ¥11.74
⏰ 每日 00:00 重置（14 小时 45 分后重置，明天 00:00）
```

即：逐模型显示**正常模型名 + 已用次数 + 已花人民币 + 剩余/上限**，
有总池的模型总池花费并在同一行；
最后再有一行**个人今日总花费与剩余额度**。免费模型显示"免费·不限"。
文本版末尾会附 `⏰ 每日 00:00 重置（X 小时后重置，明天 00:00）`。

## 管理员指令（`/quota` 子命令，仅管理员）

| 指令 | 说明 |
|---|---|
| `/quota all` | 今日全量：各模型单价、总池已花/上限、用过次数、每人上限、个人总额度 |
| `/quota usage` | 今日各模型总花费/总池/次数/人数 |
| `/quota usage <用户ID>` | 查某用户今日明细（次数/花费/总花，支持 sender_id 子串匹配） |
| `/quota reset` | 清空今日全用户计数（个人 + 总池） |
| `/quota reset <用户ID>` | 只重置某用户（总池自动重算） |

## 插件配置（WebUI 可视化编辑，单位美元）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `selectable_models` | `[]` | **允许自选的模型白名单（提供商 ID 列表）**，例 `["vip", "default"]`；留空=全部可用；`/model` 列表与切换只认这里 |
| `model_display_names` | `{}` | **模型显示名映射**，例 `{"default": "Kimi K3", "vip": "GPT 5.6 Luna"}`；列表、卡片、额度、提示全部只显示真实模型名，ID 藏起来；切换用序号或名称（名称带空格可直接写，如 `/model use GPT 5.6 Luna`） |
| `pricing_preset` | opencode_go | 内置单价预设：`opencode_go` 自动按官方价目换算；`off` 只用下面手填的 |
| `default_call_price_usd` | 0 | 单次调用默认单价；0=免费不限 |
| `model_call_prices_usd` | `{}` | 按模型覆盖单价（低谷价），优先级高于预设；例 `{"vip": 0.05}` |
| `peak_pricing_enabled` | true | 启用峰谷定价 |
| `peak_multiplier` | 2.0 | 峰时价格倍数（官方 DeepSeek 为 2） |
| `peak_windows` | `["01:00-04:00","06:00-10:00"]` | 峰时区间，可多个、支持跨天 |
| `peak_timezone_offset` | 0 | 峰时区偏移（官方按 UTC，故默认 0；填 8 可用北京时间） |
| `peak_weekdays_only` | true | 周末全天低谷 |
| `peak_models` | `[]` | 额外参与峰谷的模型（预设里的 DeepSeek 已自动包含） |
| `model_peak_prices_usd` | `{}` | 直接指定峰时单价，优先于「低谷价×倍数」 |
| `default_user_model_quota_usd` | 1.0 | 每人每天每模型默认限额 |
| `model_user_quotas_usd` | `{}` | 按模型覆盖个人限额，例 `{"vip": 5.0}` |
| `default_user_total_quota_usd` | 2.0 | **每人每天全部模型消费总额度** |
| `default_global_quota_usd` | 0 | 全用户总池默认，0=不限 |
| `model_global_quotas_usd` | `{}` | 按模型覆盖总池，例 `{"vip": 100.0}` |
| `usd_to_cny_rate` | 7.2 | 汇率，只影响展示 |
| `quota_render` | auto | 个人额度展示：`auto`/`image`（图片卡）/`text` |
| `admin_exempt` | true | 管理员免限额且不计数（不占总池） |

`model_*` 的 key 是 WebUI「模型服务」中的**提供商 ID**；
缺省走 default；`<=0` 视为不限。

## 行为说明 / FAQ

- 指令消息（`/model` 等）不会触发 AI，不扣费。
- 钱够 precision：允许"刚好够一次"的扣费（`已花+单价 > 上限` 才拦）。
- 切换到已不够钱的模型：允许切换，但警告"暂时无法用它对话"。
- 跨天按服务器本地日期自动清零；用量存插件 KV（`daily_usage_v2`）。
- provider 改名/删除后旧记录残留但不再命中；配置里写错的 ID 会在日志 warning。
