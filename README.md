# astrbot_plugin_wechat_agent

通过 [agent-wechat](https://github.com/thisnick/agent-wechat) 守护进程，把**个人微信号**接入 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的平台适配器插件。插件直接装在 AstrBot 里，不需要额外的中转服务。

> ⚠️ **使用前请读**：agent-wechat 是非官方微信客户端，使用它违反《微信软件许可及服务协议》，账号有被限制或封禁的风险。请只登录你自己有权使用的、可以承受封号风险的小号，不要用于群发、营销、批量控号等用途。本插件只是对接 agent-wechat 的 HTTP 接口，不包含任何破解或逆向微信协议的代码。

## 功能

| 功能 | 说明 |
|---|---|
| 私聊 / 群聊 | 群里 @机器人、或消息里带 AstrBot 唤醒词即可触发 |
| @ 与引用 | 收到的 @（含 @别人，会转成带 wxid 的 At）、引用消息（含被引用的图片）都会交给 AstrBot；回复时按概率偶尔引用对方。群里先发图、3 分钟内再 @机器人，这张图会带进 @ 的那条（微信图文不能同条发），看图、改图插件都能拿到 |
| 图片 / 语音 / 视频 / 文件 | 收：图片、语音（自动转 wav）；发：图片、语音（TTS）、视频、文件。超过 55 秒的语音按换气停顿自动切成几条发（微信语音最长 60 秒） |
| 分享卡片 / 聊天记录 | 收到的文章、视频、音乐、视频号分享转成「标题 + 简介 + 网址」文字（网址单独一行，链接解析插件能认）；聊天记录合并转发转成「谁：说了什么」 |
| 撤回 | 对方撤回了消息、机器人还没回的话，这条回复就不发了 |
| 发合并转发 | 微信没有合并转发，自动拆成普通消息按顺序发 |
| 群昵称 / 头像 | 优先用群昵称；`WeChatClient.avatar(wxid)` 可取联系人头像 |
| 防风控 | 所有发送串行，条间随机间隔 2~4 秒、每分钟最多 12 条，等守护进程真正发出再发下一条 |
| 拟人 | 群聊引用按概率决定 |
| 旧消息保护 | 机器人停机很久再恢复时，超过 5 分钟的旧消息不再回复 |
| **语音通话** | 白名单里的人给机器人打微信语音，自动接听，接到 [豆包端到端实时语音](https://docs.volcengine.com/docs/doubao-voice/end-to-end-real-time-voice-full-duplex-version?lang=zh)（全双工），可打断 |
| **群聊总结** | 群里发 `/总结群聊`，总结最近 24 小时的群聊 |

**目前做不到的**（agent-wechat 本身不支持）：发送动态表情（GIF 会变成静态图）、大部分群友表情下载不了、拍一拍、群语音通话、视频通话。

## 准备 agent-wechat 守护进程

需要 **agent-wechat ≥ 1.4.0**（语音通话要 1.4.0；不用通话 1.3.x 也能跑）。

1. 从 [Releases](https://github.com/thisnick/agent-wechat/releases) 下载对应平台的 `wx` 程序（Linux 需要 `libpulse0`），或用官方 Docker 镜像 `ghcr.io/thisnick/agent-wechat-native`。
2. 按 agent-wechat 文档启动守护进程（默认监听 `127.0.0.1:6174`），用 `wx auth login` 扫码登录。
3. 守护进程第一次启动会在数据目录生成 owner token（`api-token` 文件），插件用它访问接口。AstrBot 在 Docker 里时，把这个文件只读挂进容器，例如挂到 `/run/secrets/wechat-api-token`。

## 安装插件

1. AstrBot WebUI → 插件 → 从链接安装：`https://github.com/heimixieb/astrbot_plugin_wechat_agent`
2. WebUI → 机器人 → 新增，选 **wechat_agent**，至少填好：
   - `base_url`：守护进程地址，默认 `http://127.0.0.1:6174`（AstrBot 在 Docker 里时建议用 host 网络，或填宿主机地址）
   - `token_file`：token 文件路径（或直接填 `token`）
3. 保存启用。日志出现 `[wechat_agent] 已连接守护进程，账号 wxid_…` 就通了。
4. 别忘了在 AstrBot 的白名单里放行会话，格式如 `wechat:FriendMessage:wxid_xxx`、群可直接写 `xxx@chatroom`。

## 适配器配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:6174` | 守护进程地址 |
| `token_file` / `token` | `/run/secrets/wechat-api-token` / 空 | 守护进程 API token，优先读文件（改了文件不用重启） |
| `account` | 空 | 守护进程里的账号 id 或别名，留空用第一个 |
| `download_media` | `true` | 是否下载收到的图片、语音 |
| `media_wait_seconds` / `max_media_mb` | `20` / `32` | 媒体下载等待时间、大小上限 |
| `send_min_interval` / `send_max_interval` | `2` / `4` | 两条发送之间的随机间隔（秒） |
| `send_max_per_minute` | `12` | 每分钟最多发几条 |
| `max_message_age_seconds` | `300` | 超过这个秒数的旧消息不处理，`0` 不限 |
| `call_enable` | `false` | 是否接听微信语音来电 |
| `call_allow` | `[]` | 允许接听的来电 wxid，其他人自动拒接 |
| `call_voice` | `zh_female_xiaohe_jupiter_bigtts` | 豆包音色（小何 2.0），也可以填自己复刻的 `S_` 开头音色 |
| `call_speech_rate` | `0` | 电话语速，-50~100，`0` 正常，`100` 是 2 倍速 |
| `call_greeting` | `喂？你好呀～` | 接通后说的第一句话 |
| `call_prompt_file` | 空 | 电话里用的人设文件，留空用插件自带的 `call_prompt.md` |
| `call_max_minutes` | `10` | 单次通话最长分钟数，到时自动挂断 |
| `call_api_key_env` | `DOUBAO_REALTIME_API_KEY` | 从哪个环境变量读豆包 API Key |

## 语音通话

1. 在火山引擎控制台开通「端到端实时语音大模型」，拿到 API Key。
2. 把 Key 放进 AstrBot 进程的环境变量 `DOUBAO_REALTIME_API_KEY`（Docker 用 `env_file`，改完要重建容器）。
3. 适配器配置里打开 `call_enable`，在 `call_allow` 填允许来电的 wxid。
4. 用白名单里的微信给机器人打**语音电话**即可。日志里 `[wechat_call]` 会记录接听、双方说的话和挂断时长。

说明：只支持一对一语音；电话内容不会进入 AstrBot 的对话记忆；豆包按用量计费。电话人设是独立的，建议参考 `call_prompt.md` 写一份自己角色的版本，用 `call_prompt_file` 指过去。

## 群聊总结

群里发 `/总结群聊`（也可以 `群聊总结`、`群聊摘要`，或 @机器人 加这几个字），机器人用当前对话模型把最近的群聊总结成几条要点。插件配置里可以改机器人名字、提示词、时间范围、条数和冷却时间（默认同一个群 5 分钟一次）。

## 开发与测试

测试不需要真的微信：`tests/` 里有假的守护进程（含通话控制/媒体 WebSocket）和假的豆包服务。在 AstrBot 环境里运行：

```bash
cd AstrBot/data/plugins
PYTHONPATH=/path/to/AstrBot:. python -m pytest -q astrbot_plugin_wechat_agent/tests
```

## 致谢

- [agent-wechat](https://github.com/thisnick/agent-wechat)（MIT）：本插件对接的微信守护进程。
- [MaiBot](https://github.com/Mai-with-u/MaiBot)（GPL-3.0）：群聊引用概率的思路和参数参考了 MaiBot。

## 许可证

[AGPL-3.0](LICENSE)
