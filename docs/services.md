# 外部服务接入

以下路径是当前服务布局的示例。凭据文件与运行数据放在仓库外，按各服务运行用户设置读取权限。

## WorkBuddy 网关

当前网关二进制对应 [workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel) 提交 `d66384d9e3d69655f799d7d8f2e5fba1946e00ab`。在构建机编译 Linux x86-64 版本：

```bash
git clone https://github.com/linguo2625469/workbuddy2api-panel.git
cd workbuddy2api-panel
git checkout --detach d66384d9e3d69655f799d7d8f2e5fba1946e00ab
CGO_ENABLED=0 go build -trimpath -o wb2api ./cmd/server
```

建立 `workbuddy` 系统用户，将二进制放入 `/opt/bot-stack/workbuddy/`，以 `config/workbuddy.example.json` 为基础创建该目录的 `config.json`。生成独立 `api_key`，仅监听 `127.0.0.1:7863`，保证数据和授权目录对运行用户可写。安装 [systemd 模板](../deploy/workbuddy.service.example) 后，根据网关面板完成上游授权。AstrBot 使用 `http://127.0.0.1:7863/v1` 及相同 key；群成员不能访问网关管理凭据。

客服主动问答插件还读取 `/AstrBot/data/plugin_data/hospital_proactive/credentials.private.json`。从 `config/proactive-credentials.example.json` 复制到宿主机 `data/plugin_data/hospital_proactive/credentials.private.json`，填入网关地址和 key。已保存的开关和模型名在 `astrbot_plugin_hospital_proactive_config.json`。

## QQ 官方机器人

在 QQ 开放平台新建或使用已有机器人，在 AstrBot 中填写 AppID 和 AppSecret，使用平台提供的 webhook URL。按实际场景订阅群 @ 与群消息事件，并在需要读取普通群消息的群开放对应能力。先在目标群发送 @ 消息，核对事件中的官方群 OpenID，再填入私密映射文件。

客服、群友、管理群通过 AstrBot 配置组和群路由隔离，插件也有独立会话白名单。群友自动接话概率、客服主动答疑、活动通知等开关以 `config/templates.json` 为基准；部署者可在对应插件配置中调整。

## 搜索、图片、案例与记忆

- 给 AnySearch 插件配置自己的搜索 API key。
- 给模型提供商启用图片输入能力，设置图片描述 provider；不要把未授权的图片转发到不受信任的服务。
- 使用硅基流动的 `BAAI/bge-m3` 或兼容 Embedding，创建两个独立知识库。电脑医院技术文档固定提交 `d676e4fc6cdb0bcd25f956704fad5d7e52ecf504`；群友长期记忆库保持独立。
- 客服案例存储和向量索引在运行时自动创建。案例正文仅在已启用的客服群共享；群聊天原文和客户文件不跨群复制。自动入库受到模型判断、可接收消息范围与当前群文本缓存限制。

从空的客服知识库导入与当前实例相同版本的 17 篇公开文档：

```bash
git clone https://github.com/ZAFU-PCHospital/ZAFU-PCHospital-Doc.git /tmp/pch-doc
git -C /tmp/pch-doc checkout --detach d676e4fc6cdb0bcd25f956704fad5d7e52ecf504
python3 scripts/prepare_hospital_docs.py --source /tmp/pch-doc --output /private/hospital-documents.json
python3 scripts/import_hospital_docs.py --documents /private/hospital-documents.json --kb-id YOUR_KB_ID
python3 scripts/import_hospital_docs.py --documents /private/hospital-documents.json --kb-id YOUR_KB_ID --apply
```

首次执行只展示计划；`--apply` 交互登录 AstrBot 并创建导入任务。若任务中断或已存在文档，先查看任务与知识库状态，不要盲目重复导入。

## dump 后端

后端源码、`ntoseye` 补丁及原许可位于 `integrations/dump-backend/`。在构建机安装 Rust 工具链并运行：

```bash
sh scripts/build_dump_tools.sh
```

将生成的 `ephemera`、`ntoseye-hospital` 放在 `/opt/pc-hospital/bin/`，将 `backend.py` 放在 `/opt/pc-hospital/`。创建 `pchospital` 系统用户、组和 `/var/lib/pc-hospital`；`/etc/pc-hospital` 权限 750、属主 root、属组 pchospital。生成至少 32 字符的 `/etc/pc-hospital/token`，属主 root、属组 pchospital、权限 640。安装 `pc-hospital.service` 并启动；服务只监听 `127.0.0.1:6197`。将同一 token 填入 AstrBot 的 `astrbot_plugin_pc_hospital` 插件配置 `backend_token`，不要提交该配置。

## GitHub Issue 与官网活动

GitHub 使用安装到 `ZAFU-PCHospital/zafu-pchospital-web` 的 GitHub App，`Issues: write`、`Metadata: read`。复制 `config/management-credentials.example.json` 到宿主机 `data/plugin_data/management_whale/credentials.private.json`，填写 App ID、installation ID 和容器内可读的私钥路径。仓库固定在插件源码中。

官网活动写操作使用官网单独授予的机器 token，填写同一私密配置中的 `website_token`；确认官网对应接口已启用后才设置 `website_writes_enabled`。`announcement_group_bindings` 是普通群号到官方群 OpenID 的映射；同时在 `.env` 的 `WHALE_ANNOUNCEMENT_GROUP_IDS` 中配置允许通知的群号。活动草稿须由管理群成员确认；通知不具备真正的 @全体成员能力。

## 版本与隔离

本仓库中的自研插件和有适配的第三方插件来自同一时点的线上源码。AstrBot、网关及解析器版本在 Compose、模板和构建脚本中固定；第三方网络服务和 QQ 账号的授权状态需要由部署者配置。公开仓库不提供历史群聊、案例、订阅、草稿或个人记忆。
