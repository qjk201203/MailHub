# MailHub 多邮箱聚合客户端

一个自写、**纯 AI 生成**、纯 Python 标准库实现的多邮箱聚合 webmail 客户端。零第三方依赖，聚合多个邮箱（163 / 126 / Gmail / QQ / Outlook 等）到一个界面统一收发。

> **重要声明：本项目代码 100% 由 AI（大语言模型）自动生成，未经过人工逐行审查，也未经过专业安全审计。**
> 请自行阅读源码、评估风险后再用于重要数据。作者对因使用本项目造成的任何数据丢失、账号异常或安全事件不承担责任。

---

## 解决的问题

- **统一收件箱**：多个服务商邮箱的邮件聚合到一个界面按时间排序查看，不再「单个邮箱单个看」
- **根治 163「Unsafe Login」**：登录后自动发送 IMAP `ID` 命令（`imaplib` 无内置 ID 命令，需手工构造），这是 SnappyMail / RoundCube / FireMail 等现成方案都卡住的关键一步
- **统一的读写界面**：收件 / 发信 / 回复 / 全部回复 / 转发 / 附件 / 富文本，一个网页全搞定
- **纯标准库**：`imaplib + smtplib + email + sqlite3 + http.server`，不依赖 pip 安装任何第三方包，Python 3 自带即可运行

---

## 已支持功能

### 收发邮件
- 统一收件箱（聚合所有账号，按时间排序）
- 子文件夹浏览（IMAP 文件夹，含中文文件夹名，正确解码 modified UTF-7）
- 读取正文（HTML + 纯文本回退，处理 GBK / GB2312 等中文编码）
- 发送 / 回复 / 全部回复 / 转发
- 抄送（Cc）/ 密送（Bcc）
- 附件上传与下载
- 正文内嵌图片（`data:image` → `cid` 内联附件转换）
- 富文本编辑器（`document.execCommand`，加粗 / 斜体 / 列表等）
- 发件人完整邮箱显示（不用别名）

### 界面体验
- 响应式 Gmail 风格布局（桌面 3 栏 / 移动端抽屉，断点 900px）
- 深色 / 浅色主题
- 字号调节（小 / 中 / 大）
- 列表密度调节（紧凑 / 标准 / 宽松）
- 搜索过滤（主题 / 发件人 / 关键字）
- 排序（时间 / 主题 / 发件人）
- 分页「加载更多」
- 邮件 favicon 头像（Google s2 服务，带内存缓存 + 首字母回退）
- 发件人邮箱统一转小写显示

### 缓存与后台同步
- 邮件列表头缓存（SQLite `mail_cache`）
- 正文缓存（SQLite `body_cache`）
- 文件夹列表内存缓存
- favicon 内存缓存
- **后台正文预同步**：设置面板可选同步范围
  - 关闭（0）
  - 最近 100 / 500 / 2000 封
  - 近一年（全部，`-1`）
  - 同步覆盖**所有子文件夹**（近一年内），自动跳过 Gmail「所有邮件」这类全量归档，避免重复下载
  - 点开已缓存邮件「秒开」

---

## 已支持的邮箱预设

**国内**：163 / 126 / yeah / QQ / vip.qq / foxmail / sina / sina.cn / aliyun / 139 / wo / 189 / 网易企业邮（qiye.163）/ 腾讯企业邮（exmail.qq）

**国外**：Gmail / googlemail / Outlook / Hotmail / Live / MSN / Yahoo / ymail / AOL / iCloud / me / Zoho / GMX

（见 `config.py` 的 `PROVIDERS` 字典，可自行增改）

---

## 运行

```bash
cd mailhub
python3 app.py
```

启动后访问：`http://127.0.0.1:20111`（绑定 `0.0.0.0`，NAS 局域网内其他设备也可访问）

### Docker

```bash
docker build -t mailhub .
docker run -d -p 20111:20111 -v /你的数据目录:/app/data mailhub
```

数据目录由环境变量 `MAILHUB_DATA_DIR` 指定（默认脚本同目录），账号数据库 `mailhub.db` 存于此。

---

## 目录结构

```
mailhub/
├── app.py            # Web 服务（http.server 标准库）+ API
├── imap_client.py    # IMAP 客户端（含 163 的 ID 命令、UTF-7 解码）
├── storage.py        # SQLite 存储（账号 / 邮件缓存 / 正文缓存 / 设置）
├── config.py         # 邮箱服务器预设
├── frontend/
│   └── index.html    # 前端页面（独立文件，改界面不需要动 app.py）
├── Dockerfile        # Docker 部署
├── .gitignore        # 排除敏感文件（数据库等）
├── LICENSE           # MIT 许可
└── mailhub.db        # 账号数据库（运行后自动生成，不入库）
```

