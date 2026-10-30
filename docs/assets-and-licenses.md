# 来源、许可与表情素材

项目自研部分采用 AGPL-3.0。以下插件保留原项目许可证，仓库中的对应插件目录内包含各自的 `LICENSE`：

| 组件 | 来源 | 许可证 |
| --- | --- | --- |
| AnySearch | https://github.com/AgIzT/astrbot_plugin_anysearch | MIT |
| MemeWiki | https://github.com/in1t1a1/astrbot_plugin_memewiki | MIT；词条数据由其来源单独约束 |
| Meme Manager | https://github.com/anka-afk/astrbot_plugin_meme_manager | MIT |
| Minecraft Wiki | https://github.com/Mxpea/astrbot_plugin_minecraft_wiki | AGPL-3.0 |
| 简单长期记忆 | https://github.com/piexian/astrbot_plugin_simple_long_memory | AGPL-3.0 |

本仓库的第三方插件包含线上适配改动。`config/upstream-assets.json` 固定 MemeWiki 原始只读词库、图标及 Meme Manager 前端字体的上游提交和哈希。`scripts/fetch_plugin_assets.py` 从这些上游地址下载资源到本机 AstrBot 数据目录，不写入本仓库。

MemeWiki 原仓库在 2026-10-30 返回 404。词库备用地址指向 [dsh-memewiki](https://github.com/Liujelly/dsh-memewiki) 固定提交；其 Git blob 与原版本一致，下载后还会核对 SHA-256。原插件图标作为可选资源，获取不到不影响查询功能。词条内容仍遵循其原始来源的使用条件。

蓝色大肥鱼表情来源于 [赤风RED 的视频](https://www.bilibili.com/video/BV1V88G6TEvg/)。仓库不包含图片；请取得作者允许使用的素材，在 AstrBot 的 Meme Manager 中导入本地图包，图包 ID 设为 `whale-blue-fish`。重新为图片配置类别后，群友人格才能从相应分类选图。运行时自动收藏的图片也保存在私有 `data/`，不会进入仓库。

配置模板保留了原实例使用的 Cloudflare R2 图片存储类型，但不含账号信息。发送表情前需在 Meme Manager 中填写自己的对象存储信息，或选择插件支持且适合当前 QQ 官方接口的其他图片存储方式，并验证图片可被 QQ 获取。

依赖的外部系统包括 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[WorkBuddy2API Panel](https://github.com/linguo2625469/workbuddy2api-panel)、[ntoseye](https://github.com/dmaivel/ntoseye) 和 [ephemera](https://github.com/vmi-rs/ephemera)。各自的许可、账号条件和上游可用性独立于本项目。
