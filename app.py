# -*- coding: utf-8 -*-
"""
MailHub - 多邮箱聚合客户端（收件聚合 MVP）
纯 Python 标准库实现，无第三方依赖。

启动后访问 http://127.0.0.1:20111
"""

import json
import re
import os
import datetime
import secrets
import threading
import urllib.request
import ssl
import smtplib
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import Header
from email.utils import formataddr
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import config
import storage
from imap_client import MailAccount

PORT = 20111

# 自动增量同步间隔（秒）：定期拉最新邮件到本地缓存，新邮件延迟 = 这个间隔
AUTO_SYNC_INTERVAL = 120

# 登录鉴权：设置环境变量 MAILHUB_PASSWORD 后启用（留空则无鉴权，仅适合纯内网且完全可信环境）
AUTH_PASSWORD = os.environ.get('MAILHUB_PASSWORD', '').strip()
AUTH_COOKIE = 'mailhub_token'
_session_tokens = set()  # 已登录的会话 token


def _is_authed(handler):
    """判断请求是否已通过登录校验。未设密码则恒为 True。"""
    if not AUTH_PASSWORD:
        return True
    token = ''
    cookie_header = handler.headers.get('Cookie', '')
    for part in cookie_header.split(';'):
        part = part.strip()
        if part.startswith(AUTH_COOKIE + '='):
            token = part[len(AUTH_COOKIE) + 1:]
    return token in _session_tokens


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MailHub 登录</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f0f4f9;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#fff;padding:32px;border-radius:16px;box-shadow:0 4px 24px rgba(15,23,42,.12);width:320px}
h1{font-size:20px;margin:0 0 20px;color:#0f172a;text-align:center}
input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #e2e8f0;border-radius:8px;font-size:15px;margin-bottom:12px}
button{width:100%;padding:12px;background:#1a73e8;color:#fff;border:none;border-radius:8px;font-size:15px;cursor:pointer}
.err{color:#c53929;font-size:13px;text-align:center;min-height:18px;margin-top:8px}
</style></head><body>
<div class="card"><h1>MailHub</h1>
<input id="pw" type="password" placeholder="访问密码" autofocus>
<button onclick="doLogin()">登 录</button>
<div class="err" id="err"></div></div>
<script>
async function doLogin(){
  const pw=document.getElementById('pw').value;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const d=await r.json();
  if(d.ok){location.href='/';}else{document.getElementById('err').textContent=d.error||'密码错误';}
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
</script>
</body></html>"""


# favicon 代理（用于拉取 Google favicon 等被墙资源）。
# 通过环境变量 MAILHUB_PROXY 配置，例如 http://127.0.0.1:7890；留空则直连（favicon 失败时回退首字母头像）。
PROXY = os.environ.get('MAILHUB_PROXY', '').strip()

# favicon 内存缓存：{domain: (content_type, bytes)}
_FAVICON_CACHE = {}


def fetch_favicon(domain):
    """拉取指定域名的 favicon，返回 (bytes, content_type) 或 None。带内存缓存，同域名只拉一次。"""
    if not domain:
        return None
    domain = domain.lower().strip()
    if domain in _FAVICON_CACHE:
        return _FAVICON_CACHE[domain]
    url = 'https://www.google.com/s2/favicons?domain=%s&sz=64' % domain
    # SSL context 放进 HTTPSHandler
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    if PROXY:
        proxy_handler = urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
        opener = urllib.request.build_opener(proxy_handler, https_handler)
    else:
        opener = urllib.request.build_opener(https_handler)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with opener.open(req, timeout=10) as resp:
            data = resp.read()
            ctype = resp.headers.get('Content-Type', 'image/png')
            _FAVICON_CACHE[domain] = (data, ctype)
            return data, ctype
    except Exception:
        return None


def strip_html(s):
    """剥离 HTML 标签，得到纯文本（用于列表预览）"""
    if not s:
        return ''
    s = re.sub(r'<style[^>]*>.*?</style>', ' ', s, flags=re.S | re.I)
    s = re.sub(r'<script[^>]*>.*?</script>', ' ', s, flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'&nbsp;', ' ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


# ---------- 聚合取信 ----------

def _fetch_account_mails(acc, folder='INBOX', limit=50):
    """连接单账号，取指定文件夹邮件头，返回标准化列表"""
    a = MailAccount(acc['email'], acc['password'],
                    acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    a.connect()
    mails = a.fetch_recent(folder, limit, light=True)
    for m in mails:
        m['account'] = acc['email']
        m['account_name'] = acc['display_name'] or acc['email']
        m['folder'] = folder
        m['preview'] = (m.get('text') or strip_html(m.get('html', '')) or '')[:120]
        m.pop('text', None)
        m.pop('html', None)
    a.logout()
    return mails


# 邮件列表内存缓存：{(account, folder): (timestamp, data)}
import time as _time
_CACHE = {}
_CACHE_TTL = 300  # 5 分钟


def _prefetch_bodies(account_email, folder, mids):
    """后台批量预取正文到磁盘缓存（静默，不阻塞）。批量 fetch 速度远快于逐封。"""
    acc = storage.get_account(account_email)
    if not acc:
        return
    # 过滤掉已有缓存的 seq
    to_fetch = [m.get('uid', '') for m in mids
                if m.get('uid', '') and not storage.load_body(account_email, folder, m.get('uid', ''))]
    if not to_fetch:
        return
    # 分批预取（每批最多 20 封，避免单次请求过大）
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        for i in range(0, len(to_fetch), 20):
            batch = to_fetch[i:i+20]
            bodies = a.fetch_bodies_batch(folder, batch)
            for seq, full in bodies.items():
                try:
                    full['account'] = acc['email']
                    full['account_name'] = acc['display_name'] or acc['email']
                    storage.save_body(account_email, folder, seq, full)
                except Exception:
                    continue
    except Exception:
        pass
    finally:
        a.logout()


def aggregate_all(account=None, folder='INBOX', limit=50, force=False, offset=0):
    """聚合取信。优先读磁盘缓存（秒开），force=True 或缓存空时才重新拉 IMAP。offset 用于分页（跳过前 N 封）。"""
    accounts = storage.list_accounts()
    if account:
        accounts = [a for a in accounts if a['email'] == account]

    flat = []
    need_fetch = False

    for acc in accounts:
        cached = storage.load_mail_cache(acc['email'], folder, limit + offset)
        if cached and not force:
            flat.extend(cached)
        else:
            need_fetch = True
            try:
                mails = _fetch_account_mails(acc, folder, limit + offset)
            except Exception:
                mails = []
            if mails:
                storage.save_mail_cache(acc['email'], folder, mails)
            flat.extend(mails)

    flat.sort(key=lambda x: x.get('date', ''), reverse=True)
    # 分页：跳过前 offset 封
    flat = flat[offset:offset + limit]
    result = {'all_mails': flat, 'total': len(flat)}

    # 后台批量预取当前列表所有邮件的正文到磁盘（无论 force 与否，补缓存）
    for acc in accounts:
        mids = [m for m in flat if m.get('account') == acc['email']]
        if mids:
            t = threading.Thread(target=_prefetch_bodies, args=(acc['email'], folder, mids))
            t.daemon = True
            t.start()

    return result


# 文件夹缓存：{account: (timestamp, [folders])}
_FOLDER_CACHE = {}
_FOLDER_CACHE_TTL = 600  # 10 分钟

# 后台同步状态
_SYNC_STATUS = {'running': False, 'done': 0, 'total': 0, 'current_account': '', 'current_folder': '', 'last_error': ''}

# 未读计数缓存：{account_email: {'INBOX': n, '文件夹raw': n, ...}}，后台定期刷新
_UNREAD_CACHE = {}
_UNREAD_UPDATED_AT = 0
_UNREAD_REFRESHING = {'active': False, 'current': ''}


def _refresh_unread_counts():
    """后台线程：遍历所有账号的所有文件夹，用 STATUS 统计未读数。
    每统计完一个文件夹立即写入 _UNREAD_CACHE（增量累加），前端轮询即可看到进度，不必等全部完成。"""
    global _UNREAD_UPDATED_AT
    _UNREAD_REFRESHING['active'] = True
    for acc in storage.list_accounts():
        email = acc['email']
        a = MailAccount(email, acc['password'], acc['imap_host'],
                        acc['imap_port'], acc.get('imap_ssl', 1))
        try:
            a.connect()
            folders = a.list_folders()
            # 保留旧缓存值，逐个覆盖更新（这样上一个账号的结果不会丢）
            cnt = dict(_UNREAD_CACHE.get(email, {}))
            _UNREAD_REFRESHING['current'] = email
            # 只统计真实文件夹（排除「所有邮件」归档 + Gmail 系统标签，避免重复和遍历过慢）
            for f in folders:
                name = f.get('name') or ''
                if ('所有邮件' in name) or ('All Mail' in name):
                    continue
                # 跳过 Gmail 的 [Google Mail] 系统标签（所有邮件/已删除/聊天/草稿/星标等，与其它标签重复）
                if '[Google Mail]' in name:
                    continue
                raw = f['raw']
                try:
                    q = a._quote_folder(raw)
                    typ, data = a.conn.status(q, '(UNSEEN)')
                    n = 0
                    if typ == 'OK' and data:
                        s = data[0].decode('utf-8', 'ignore') if isinstance(data[0], bytes) else str(data[0])
                        m = re.search(r'UNSEEN\s+(\d+)', s)
                        n = int(m.group(1)) if m else 0
                except Exception:
                    n = 0
                cnt[raw] = n
                # 每统计完一个文件夹立即写回缓存，前端随时能看到累加进度
                _UNREAD_CACHE[email] = dict(cnt)
            # INBOX 兜底（若 list_folders 未包含）
            if 'INBOX' not in cnt:
                cnt['INBOX'] = 0
            _UNREAD_CACHE[email] = cnt
        except Exception:
            # 连接失败也保留一个占位，避免前端以为该账号无数据
            if email not in _UNREAD_CACHE:
                _UNREAD_CACHE[email] = {'INBOX': 0}
        finally:
            a.logout()
    _UNREAD_REFRESHING['active'] = False
    _UNREAD_REFRESHING['current'] = ''
    _UNREAD_UPDATED_AT = _time.time()


def start_unread_refresher():
    """启动未读计数后台刷新线程（启动时跑一次，之后每 5 分钟刷新）。"""
    def _loop():
        while True:
            try:
                _refresh_unread_counts()
            except Exception:
                pass
            _time.sleep(300)
    t = threading.Thread(target=_loop)
    t.daemon = True
    t.start()


def _run_background_sync(sync_range):
    """后台同步线程：按最新→最旧顺序，批量拉正文到磁盘。
    sync_range > 0：最近 N 封；sync_range == -1：近一年（全部）。"""
    _SYNC_STATUS['running'] = True
    _SYNC_STATUS['done'] = 0
    _SYNC_STATUS['total'] = 0
    _SYNC_STATUS['last_error'] = ''
    try:
        accounts = storage.list_accounts()
        for acc in accounts:
            _SYNC_STATUS['current_account'] = acc['email']
            a = MailAccount(acc['email'], acc['password'], acc['imap_host'],
                            acc['imap_port'], acc.get('imap_ssl', 1))
            try:
                a.connect()
                # 列出所有文件夹（收件箱 + 子文件夹），每个都同步
                folders = a.list_folders()
                # 排除「所有邮件」类归档文件夹（Gmail All Mail = 收件箱+已发送+已删除等的全集，会大量重复）
                def _skip_folder(f):
                    name = (f.get('name') or '')
                    return ('所有邮件' in name) or ('All Mail' in name)
                folder_names = [f['raw'] for f in folders if not _skip_folder(f)] if folders else ['INBOX']
                for raw_folder in folder_names:
                    folder = raw_folder
                    # 必须 SELECT 后才能 SEARCH（含空格文件夹名自动加引号）
                    try:
                        typ_sel, _ = a.select_folder(folder, readonly=True)
                    except Exception:
                        continue
                    if typ_sel != 'OK':
                        continue
                    # 根据范围搜索邮件序号
                    if sync_range == -1:
                        since = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%d-%b-%Y')
                        typ, data = a.conn.search(None, '(SINCE "%s")' % since)
                    else:
                        typ, data = a.conn.search(None, 'ALL')
                    if typ != 'OK':
                        continue
                    all_ids = [x.decode() for x in (data[0].split() if data and data[0] else [])]
                    # 从最新开始
                    all_ids.reverse()
                    # 限制同步数量（-1 表示全部不截断）
                    target = all_ids if sync_range == -1 else all_ids[:sync_range]
                    # 已有缓存的跳过
                    need = [s for s in target if not storage.load_body(acc['email'], folder, s)]
                    _SYNC_STATUS['total'] += len(target)
                    _SYNC_STATUS['current_folder'] = folder
                    # 分批批量拉
                    for i in range(0, len(need), 20):
                        batch = need[i:i+20]
                        bodies = a.fetch_bodies_batch(folder, batch)
                        for seq, full in bodies.items():
                            try:
                                full['account'] = acc['email']
                                full['account_name'] = acc['display_name'] or acc['email']
                                storage.save_body(acc['email'], folder, seq, full)
                                _SYNC_STATUS['done'] += 1
                            except Exception:
                                continue
            except Exception as e:
                _SYNC_STATUS['last_error'] = '%s: %s' % (acc['email'], str(e))
            finally:
                a.logout()
    finally:
        _SYNC_STATUS['running'] = False
        _SYNC_STATUS['current_account'] = ''
        _SYNC_STATUS['current_folder'] = ''


def start_background_sync(sync_range):
    """启动后台同步（若已在运行则忽略）。sync_range==0 表示关闭。"""
    if sync_range == 0:
        return
    global _sync_thread
    if _SYNC_STATUS.get('running'):
        return
    t = threading.Thread(target=_run_background_sync, args=(sync_range,))
    t.daemon = True
    t.start()


def _auto_incremental_sync():
    """常驻线程：每隔 AUTO_SYNC_INTERVAL 秒，拉各账号最新 N 封的列表头+正文到本地缓存。
    解决「新邮件延迟几小时才显示」和「点邮件要实时连 Gmail 慢到死」两个问题。"""
    LATEST_N = 30
    while True:
        try:
            for acc in storage.list_accounts():
                email = acc['email']
                try:
                    a = MailAccount(email, acc['password'], acc['imap_host'],
                                    acc['imap_port'], acc.get('imap_ssl', 1))
                    a.connect()
                    for folder in ['INBOX']:
                        try:
                            headers = a.fetch_recent(folder, LATEST_N, light=True)
                            if headers:
                                for m in headers:
                                    m['account'] = email
                                    m['account_name'] = acc['display_name'] or email
                                    m['folder'] = folder
                                storage.save_mail_cache(email, folder, headers)
                            # 预取最新正文（未缓存的部分）
                            seqs = [m.get('uid','') for m in headers if m.get('uid','') and not storage.load_body(email, folder, m.get('uid',''))]
                            if seqs:
                                bodies = a.fetch_bodies_batch(folder, seqs)
                                for seq, full in bodies.items():
                                    try:
                                        full['account'] = email
                                        full['account_name'] = acc['display_name'] or email
                                        storage.save_body(email, folder, seq, full)
                                    except Exception:
                                        continue
                        except Exception:
                            continue
                    a.logout()
                except Exception:
                    pass
        except Exception:
            pass
        _time.sleep(AUTO_SYNC_INTERVAL)


def start_auto_sync():
    t = threading.Thread(target=_auto_incremental_sync)
    t.daemon = True
    t.start()


def list_account_folders(account_email):
    """列出某账号的所有文件夹（带缓存，避免每次刷新都连 IMAP）"""
    now = _time.time()
    cached = _FOLDER_CACHE.get(account_email)
    if cached and now - cached[0] < _FOLDER_CACHE_TTL:
        return cached[1]

    acc = storage.get_account(account_email)
    if not acc:
        return []
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        folders = a.list_folders()
    except Exception:
        return []
    finally:
        a.logout()
    _FOLDER_CACHE[account_email] = (now, folders)
    return folders


def fetch_mail_body(account_email, folder, seq_num):
    """按账号+文件夹+序号取单封邮件全文（优先读磁盘缓存，秒开；没有才连 IMAP）"""
    # 1. 先读磁盘缓存
    cached = storage.load_body(account_email, folder, seq_num)
    if cached:
        return cached
    # 2. 缓存未命中，连 IMAP 拉全文
    acc = storage.get_account(account_email)
    if not acc:
        return None
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        m = a.fetch_one_folder(folder, seq_num)
        if m:
            m['account'] = acc['email']
            m['account_name'] = acc['display_name'] or acc['email']
            # 写入磁盘缓存
            try:
                storage.save_body(account_email, folder, seq_num, m)
            except Exception:
                pass
        return m
    finally:
        a.logout()


def mark_read(account_email, folder, seq):
    """在 IMAP 服务器上把某封邮件标记为已读（\\Seen），并同步本地缓存与未读计数。"""
    acc = storage.get_account(account_email)
    if not acc:
        return False
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        try:
            a.select_folder(folder, readonly=False)
        except Exception:
            return False
        typ, data = a.conn.store(seq, '+FLAGS', '(\\Seen)')
        if typ != 'OK':
            return False
        # 同步磁盘缓存：该封邮件 is_read=True（避免刷新后又变回未读）
        storage.update_mail_read_status(account_email, folder, seq, True)
        # 同步未读计数缓存 -1（侧边栏数字立即变化）
        _unread_decrement(account_email, folder)
        return True
    except Exception:
        return False
    finally:
        a.logout()


def _unread_decrement(account_email, folder):
    """把某账号某文件夹的未读计数 -1（最低 0），侧边栏未读数立即变化。"""
    try:
        cnt = _UNREAD_CACHE.get(account_email, {})
        cur = int(cnt.get(folder, 0))
        if cur > 0:
            cnt[folder] = cur - 1
            _UNREAD_CACHE[account_email] = cnt
    except Exception:
        pass


def _unread_increment(account_email, folder):
    """把某账号某文件夹的未读计数 +1，侧边栏未读数立即变化。"""
    try:
        cnt = _UNREAD_CACHE.get(account_email, {})
        cnt[folder] = int(cnt.get(folder, 0)) + 1
        _UNREAD_CACHE[account_email] = cnt
    except Exception:
        pass


def mark_unread(account_email, folder, seq):
    """把某封邮件标记为未读（移除 \\Seen），并同步缓存与未读计数。"""
    acc = storage.get_account(account_email)
    if not acc:
        return False
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        try:
            a.select_folder(folder, readonly=False)
        except Exception:
            return False
        typ, data = a.conn.store(seq, '-FLAGS', '(\\Seen)')
        if typ != 'OK':
            return False
        storage.update_mail_read_status(account_email, folder, seq, False)
        _unread_increment(account_email, folder)
        return True
    except Exception:
        return False
    finally:
        a.logout()


def toggle_flag(account_email, folder, seq, flag):
    """给某封邮件加/删星标（\\Flagged）。flag=True 加星，flag=False 取消星标。"""
    acc = storage.get_account(account_email)
    if not acc:
        return False
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        try:
            a.select_folder(folder, readonly=False)
        except Exception:
            return False
        op = '+FLAGS' if flag else '-FLAGS'
        typ, data = a.conn.store(seq, op, '(\\Flagged)')
        return typ == 'OK'
    except Exception:
        return False
    finally:
        a.logout()


def delete_mail(account_email, folder, seq):
    """删除指定账号某文件夹里的一封邮件（\\Deleted + EXPUNGE）。"""
    acc = storage.get_account(account_email)
    if not acc:
        return False
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        return a.delete_mail(folder, seq)
    except Exception:
        return False
    finally:
        a.logout()


def send_email(account_email, to, subject, body_html, cc='', bcc='', attachments=None):
    """用指定账号发送邮件（SMTP）。attachments: [{name, data(base64), content_type}]"""
    acc = storage.get_account(account_email)
    if not acc:
        return False, '账号不存在'
    smtp_host = acc['smtp_host'] or ''
    smtp_port = acc['smtp_port'] or 465
    if not smtp_host:
        return False, '该账号未配置 SMTP 服务器'

    # 收件人解析
    def split_addrs(s):
        return [x.strip() for x in (s or '').replace(';', ',').split(',') if x.strip()]

    to_list = split_addrs(to)
    cc_list = split_addrs(cc)
    bcc_list = split_addrs(bcc)
    if not to_list and not cc_list and not bcc_list:
        return False, '缺少收件人'

    # 构造邮件（有附件用 mixed，否则 alternative）
    has_att = attachments and len(attachments) > 0
    msg = MIMEMultipart('mixed' if has_att else 'alternative')
    # From 头带显示名（display_name 为空时退回裸邮箱）
    display_name = (acc.get('display_name') or '').strip()
    msg['From'] = formataddr((display_name, account_email)) if display_name else account_email
    msg['To'] = ', '.join(to_list)
    msg['Subject'] = Header(subject or '(无主题)', 'utf-8')
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)
    # 处理正文中的内嵌图片（data:image -> cid 引用 + inline 附件）
    html_body = body_html or ''
    def _extract_inline_images(html_body):
        """把 <img src="data:..."> 提取为 cid 引用的 inline 图片，返回 (新html, [图片列表])"""
        inline_parts = []
        counter = [0]
        def repl(m):
            counter[0] += 1
            dataurl = m.group(1)
            cid = 'inline_%d' % counter[0]
            inline_parts.append({'cid': cid, 'dataurl': dataurl})
            return 'src="cid:%s"' % cid
        new_html = re.sub(r'src="(data:image/[^"]+)"', repl, html_body, flags=re.I)
        return new_html, inline_parts

    html_body, inline_images = _extract_inline_images(html_body)

    msg.attach(MIMEText(html_body or '', 'html', 'utf-8'))

    # 内嵌图片（正文里的插图，用 Content-ID 引用）
    for idx, img in enumerate(inline_images):
        try:
            dataurl = img['dataurl']
            # data:image/png;base64,xxxx
            m = re.match(r'data:(image/[^;]+);base64,(.+)', dataurl, re.S)
            if not m:
                continue
            ctype, b64 = m.group(1), m.group(2)
            raw = base64.b64decode(b64)
            main, sub = ctype.split('/')
            part = MIMEBase(main, sub)
            part.set_payload(raw)
            from email import encoders
            encoders.encode_base64(part)
            part.add_header('Content-ID', '<%s>' % img['cid'])
            part.add_header('Content-Disposition', 'inline')
            msg.attach(part)
        except Exception:
            continue

    # 附件
    if has_att:
        for att in attachments:
            try:
                name = att.get('name', 'attachment')
                data = base64.b64decode(att.get('data', ''))
                ctype = att.get('content_type', 'application/octet-stream')
                main, sub = (ctype.split('/', 1) + [''])[:2]
                part = MIMEBase(main, sub)
                part.set_payload(data)
                try:
                    from email import encoders
                    encoders.encode_base64(part)
                except Exception:
                    pass
                part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', name))
                msg.attach(part)
            except Exception:
                continue

    all_rcpt = to_list + cc_list + bcc_list

    try:
        smtp_ssl = acc.get('smtp_ssl', 1)
        if smtp_ssl == 2:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls()
        elif smtp_ssl == 0:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        server.login(account_email, acc['password'])
        server.sendmail(account_email, all_rcpt, msg.as_string())
        server.quit()

        # 发送成功后，保存到「已发送」文件夹（APPEND）
        try:
            _save_to_sent(acc, msg.as_string())
        except Exception:
            pass  # 保存失败不影响发送结果

        return True, '发送成功'
    except Exception as e:
        return False, '发送失败: %s' % e


def _save_to_sent(acc, raw_msg):
    """把已发送的邮件 APPEND 到账号的「已发送」文件夹。失败静默。"""
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'],
                    acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        folders = a.list_folders()
        sent = None
        # 常见的「已发送」文件夹名（中文/英文/特殊）
        for f in folders:
            name = (f.get('name') or '').lower()
            if name in ('已发送', '已发邮件', 'sent', 'sent items', 'sent mail'):
                sent = f['raw']
                break
        if not sent:
            # 找不到时退回 INBOX 不保存，或尝试常见 raw 名
            return
        a.append_message(sent, raw_msg.encode('utf-8') if isinstance(raw_msg, str) else raw_msg, flags=('Seen',))
    except Exception:
        pass
    finally:
        a.logout()


# ---------- HTTP 处理器 ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def _serve_login(self):
        body = LOGIN_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        # 鉴权：未设密码则放行；已设密码但未登录 → 首页给登录页，API 返回 401
        if AUTH_PASSWORD and not _is_authed(self):
            if path == '/' or path == '/api/login':
                self._serve_login()
            else:
                self._send(401, {'error': '未登录'})
            return
        if path == '/':
            self._serve_index()
        elif path == '/api/inbox':
            account = qs.get('account', [''])[0] or None
            folder = qs.get('folder', ['INBOX'])[0] or 'INBOX'
            force = qs.get('force', ['0'])[0] == '1'
            limit = int(qs.get('limit', ['50'])[0] or 50)
            offset = int(qs.get('offset', ['0'])[0] or 0)
            self._send(200, aggregate_all(account=account, folder=folder, force=force, limit=limit, offset=offset))
        elif path == '/api/accounts':
            self._send(200, {'accounts': _sanitize_accounts(storage.list_accounts())})
        elif path == '/api/folders':
            account = qs.get('account', [''])[0]
            self._send(200, {'folders': list_account_folders(account)})
        elif path == '/api/mail':
            account = qs.get('account', [''])[0]
            folder = qs.get('folder', ['INBOX'])[0]
            seq = qs.get('seq', [''])[0]
            m = fetch_mail_body(account, folder, seq)
            if m:
                self._send(200, m)
            else:
                self._send(404, {'error': 'mail not found'})
        elif path == '/api/favicon':
            domain = qs.get('domain', [''])[0]
            res = fetch_favicon(domain)
            if res:
                data, ctype = res
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {'error': 'favicon not found'})
        elif path == '/api/settings':
            self._send(200, {
                'sync_range': int(storage.get_setting('sync_range', '0') or 0),
                'sync_status': _SYNC_STATUS,
            })
        elif path == '/api/sync/status':
            self._send(200, _SYNC_STATUS)
        elif path == '/api/unread':
            # 返回各账号各文件夹的未读计数（内存缓存，后台逐步累加，边遍历边可见）
            self._send(200, {
                'unread': _UNREAD_CACHE,
                'updated_at': _UNREAD_UPDATED_AT,
                'refreshing': _UNREAD_REFRESHING['active'],
                'current': _UNREAD_REFRESHING['current'],
            })
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b'{}'
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            data = {}

        # 登录接口：无需鉴权，校验密码后发放 session token
        if path == '/api/login':
            if not AUTH_PASSWORD:
                self._send(200, {'ok': True})
                return
            pw = (data.get('password') or '')
            if pw == AUTH_PASSWORD:
                token = secrets.token_hex(16)
                _session_tokens.add(token)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Set-Cookie', '%s=%s; Path=/; HttpOnly; SameSite=Lax' % (AUTH_COOKIE, token))
                self.send_header('Content-Length', '11')
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:
                self._send(401, {'ok': False, 'error': '密码错误'})
            return

        # 鉴权：未设密码则放行；已设密码但未登录 → 拒绝
        if AUTH_PASSWORD and not _is_authed(self):
            self._send(401, {'error': '未登录'})
            return

        if path == '/api/account':
            # 添加账号
            email = (data.get('email') or '').strip()
            password = data.get('password') or ''
            # 自动匹配服务器预设
            preset = config.lookup_provider(email)
            imap_host = data.get('imap_host') or (preset[0] if preset else '')
            imap_port = int(data.get('imap_port') or (preset[1] if preset else 993))
            imap_ssl = int(data.get('imap_ssl') or 1)
            smtp_host = data.get('smtp_host') or (preset[2] if preset else '')
            smtp_port = int(data.get('smtp_port') or (preset[3] if preset else 465))
            smtp_ssl = int(data.get('smtp_ssl') or 1)
            display_name = data.get('display_name') or ''
            if not smtp_host and preset:
                smtp_host = preset[2]

            if not email or not password or not imap_host:
                self._send(400, {'error': '请填写邮箱、密码（授权码）、收件服务器和发件服务器'})
                return

            # 先测试 IMAP 连接
            try:
                a = MailAccount(email, password, imap_host, imap_port, imap_ssl)
                a.connect()
                a.logout()
            except Exception as e:
                self._send(400, {'error': 'IMAP 连接验证失败: %s' % e})
                return

            storage.add_account(email, password, imap_host, imap_port, imap_ssl,
                                smtp_host, smtp_port, smtp_ssl, display_name)
            self._send(200, {'ok': True, 'accounts': _sanitize_accounts(storage.list_accounts())})

        elif path == '/api/account/delete':
            email = (data.get('email') or '').strip()
            storage.delete_account(email)
            self._send(200, {'ok': True, 'accounts': _sanitize_accounts(storage.list_accounts())})

        elif path == '/api/send':
            account = (data.get('account') or '').strip()
            to = data.get('to') or ''
            subject = data.get('subject') or ''
            body = data.get('body') or ''
            cc = data.get('cc') or ''
            bcc = data.get('bcc') or ''
            attachments = data.get('attachments') or []
            ok, msg = send_email(account, to, subject, body, cc, bcc, attachments)
            self._send(200 if ok else 400, {'ok': ok, 'message': msg})

        elif path == '/api/settings':
            sync_range = int(data.get('sync_range') or 0)
            storage.set_setting('sync_range', sync_range)
            # 启动后台同步
            start_background_sync(sync_range)
            self._send(200, {'ok': True, 'sync_range': sync_range, 'sync_status': _SYNC_STATUS})

        elif path == '/api/mark_read':
            account = (data.get('account') or '').strip()
            folder = data.get('folder') or 'INBOX'
            seq = str(data.get('seq') or '')
            ok = mark_read(account, folder, seq)
            self._send(200, {'ok': ok})

        elif path == '/api/mark_unread':
            account = (data.get('account') or '').strip()
            folder = data.get('folder') or 'INBOX'
            seq = str(data.get('seq') or '')
            ok = mark_unread(account, folder, seq)
            self._send(200, {'ok': ok})

        elif path == '/api/mail/delete':
            account = (data.get('account') or '').strip()
            folder = data.get('folder') or 'INBOX'
            seq = str(data.get('seq') or '')
            ok = delete_mail(account, folder, seq)
            self._send(200, {'ok': ok})

        elif path == '/api/mail/flag':
            account = (data.get('account') or '').strip()
            folder = data.get('folder') or 'INBOX'
            seq = str(data.get('seq') or '')
            flag = bool(data.get('flag'))
            ok = toggle_flag(account, folder, seq, flag)
            self._send(200, {'ok': ok})

        else:
            self._send(404, {'error': 'not found'})

    def _serve_index(self):
        html = _get_index_html()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(html)


# 前端页面：从独立文件 frontend/index.html 读取（启动时缓存到内存，零依赖）
_INDEX_HTML_CACHE = None


def _get_index_html():
    """读取前端页面，首次读取后缓存到内存。"""
    global _INDEX_HTML_CACHE
    if _INDEX_HTML_CACHE is None:
        html_path = os.path.join(os.path.dirname(__file__), 'frontend', 'index.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            _INDEX_HTML_CACHE = f.read().encode('utf-8')
    return _INDEX_HTML_CACHE


def _sanitize_accounts(accounts):
    """API 返回账号列表时脱敏：去掉 password 等敏感字段（授权码绝不能下发到前端）。"""
    safe = []
    for a in accounts:
        a = dict(a)
        a.pop('password', None)
        safe.append(a)
    return safe


# ---------- 前端页面 ----------



def main():
    storage.init_db()
    start_unread_refresher()
    start_auto_sync()
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print('MailHub 已启动： http://127.0.0.1:%d' % PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()
