# 官网备份告警

备份脚本由官网服务器维护；`integrations/backup-alert/zafu-backup-alert` 是独立的通知 hook。它读取备份状态并只发送固定字段：主机、时间、状态、可用磁盘字节和分类后的短原因，不发送原始错误、配置或凭据。

## 机器人服务器

1. 在部署 `.env` 中配置 `WHALE_BACKUP_GROUP_UMO` 为已有的管理群完整会话标识，`WHALE_BACKUP_SOURCE_HOST` 为官网服务器 `hostname`。前者必须也出现在 `astrbot_plugin_whale_features` 的 `management_sessions` 中。
2. `data/plugin_data/whale_features/backup-alert-auth.json` 只保存 `{"sha256":"令牌的 SHA-256 十六进制值"}`，权限 600。这个令牌与部署通知令牌独立。
3. nginx 将 `/integrations/whale-deploy/backup` 代理到 `127.0.0.1:6196/deploy/backup`；可复用 [nginx 示例](../deploy/nginx.conf.example) 中的前缀 location。

## 备份服务器

1. 将 `integrations/backup-alert/zafu-backup-alert` 安装为 `/usr/local/bin/zafu-backup-alert`，属主 root、权限 700。
2. 生成独立随机令牌写入 `/etc/zafu-backup-alert.secret`，属主 root、权限 600；不要放在仓库或群消息中。在机器人服务器按上一步写入其 SHA-256 摘要。
3. 将 `config/backup-alert.settings.example.json` 复制为 `/etc/zafu-backup-alert.settings.json`，设置真实 HTTPS 地址，权限 600。
4. 在备份配置中设置 `ALERT_HOOK=/usr/local/bin/zafu-backup-alert`、`ALERT_ON_SUCCESS=0`。保留现有备份和磁盘控制逻辑。

失败按类别 30 分钟限频，通知结果未知时不自动重发；接收端只允许指定来源主机和指定管理群。QQ 接口接受消息和群成员看到消息是两个状态。正常备份不会发成功通知；需要成功通知时可按备份脚本的既有约定调整配置。
