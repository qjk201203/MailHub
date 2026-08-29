# -*- coding: utf-8 -*-
"""
MailHub 多邮箱聚合客户端 - 配置

内置常见邮箱的 IMAP/SMTP 服务器预设，方便添加账号时自动匹配。
"""

# 常见邮箱服务器预设（域名 -> (IMAP主机, IMAP端口, SMTP主机, SMTP端口)）
PROVIDERS = {
    # 国内
    "163.com": ("imap.163.com", 993, "smtp.163.com", 465),
    "126.com": ("imap.126.com", 993, "smtp.126.com", 465),
    "yeah.net": ("imap.yeah.net", 993, "smtp.yeah.net", 465),
    "qq.com": ("imap.qq.com", 993, "smtp.qq.com", 465),
    "vip.qq.com": ("imap.qq.com", 993, "smtp.qq.com", 465),
    "foxmail.com": ("imap.qq.com", 993, "smtp.qq.com", 465),
    "sina.com": ("imap.sina.com", 993, "smtp.sina.com", 465),
    "sina.cn": ("imap.sina.com", 993, "smtp.sina.com", 465),
    "aliyun.com": ("imap.aliyun.com", 993, "smtp.aliyun.com", 465),
    "139.com": ("imap.139.com", 993, "smtp.139.com", 465),
    "wo.cn": ("imap.wo.cn", 993, "smtp.wo.cn", 465),
    "189.cn": ("imap.189.cn", 993, "smtp.189.cn", 465),
    "qiye.163.com": ("imap.qiye.163.com", 993, "smtp.qiye.163.com", 465),
    "exmail.qq.com": ("imap.exmail.qq.com", 993, "smtp.exmail.qq.com", 465),
    # 国外
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "outlook.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "hotmail.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "live.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "msn.com": ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "ymail.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465),
    "aol.com": ("imap.aol.com", 993, "smtp.aol.com", 465),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "zoho.com": ("imap.zoho.com", 993, "smtp.zoho.com", 465),
    "gmx.com": ("imap.gmx.com", 993, "smtp.gmx.com", 465),
}


def lookup_provider(email_addr):
    """根据邮箱地址后缀，返回 IMAP/SMTP 服务器预设（找不到返回 None）"""
    domain = email_addr.rsplit("@", 1)[-1].lower() if "@" in email_addr else ""
    return PROVIDERS.get(domain)