---

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/`                | 网页前端 |
| GET  | `/api/inbox`        | 聚合收件箱（支持 `folder`、`offset`、`limit` 参数分页） |
| GET  | `/api/accounts`     | 账号列表 |
| GET  | `/api/folders`      | 指定账号的文件夹列表 |
| GET  | `/api/mail`         | 单封邮件全文（`account` + `folder` + `seq`） |
| GET  | `/api/favicon`      | 域名 favicon（`domain` 参数） |
| GET  | `/api/settings`     | 读取设置（同步范围 + 同步状态） |
| GET  | `/api/sync/status`  | 后台同步进度（`running` / `done` / `total` / `current_account` / `current_folder` / `last_error`） |
| POST | `/api/account`      | 添加账号（自动匹配服务器） |
| POST | `/api/account/delete` | 删除账号 |
| POST | `/api/send`         | 发送邮件（正文 / 抄送 / 密送 / 附件 / 内嵌图） |
| POST | `/api/settings`     | 保存设置并触发后台同步（`sync_range`） |

---

## 注意事项（务必阅读）

1. **纯 AI 生成，未经审计**：代码由 AI 自动生成，请自行审查后再用于重要邮箱。
2. **密码填「授权码 / 应用专用密码」，不是邮箱登录密码**：
   - 163 / 126 / yeah：网易邮箱 → 设置 → POP3/SMTP/IMAP → 客户端授权密码
   - QQ / foxmail：QQ 邮箱 → 设置 → 账户 → 生成授权码（16 位）
   - Gmail：Google 账号 → 安全性 → 应用专用密码（需先开启两步验证）
3. **账号凭据明文存于本地 SQLite**：`mailhub.db` 以明文保存授权码，**切勿上传到公开仓库**（`.gitignore` 已排除）。请确保 NAS 文件权限安全。
4. **单用户，无登录鉴权**：服务启动即所有人可访问（绑定 `0.0.0.0`），公网暴露需自行加反向代理鉴权 / 内网访问。
5. **favicon 依赖代理**：拉取 Google favicon 走 `http://127.0.0.1:7890`（mihomo/clash 代理）。若代理未开，favicon 会静默回退为首字母头像，不影响邮件功能。可在 `app.py` 的 `PROXY` 变量修改。
6. **Gmail 国际线路较慢**：首次同步 / 读取会受线路影响，正文预同步 + 磁盘缓存可缓解。163 需保持 IMAP ID 命令（已内置）。
7. **同步范围与重复**：后台同步覆盖所有子文件夹（近一年），已排除 Gmail「所有邮件」归档避免重复。设置「关闭」即不同步。
8. **HTTPS 未内置**：服务为纯 HTTP，密码 / 授权码在局域网明文传输，建议仅内网使用或前置 HTTPS 反代。

---

## 开发进度

### ✅ 已完成
- [x] 多邮箱统一收件箱 + 子文件夹（含中文、嵌套、modified UTF-7 解码）
- [x] 163 Unsafe Login 根治（IMAP ID 命令）
- [x] 发送 / 回复 / 全部回复 / 转发
- [x] 抄送 / 密送 / 附件 / 内嵌图片
- [x] 富文本编辑器
- [x] 响应式布局 + 深色主题 + 字号 + 密度
- [x] 搜索过滤 + 排序 + 分页「加载更多」
- [x] 邮件列表头缓存 / 正文缓存 / 文件夹缓存 / favicon 缓存
- [x] 后台正文预同步（所有子文件夹，近一年范围可选）
- [x] favicon（代理 + 首字母回退）
- [x] Docker 部署

### 🚧 待完善（TODO）
- [ ] 草稿箱 —— 早期提及，尚未实现
- [ ] 登录鉴权 / 多用户
- [ ] HTTPS 支持
- [ ] 邮件删除 / 标记已读 / 移动文件夹（IMAP 写操作互交）
- [ ] 附件在线预览（图片缩放 / PDF）
- [ ] 任意范围同步的增量同步（当前每次全量重扫）

### ❌ 已知限制（当前版本）
- 单用户、无鉴权
- 账号凭据明文存本地 SQLite
- Gmail 发送需应用专用密码，且有每日发送量限制
- 邮件标签（Gmail label / 星标）未完全映射

---

## License

MIT（如无另行说明）。本仓库代码纯 AI 生成，按 MIT 许可使用，作者不承担任何责任。
