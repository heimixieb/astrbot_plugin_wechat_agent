# astrbot_plugin_wechat_agent · 微信个人号适配器

通过 [agent-wechat](https://github.com/thisnick/agent-wechat) 守护进程，把**个人微信号**接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。插件直接装在 AstrBot 里，不需要额外的中转服务。

**亮点**：群聊做得像真人（偶尔引用、撤回了就不回、先发图再 @ 也看得懂），能**接微信语音电话**（豆包端到端实时语音，可打断），群里一句 `/总结群聊`；**在 AstrBot 日志里直接扫码登录**，不用敲命令。每个功能都能在 WebUI 里单独开关。

> ⚠️ **使用前请读**：agent-wechat 是非官方微信客户端，使用它违反《微信软件许可及服务协议》，账号有被限制或封禁的风险。请只登录你自己有权使用的、可以承受封号风险的小号，不要用于群发、营销、批量控号等用途。本插件只是对接 agent-wechat 的 HTTP 接口，不包含任何破解或逆向微信协议的代码。

## 5 分钟上手

**① 装守护进程**（Linux x64 / arm64，在跑 AstrBot 的那台机器上执行）：

```bash
curl -fsSLO https://raw.githubusercontent.com/heimixieb/astrbot_plugin_wechat_agent/main/deploy/install_daemon.sh
bash install_daemon.sh
```

国内服务器连不上 GitHub 时，两处都加镜像前缀：

```bash
curl -fsSLO https://gh-proxy.com/https://raw.githubusercontent.com/heimixieb/astrbot_plugin_wechat_agent/main/deploy/install_daemon.sh
GH_PROXY=https://gh-proxy.com/ bash install_daemon.sh
```

脚本会下载 `wx` 并核对 sha256、生成 token、配成开机自启，最后打印 AstrBot 里要填的内容。macOS / Windows 请到 [Releases](https://github.com/thisnick/agent-wechat/releases) 手动下载，按 [agent-wechat 文档](https://github.com/thisnick/agent-wechat) 启动。

**② 装插件**：AstrBot WebUI → 插件市场搜「微信个人号适配器」，或从链接安装 `https://github.com/heimixieb/astrbot_plugin_wechat_agent`。

**③ 加机器人**：WebUI → 机器人 → 新增 → 选「微信 (agent-wechat)」，填：
- 守护进程地址：`http://127.0.0.1:6174`
- Token 文件：脚本最后打印的路径（AstrBot 在 Docker 里见下文）

**④ 扫码**：保存启用后打开 WebUI「控制台」日志，会出现一个二维码（同时存成 `data/plugin_data/astrbot_plugin_wechat_agent/<机器人id>/login_qr.png`）。用手机微信扫码，并在手机上点「登录」。日志出现 `已连接守护进程，账号 wxid_…` 就通了。

**⑤ 放行会话**：AstrBot 开了白名单时只回名单里的会话。给机器人发条消息，AstrBot 日志里会出现 `昵称/wxid_xxx: 内容`，那个 `wxid_xxx` 就是对方的微信号（也可以发 `/sid` 查）。填进白名单，格式如 `wechat:FriendMessage:wxid_xxx`，群可以直接写 `xxx@chatroom`。

### AstrBot 在 Docker 里

守护进程只听本机 `127.0.0.1`，所以 AstrBot 容器要开 **host 网络**，并把 token 文件只读挂进去，然后 Token 文件填 `/run/secrets/wechat-api-token`：

```yaml
services:
  astrbot:
    network_mode: host
    volumes:
      - ./data:/AstrBot/data
      - ~/agent-wechat/secrets/api-token:/run/secrets/wechat-api-token:ro
```

完整示例见 [deploy/docker-compose.example.yml](deploy/docker-compose.example.yml)。改完要重建容器：`docker compose up -d --force-recreate astrbot`。

## 功能开关

全部在 WebUI「机器人 → 微信 (agent-wechat)」里，每项都有说明。下面这些**默认都开着**，不需要的自己关掉：

| 开关 | 做什么 |
|---|---|
| 自动扫码登录 `auto_login` | 没登录时先试免扫码，不行就把二维码打在日志里 |
| 群聊偶尔引用对方 `quote_reply` | 照搬 [MaiBot](https://github.com/Mai-with-u/MaiBot) 的概率：她回的那条后面插进来的消息越多，越倾向引用原消息（约 10%~31%），私聊从不引用 |
| 撤回了就不回 `skip_recalled` | 对方撤回了消息、机器人还没回的话，这条回复就不发了 |
| 先发图再 @ 时带上图 `attach_recent_image` | 微信图文不能同条发：群里有人发图后 3 分钟内（`recent_image_seconds`）又 @ 机器人，把他刚发的图带进这条，看图、改图插件都能拿到 |
| 分享卡片转文字 `parse_share_card` | 文章/视频/音乐/视频号分享转成「标题 + 简介 + 网址」（网址单独一行，链接解析插件能认）；聊天记录合并转发转成「谁：说了什么」 |
| 长语音自动切段 `split_long_voice` | 微信语音最长 60 秒，超过 55 秒的按换气停顿切成几条发 |
| 看视频 `watch_video` | 私聊视频直接交给机器人；群里只在有人 @ 机器人并引用视频、发完视频 3 分钟内 @ 机器人、或机器人刚在聊时引用视频，才下载来看（看视频要抽帧识别，费钱），其余群视频只给一个「[视频]」。需要会理解视频的插件 |
| 大图附原图文件 `send_image_original` | agent-wechat 发图会把每张图压到 64KB 以内，大图会糊；长边 ≥1600 的静态大图（比如 AI 画的图）发完后再发一份原图文件 |
| 下载图片和语音 `download_media` | 收到的图片、语音下载下来交给 AstrBot（语音自动转 wav，方便语音识别） |
| 防风控节流 | 所有发送串行，条间随机 2~4 秒（`send_min_interval` / `send_max_interval`）、每分钟最多 12 条（`send_max_per_minute`，0 = 不限），等守护进程真正发出再发下一条 |
| 旧消息不回 `max_message_age_seconds` | 机器人停了很久再恢复时，超过 5 分钟的旧消息不回复（0 = 不限） |
| 总结群聊 `summary_enable` | 群里发 `/总结群聊`（或 `群聊总结`、`群聊摘要`），在**插件配置**里开关，还能改机器人名字、提示词、时间范围、条数、冷却时间 |

需要额外准备才能用、所以**默认关着**的：

| 开关 | 做什么 |
|---|---|
| 接听语音电话 `call_enable` | 见下文「语音通话」，需要豆包 API Key |
| 掉线邮件提醒 `notify_email` / `notify_email_code` | 填邮箱和它的 SMTP 授权码（QQ 邮箱：网页版「设置 → 账号」开启 POP3/SMTP 服务后生成）后：掉线要扫码时把二维码发到邮箱（就在正文里）、要手机确认时提醒、重新上线再发一封（同类 10 分钟最多一封）。完全免费 |

始终生效的基础能力：私聊/群聊收发；@ 与引用（收到的 @，包括 @别人，会转成带 wxid 的 At；被引用的图片也会带上）；收图片、语音、视频，发图片、语音、视频、文件；下载不下来的表情标成「[表情包]」；微信没有合并转发，自动拆成普通消息按顺序发；优先用群昵称；`WeChatClient.avatar(wxid)` 取联系人头像。

**目前做不到的**（agent-wechat 本身不支持）：发送动态表情（GIF 会变成静态图）、大部分群友表情下载不了、发图不压缩（每张图压到 64KB，大图只能另发文件）、拍一拍、转账红包、朋友圈、群语音通话、视频通话。

## 语音通话

1. 在火山引擎控制台开通「端到端实时语音大模型」，拿到 API Key。
2. 把 Key 放进 AstrBot 进程的环境变量 `DOUBAO_REALTIME_API_KEY`（Docker 用 `environment` / `env_file`，改完要重建容器）。
3. 适配器配置里打开「接听语音电话」，在「允许来电的 wxid」里填人（填一个 `*` 就是所有好友都能打）。
4. 用白名单里的微信给机器人打**语音电话**即可。日志里 `[wechat_call]` 会记录接听、双方说的话和挂断时长。

可调：音色（可填自己复刻的 `S_` 开头音色）、语速、接通第一句、电话人设文件（留空用自带的 `call_prompt.md`，建议照着写一份自己角色的）、单次最长分钟数。只支持一对一语音，同一时间只接一个人（通话中别人打来会拒接）；电话内容不进 AstrBot 的对话记忆；豆包按用量计费；需要守护进程 ≥ 1.4.0。

## 常见问题

| 日志里看到 | 怎么办 |
|---|---|
| `连不上守护进程 http://127.0.0.1:6174` | 守护进程没在跑：`sudo systemctl status agent-wechat`。AstrBot 在 Docker 里：没开 host 网络（容器里的 127.0.0.1 是容器自己） |
| `找不到 token 文件` | 路径填错，或 Docker 里没把 token 文件挂进容器 |
| `守护进程拒绝了 token` | token 文件内容和守护进程配置里 `tokenFile` 指向的不是同一个 |
| `微信还没登录，先等守护进程自己重连` | 正常：守护进程刚重启时会自己重连，45 秒内没连上才出二维码 |
| `微信登录掉了（服务码 -13）` | 微信服务器拒绝了这次登录（被踢），守护进程不会自己重连，日志里马上出二维码，重新扫码即可；别短时间连着扫 |
| 二维码扫不出来 | 日志窗口换成深色背景再扫；或者直接打开上面说的 `login_qr.png` |
| 机器人不回话 | 先看会话有没有加进白名单（⑤）；群里要 @ 它或带唤醒词 |
| 微信里收到「异常登录」提示 | 按手机微信的引导验证；别频繁重启守护进程、别在别处同时登录这个号 |

## 全部配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:6174` | 守护进程地址 |
| `token_file` / `token` | `/run/secrets/wechat-api-token` / 空 | 守护进程 API token，优先读文件（改了文件不用重启） |
| `account` | 空 | 守护进程里的账号 id 或别名，留空用第一个；一个都没有时自动建 `astrbot` |
| `auto_login` | `true` | 自动扫码登录 |
| `download_media` | `true` | 下载收到的图片、语音 |
| `media_wait_seconds` / `max_media_mb` | `20` / `32` | 媒体下载等待时间、大小上限 |
| `send_min_interval` / `send_max_interval` | `2` / `4` | 两条发送之间的随机间隔（秒） |
| `send_max_per_minute` | `12` | 每分钟最多发几条，0 不限 |
| `max_message_age_seconds` | `300` | 超过这个秒数的旧消息不处理，0 不限 |
| `quote_reply` | `true` | 群聊偶尔引用对方 |
| `skip_recalled` | `true` | 撤回了就不回 |
| `attach_recent_image` / `recent_image_seconds` | `true` / `180` | 先发图再 @ 时带上图、时限 |
| `parse_share_card` | `true` | 分享卡片 / 聊天记录转文字 |
| `split_long_voice` | `true` | 长语音自动切段 |
| `watch_video` | `true` | 看视频（群里只在被 @ / 引用 / 正在聊时看） |
| `send_image_original` | `true` | 大图附原图文件 |
| `notify_email` / `notify_email_code` | 空 / 空 | 掉线提醒发给哪个邮箱（自己发给自己）、它的 SMTP 授权码；留空不发 |
| `notify_smtp` | `smtp.qq.com:465` | SMTP 服务器（SSL），163 邮箱填 `smtp.163.com:465` |
| `call_enable` | `false` | 接听微信语音来电 |
| `call_allow` | `[]` | 允许接听的来电 wxid，其他人自动拒接；填 `*` = 所有好友 |
| `call_voice` | `zh_female_xiaohe_jupiter_bigtts` | 豆包音色（小何 2.0） |
| `call_speech_rate` | `0` | 电话语速，-50~100，`100` 是 2 倍速 |
| `call_greeting` | `喂？你好呀～` | 接通后说的第一句话 |
| `call_prompt_file` | 空 | 电话人设文件，留空用自带的 `call_prompt.md` |
| `call_max_minutes` | `10` | 单次通话最长分钟数 |
| `call_api_key_env` | `DOUBAO_REALTIME_API_KEY` | 从哪个环境变量读豆包 API Key |

## 开发与测试

测试不需要真的微信：`tests/` 里有假的守护进程（含登录、通话控制/媒体 WebSocket）和假的豆包服务。在 AstrBot 环境里运行：

```bash
cd AstrBot/data/plugins
PYTHONPATH=/path/to/AstrBot:. python -m pytest -q astrbot_plugin_wechat_agent/tests
```

## 致谢

- [agent-wechat](https://github.com/thisnick/agent-wechat)（MIT）：本插件对接的微信守护进程。
- [MaiBot](https://github.com/Mai-with-u/MaiBot)（GPL-3.0）：群聊引用概率的思路和参数参考了 MaiBot。

## 许可证

[AGPL-3.0](LICENSE)
