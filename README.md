# MailHub 多邮箱聚合客户端

一个自写、纯 Python 标准库实现的多邮箱聚合收件箱。零第三方依赖，解决「现成 webmail 无法统一看待多个邮箱」的痛点。

## 核心特性

- **统一收件箱**：多个服务商邮箱（163/126/Gmail/QQ/Outlook 等）的邮件聚合到一个界面按时间排序查看
- **解决 163 的 IMAP ID 要求**：登录后自动发送 IMAP ID 命令，根治网易「Unsafe Login」导致读不出邮件的问题（这是 SnappyMail/RoundCube/FireMail 等现成方案都卡住的关键）
- **自动匹配服务器**：添加账号时根据邮箱后缀自动填 IMAP/SMTP 服务器，也可手动指定
- **纯标准库**：不依赖 pip 安装任何第三方包，Python 3 自带即可运行

## 已支持的邮箱预设

国内：163/126/yeah/QQ/foxmail/sina/aliyun/139/wo/189/网易企业邮/腾讯企业邮
国外：Gmail/Outlook/Hotmail/Live/MSN/Yahoo/AOL/iCloud/me/Zoho/GMX

（见 `config.py` 的 PROVIDERS 字典，可自行增改）

## 运行

```bash
cd mailhub
python3 app.py
```

启动后访问：`http://127.0.0.1:20111`

## 目录结构

```
mailhub/
├── app.py          # Web 服务（http.server 标准库）+ 前端页面 + API
├── imap_client.py  # IMAP 客户端（含 163 的 ID 命令处理）
├── storage.py      # 账号存储（SQLite）
├── config.py       # 邮箱服务器预设
└── mailhub.db      # 账号数据库（运行后自动生成）
```

## API

- `GET  /api/inbox`                         聚合收件箱
- `GET  /api/accounts`                      账号列表
- `POST /api/account`                       添加账号（自动匹配服务器）
- `POST /api/account/delete`                删除账号

## 注意事项

- 密码栏填「授权码/应用专用密码」，不是邮箱登录密码：
  - 163/126/yeah：网易邮箱 → 设置 → POP3/SMTP/IMAP → 客户端授权密码
  - QQ：QQ邮箱 → 设置 → 账户 → 生成授权码（16位）
  - Gmail：Google 账号 → 安全性 → 应用专用密码

## 已知限制（当前 MVP）

- 仅「收件」聚合，发信/回复未做（后续可加 smtplib）
- 每次刷新实时拉取，未做本地持久化缓存（大邮箱会慢）
- 单用户，无登录鉴权
