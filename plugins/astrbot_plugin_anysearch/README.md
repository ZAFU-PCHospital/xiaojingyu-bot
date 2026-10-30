# Anysearch实时联网搜索

这是一个面向 AstrBot 的 Anysearch 原生搜索插件。


## 插件优势

- 实时联网检索：适合查询新闻、文档、事实核验、产品信息和近期变化。
- 检索覆盖更广：支持网页、新闻、代码、文档、学术、图片、视频等内容类型。
- 批量查询：一次并行执行多个独立搜索，适合对比资料或快速收集多条线索。
- 网页正文提取：当搜索摘要不够时，可提取公开网页的 Markdown 正文，方便 Bot 深入阅读。

## 指令

```text
/anysearch 关键词
/anysearch extract https://example.com/page
/anysearch batch 关键词1,关键词2,关键词3
```

也可以使用中文别名：

```text
/搜索 关键词
```

## LLM 工具

插件会向 AstrBot 注册以下函数工具，供LLM在对话中自动调用：

- `anysearch_search`：通用实时联网搜索，适合新闻、事实、文档和网页资料检索。
- `anysearch_extract`：提取公开网页的可读 Markdown 正文。
- `anysearch_batch_search`：并行执行 1 到 5 个搜索查询。

## 配置

`api_key` 为可选项。留空时仍可使用 Anysearch 匿名访问；如果你希望获得更稳定的额度，请在https://anysearch.com/console/api-keys 获取Api后在配置界面填入 Anysearch API Key。

`command_result_chunk_size` 控制长结果分段长度，默认 `1200`。直接使用 `/anysearch`、`/anysearch extract` 或 `/anysearch batch` 时，单条结果超过这个长度才会按下面的开关分段发送；填写 `0` 表示不分段。

`chunk_private_command_results` 控制私聊长结果自动分段，默认开启，仅对 OneBot v11/NapCat 私聊生效。在该环境下，AstrBot 默认会把超过 `platform_settings.forward_threshold` 的长文本包装成合并转发消息；如果协议端无法发送私聊合并转发，可能出现 `send_private_forward_msg`、`发送转发消息`、`retcode=1200` 一类报错。保持默认私聊分段即可避开大多数情况。

`chunk_group_command_results` 控制群聊长结果自动分段，默认关闭，避免长搜索结果在群聊里拆成多条造成刷屏。只有在群聊合并转发也不可用时才建议开启；否则让 AstrBot 继续按全局 `platform_settings.forward_threshold` 处理长消息更适合群聊。

## 安全说明

搜索关键词和待提取的 URL 会发送到 `https://api.anysearch.com/mcp`。请不要用本插件查询密码、私人文档、商业机密或其他敏感信息。

`anysearch_extract` 只接受公开的 `http` 和 `https` URL。插件会在调用 Anysearch 前拒绝本地地址、回环地址、私网 IP、链路本地地址、组播地址和未指定地址。
