# 小鲸鱼

小鲸鱼是基于 AstrBot 和 QQ 官方机器人的群聊助手。本仓库保存群友、电脑医院客服和管理三类人格的配置模板、自研插件和经过适配的第三方插件源码。

- **群友**：按概率接话、表情包、链接摘要、群聊总结、Steam 查询与订阅、梗查询、群内长期记忆。
- **客服**：结合群消息、技术文档和共享案例回答电脑问题，自动沉淀维修案例，分析 QQ 文件中的 Windows dump，查询活动排队人数。
- **管理**：GitHub Issue 草稿与确认创建、官网活动查询与确认操作、讨论转待办、站点和备份告警。

客服群共享脱敏案例正文；群友记忆、客服案例和管理数据使用不同作用域。历史群聊、个人记忆、客户案例、账号凭据和表情图片不在仓库中，新部署从空数据开始。

## 环境

- Linux x86-64，Docker Compose、Python 3.10+、Git；Windows 可使用具备 Linux Docker 的 WSL2。
- `compose.yaml` 采用 Linux host 网络，AstrBot 与本机网关、dump 后端通过 `127.0.0.1` 通信。公网 QQ webhook 需要独立域名、HTTPS 和 QQ 官方机器人。
- 按生产实例固定 AstrBot 镜像摘要；构建额外依赖需要外网和磁盘空间。低内存服务器可在其他同架构机器构建后导入镜像。
- 需要自己的 QQ 官方机器人 AppID/AppSecret、可用的 WorkBuddy 上游授权及网关 API key。搜索、图片、Embedding、GitHub App 和官网机器接口凭据按实际启用的功能配置。

## 从空环境开始

1. 克隆仓库，复制 `.env.example` 为 `.env`。`WHALE_ANNOUNCEMENT_GROUP_IDS` 使用逗号分隔普通群号；备份告警使用的两个变量见 [备份告警](docs/backup-alert.md)。这些值只写入本机 `.env`。
2. 准备插件和上游资源：

   ```bash
   mkdir -p data
   python3 scripts/install_plugins.py --data-dir ./data
   python3 scripts/fetch_plugin_assets.py --data-dir ./data
   ```

   资源下载固定上游提交并核对 SHA-256。梗词库由原上游提供，第三方词条版权与使用条件见其项目；蓝色大肥鱼表情包需自行取得并导入，见 [素材说明](docs/assets-and-licenses.md)。
3. 建立私密群映射：将 `config/group-mapping.example.json` 复制到仓库外，填入 QQ Bot AppID、官方群 OpenID 和群号。QQ 官方接口的群 OpenID 与普通群号不同；先在目标群接入机器人，再从 AstrBot 官方适配器事件或管理界面确认映射。
4. 在仓库外创建**空目录**，渲染配置：

   ```bash
   python3 scripts/render_plugin_configs.py --mapping /private/group-mapping.json --out /private/whale-rendered
   mkdir -p data/config
   cp /private/whale-rendered/*_config.json data/config/
   ```

   `backend_token`、搜索 API key、QQ 凭据等保留为空，按下文单独填写。首次安装配置后启动：

   ```bash
   docker compose build
   docker compose up -d
   ```

5. 登录 AstrBot 面板，添加 QQ 官方机器人并配置 HTTPS webhook、事件订阅和群消息权限。将 webhook 链接填入 QQ 开放平台；公开地址的反向代理参考 [nginx 示例](deploy/nginx.conf.example)。
6. 在 AstrBot 面板添加 WorkBuddy OpenAI 兼容提供商，模型 ID 使用 `workbuddy/cn:deepseek-v4-flash`；配置图片输入和图片描述提供商。添加硅基流动 BGE-M3 Embedding，建立 `电脑医院技术与案例` 和 `小鲸鱼群友长期记忆` 两个独立知识库，前者导入 [电脑医院技术文档](https://github.com/ZAFU-PCHospital/ZAFU-PCHospital-Doc)，后者初始为空。
7. 插件加载完成后，创建人格、配置组和群路由：

   ```bash
   python3 scripts/restore_profiles.py --rendered-dir /private/whale-rendered
   python3 scripts/restore_profiles.py --rendered-dir /private/whale-rendered --apply
   ```

   第二条通过交互输入 AstrBot 管理员账号密码。脚本在目标人格、配置组或群路由已存在时停止，避免覆盖。也可以按 `config/personas.json` 和渲染出的 `profiles.json` 手工创建。客服、群友、管理群分别使用各自配置组；不要把客服知识库挂到群友或管理组。
8. 按 [服务接入](docs/services.md) 配置网关、dump 后端、搜索、GitHub App 和官网令牌。密钥和运行数据只放入仓库外的私密文件，完成后在 AstrBot 中重载相关插件。

`scripts/install_plugins.py` 默认不覆盖已有插件目录。明确要覆盖源码时才加 `--overwrite`；该命令不清理已有运行数据。

## 运行数据与功能检查

`data/` 包括 AstrBot 数据库、配置、官方机器人凭据、插件状态、知识库、个人记忆和群内处理记录。请使用你自己的私密备份方案保护整个目录；本仓库不包含这些数据。模型上游授权、官方机器人权限、GitHub App 安装和官网机器凭据需要重新配置。

配置完成后，在各自的测试群验证：群友 @ 回复及记忆存取；客服问题检索、案例入库和文件分析；管理群 Issue 草稿及确认流程。创建活动、群发通知等写操作应先用隔离环境验证。

## 目录

- `plugins/`：自研插件及保留原许可证的第三方插件源码；不含运行时数据。
- `config/`：三类人格、配置组、插件和外部服务模板。
- `integrations/`：dump 后端和网站服务器备份告警 hook。
- `scripts/`：安装、配置渲染、群路由恢复、上游数据下载和解析器构建脚本。
- `deploy/`：AstrBot Compose、反向代理及网关服务示例。

项目自研代码使用 [AGPL-3.0](LICENSE)；第三方组件及素材遵循各自许可，详见 [来源与许可](docs/assets-and-licenses.md)。

公开快照相对运行实例的配置化调整见 [可迁移调整](docs/portability.md)。
