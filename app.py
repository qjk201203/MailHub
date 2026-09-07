# -*- coding: utf-8 -*-
"""
MailHub - 多邮箱聚合客户端
纯 Python 标准库实现，零外部 pip 依赖。

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
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import Header
from email.utils import formataddr
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    from . import config
    from . import storage
    from .imap_client import MailAccount
except ImportError:
    import config
    import storage
    from imap_client import MailAccount

PORT = 20111

# 自动增量同步间隔（秒）：定期拉最新邮件到本地缓存
AUTO_SYNC_INTERVAL = 120

# 登录鉴权：设置环境变量 MAILHUB_PASSWORD 后启用（留空则无鉴权）
AUTH_PASSWORD = os.environ.get('MAILHUB_PASSWORD', '').strip()
AUTH_COOKIE = 'mailhub_token'
_session_tokens = set()

# 全局线程池：多账号并发请求加速
_EXECUTOR = ThreadPoolExecutor(max_workers=8)


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
:root{--accent:#0b57d0;--bg:#f8fafd;--surface:#ffffff;--border:#e2e8f0;--text:#1f1f1f;--muted:#5e5e5e}
body{font-family:-apple-system,BlinkMacSystemFont,'Google Sans',Roboto,'Segoe UI','PingFang SC',sans-serif;background:var(--bg);display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:var(--surface);padding:36px 32px;border-radius:24px;box-shadow:0 4px 24px rgba(0,0,0,.08);width:340px;text-align:center;border:1px solid var(--border)}
.logo-icon{width:44px;height:44px;margin-bottom:12px}
h1{font-size:22px;font-weight:500;margin:0 0 8px;color:var(--text)}
p.sub{font-size:13px;color:var(--muted);margin:0 0 24px}
input{width:100%;box-sizing:border-box;padding:12px 14px;border:1px solid var(--border);border-radius:12px;font-size:14px;margin-bottom:14px;outline:none;transition:border-color .2s}
input:focus{border-color:var(--accent)}
button{width:100%;padding:12px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-size:14px;font-weight:500;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.9}
.err{color:#ba1a1a;font-size:13px;text-align:center;min-height:18px;margin-top:10px}
</style></head><body>
<div class="card">
  <svg class="logo-icon" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#0b57d0"/><path fill="#ffffff" d="M6 8.5 12 12.5 18 8.5l-6 3.5-6-3.5zM6 9.5v6c0 .5.4 1 1 1h10c.6 0 1-.5 1-1v-6l-6 4-6-4z"/></svg>
  <h1>MailHub</h1>
  <p class="sub">多邮箱聚合 Webmail 客户端</p>
  <input id="pw" type="password" placeholder="输入访问密码" autofocus>
  <button onclick="doLogin()">登 录</button>
  <div class="err" id="err"></div>
</div>
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

PROXY = os.environ.get('MAILHUB_PROXY', '').strip()
_FAVICON_CACHE = {}


def fetch_favicon(domain):
    """拉取指定域名的 favicon（支持代理与国内免代理双通道）"""
    if not domain:
        return None
    domain = domain.lower().strip()
    if domain in _FAVICON_CACHE:
        return _FAVICON_CACHE[domain]
    
    proxy = storage.get_setting('proxy_url', os.environ.get('MAILHUB_PROXY', '')).strip()
    
    # 优先走 Google Favicon，国内/海外均尝试备用通道
    urls_to_try = [
        'https://www.google.com/s2/favicons?domain=%s&sz=64' % domain,
        'https://icons.duckduckgo.com/ip3/%s.ico' % domain,
        'https://api.faviconkit.com/%s/64' % domain,
        'https://%s/favicon.ico' % domain
    ]
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    
    if proxy:
        proxy_handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
        opener = urllib.request.build_opener(proxy_handler, https_handler)
    else:
        opener = urllib.request.build_opener(https_handler)
    
    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with opener.open(req, timeout=3) as resp:
                data = resp.read()
                if data and len(data) > 30: # 过滤空响应
                    ctype = resp.headers.get('Content-Type', 'image/png')
                    _FAVICON_CACHE[domain] = (data, ctype)
                    return data, ctype
        except Exception:
            continue
            
    return None


def strip_html(s):
    """剥离 HTML 标签得到预览文本"""
    if not s:
        return ''
    s = re.sub(r'<style[^>]*>.*?</style>', ' ', s, flags=re.S | re.I)
    s = re.sub(r'<script[^>]*>.*?</script>', ' ', s, flags=re.S | re.I)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'&nbsp;', ' ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def _fetch_account_mails(acc, folder='INBOX', limit=50):
    """单账号拉取邮件列表"""
    a = MailAccount(acc['email'], acc['password'],
                    acc['imap_host'], acc['imap_port'], acc.get('imap_ssl', 1))
    a.connect()
    mails = a.fetch_recent(folder, limit, light=True)
    for m in mails:
        m['account'] = acc['email']
        m['account_name'] = acc['display_name'] or acc['email']
        m['folder'] = folder
        m['preview'] = (m.get('text') or strip_html(m.get('html', '')) or '')[:140]
        m.pop('text', None)
        m.pop('html', None)
    a.logout()
    return mails


def _prefetch_bodies(account_email, folder, mids):
    """后台批量预取邮件全文"""
    acc = storage.get_account(account_email)
    if not acc:
        return
    to_fetch = [m.get('uid', '') for m in mids
                if m.get('uid', '') and not storage.load_body(account_email, folder, m.get('uid', ''))]
    if not to_fetch:
        return
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
    """多账号并发聚合取信（支持全量加载与深度翻页）"""
    accounts = storage.list_accounts()
    if account:
        accounts = [a for a in accounts if a['email'] == account]

    flat = []
    need_fetch_accounts = []

    # 优先从本地数据库载入全量缓存，按日期倒序
    for acc in accounts:
        cached = storage.load_mail_cache(acc['email'], folder, limit=5000)
        if cached and not force:
            flat.extend(cached)
        else:
            need_fetch_accounts.append(acc)

    # 若无缓存或触发了强制刷新，使用并发线程池从远端拉取最新大批量索引
    if need_fetch_accounts:
        futures = {
            _EXECUTOR.submit(_fetch_account_mails, acc, folder, 5000): acc
            for acc in need_fetch_accounts
        }
        for fut in as_completed(futures):
            acc = futures[fut]
            try:
                mails = fut.result()
                if mails:
                    storage.save_mail_cache(acc['email'], folder, mails)
                flat.extend(mails)
            except Exception:
                pass

    # 全局按邮件日期严格倒序排列（用时间戳排序，兼容旧缓存的原始 Date 字符串）
    from imap_client import _parse_date as _pd
    def _ts(m):
        d = m.get('date', '')
        # 如果 already 格式化了 (YYYY-MM-DD HH:MM:SS)
        if len(d) == 19 and d[4]=='-':
             try:
                return datetime.datetime.strptime(d, '%Y-%m-%d %H:%M:%S').timestamp()
             except:
                pass
        # 尝试原始字符串
        pd_res = _pd(d)
        if len(pd_res) == 19 and pd_res[4]=='-':
             try:
                return datetime.datetime.strptime(pd_res, '%Y-%m-%d %H:%M:%S').timestamp()
             except:
                pass
        return 0

    flat.sort(key=_ts, reverse=True)
    total_count = len(flat)
    paged = flat[offset:offset + limit]
    result = {'all_mails': paged, 'total': total_count}

    # 后台异步预拉取当前可视页的正文
    for acc in accounts:
        mids = [m for m in paged if m.get('account') == acc['email']]
        if mids:
            _EXECUTOR.submit(_prefetch_bodies, acc['email'], folder, mids)

    return result


_FOLDER_CACHE = {}
_FOLDER_CACHE_TTL = 600

_SYNC_STATUS = {'running': False, 'done': 0, 'total': 0, 'current_account': '', 'current_folder': '', 'last_error': ''}

_UNREAD_CACHE = {}
_UNREAD_UPDATED_AT = 0
_UNREAD_REFRESHING = {'active': False, 'current': ''}


def _refresh_unread_single(acc):
    """单个账号统计未读"""
    email = acc['email']
    a = MailAccount(email, acc['password'], acc['imap_host'],
                    acc['imap_port'], acc.get('imap_ssl', 1))
    cnt = dict(_UNREAD_CACHE.get(email, {}))
    try:
        a.connect()
        folders = a.list_folders()
        for f in folders:
            name = f.get('name') or ''
            if ('所有邮件' in name) or ('All Mail' in name) or ('[Google Mail]' in name):
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
        if 'INBOX' not in cnt:
            cnt['INBOX'] = 0
        return email, cnt
    except Exception:
        if email not in _UNREAD_CACHE:
            cnt['INBOX'] = 0
        return email, cnt
    finally:
        a.logout()


def _refresh_unread_counts():
    """并发刷新所有账号的未读数"""
    global _UNREAD_UPDATED_AT
    _UNREAD_REFRESHING['active'] = True
    accounts = storage.list_accounts()
    futures = [_EXECUTOR.submit(_refresh_unread_single, acc) for acc in accounts]
    for fut in as_completed(futures):
        try:
            email, cnt = fut.result()
            _UNREAD_CACHE[email] = cnt
        except Exception:
            pass
    _UNREAD_REFRESHING['active'] = False
    _UNREAD_UPDATED_AT = _time.time()


def start_unread_refresher():
    def _loop():
        while True:
            try:
                _refresh_unread_counts()
            except Exception:
                pass
            _time.sleep(180)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


def _run_background_sync(sync_range):
    """后台批量正文预取"""
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
                folders = a.list_folders()
                folder_names = [f['raw'] for f in folders if not (('所有邮件' in (f.get('name') or '')) or ('All Mail' in (f.get('name') or '')))] if folders else ['INBOX']
                for raw_folder in folder_names:
                    folder = raw_folder
                    try:
                        typ_sel, _ = a.select_folder(folder, readonly=True)
                    except Exception:
                        continue
                    if typ_sel != 'OK':
                        continue
                    if sync_range == -1:
                        since = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime('%d-%b-%Y')
                        typ, data = a.conn.search(None, '(SINCE "%s")' % since)
                    else:
                        typ, data = a.conn.search(None, 'ALL')
                    if typ != 'OK':
                        continue
                    all_ids = [x.decode() for x in (data[0].split() if data and data[0] else [])]
                    all_ids.reverse()
                    target = all_ids if sync_range == -1 else all_ids[:sync_range]
                    need = [s for s in target if not storage.load_body(acc['email'], folder, s)]
                    _SYNC_STATUS['total'] += len(target)
                    _SYNC_STATUS['current_folder'] = folder
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
    if sync_range == 0 or _SYNC_STATUS.get('running'):
        return
    t = threading.Thread(target=_run_background_sync, args=(sync_range,), daemon=True)
    t.start()


def _auto_incremental_sync():
    """定期抓取最新邮件"""
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
    t = threading.Thread(target=_auto_incremental_sync, daemon=True)
    t.start()


def list_account_folders(account_email):
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
    cached = storage.load_body(account_email, folder, seq_num)
    if cached:
        return cached
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
            try:
                storage.save_body(account_email, folder, seq_num, m)
            except Exception:
                pass
        return m
    finally:
        a.logout()


def mark_read(account_email, folder, seq):
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
        typ, data = a.conn.store(str(seq), '+FLAGS', '(\Seen)')
        if typ != 'OK':
            return False
        storage.update_mail_read_status(account_email, folder, seq, True)
        _unread_decrement(account_email, folder)
        return True
    except Exception:
        return False
    finally:
        a.logout()


def _unread_decrement(account_email, folder):
    try:
        cnt = _UNREAD_CACHE.get(account_email, {})
        cur = int(cnt.get(folder, 0))
        if cur > 0:
            cnt[folder] = cur - 1
            _UNREAD_CACHE[account_email] = cnt
    except Exception:
        pass


def _unread_increment(account_email, folder):
    try:
        cnt = _UNREAD_CACHE.get(account_email, {})
        cnt[folder] = int(cnt.get(folder, 0)) + 1
        _UNREAD_CACHE[account_email] = cnt
    except Exception:
        pass


def mark_unread(account_email, folder, seq):
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
        typ, data = a.conn.store(str(seq), '-FLAGS', '(\Seen)')
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
        typ, data = a.conn.store(str(seq), op, '(\Flagged)')
        return typ == 'OK'
    except Exception:
        return False
    finally:
        a.logout()


def delete_mail(account_email, folder, seq):
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


def _friendly_imap_error(e):
    s = str(e).lower()
    if 'authentication' in s or 'login' in s or 'auth' in s or 'pass' in s or 'credentials' in s:
        return '登录失败：邮箱或授权码错误（请用授权码/应用专用密码，而非登录密码）'
    if 'refused' in s or 'connection' in s and 'timeout' in s:
        return '连接超时或服务器无法访问：请检查 IMAP 服务器地址和端口'
    if 'timeout' in s or 'timed out' in s:
        return '连接超时：请检查网络或服务器地址'
    if 'ssl' in s or 'tls' in s or 'certificate' in s or 'handshake' in s:
        return '加密方式错误：请检查选择了正确的 SSL/TLS 加密方式'
    if 'unsafe' in s:
        return '服务器拒绝了连接（Unsafe Login）：网易邮箱需要客户端授权码'
    if 'name or service not known' in s or 'getaddrinfo' in s or 'unknown host' in s:
        return '服务器地址错误：无法解析域名'
    return 'IMAP 连接失败：%s' % str(e)


def send_email(account_email, to, subject, body_html, cc='', bcc='', attachments=None):
    """用指定账号通过 SMTP 发送邮件"""
    acc = storage.get_account(account_email)
    if not acc:
        return False, '账号不存在'
    smtp_host = acc['smtp_host'] or ''
    smtp_port = acc['smtp_port'] or 465
    if not smtp_host:
        return False, '该账号未配置 SMTP 服务器'

    def split_addrs(s):
        return [x.strip() for x in (s or '').replace(';', ',').split(',') if x.strip()]

    to_list = split_addrs(to)
    cc_list = split_addrs(cc)
    bcc_list = split_addrs(bcc)
    if not to_list and not cc_list and not bcc_list:
        return False, '缺少收件人'

    has_att = attachments and len(attachments) > 0
    msg = MIMEMultipart('mixed' if has_att else 'alternative')
    display_name = (acc.get('display_name') or '').strip()
    msg['From'] = formataddr((display_name, account_email)) if display_name else account_email
    msg['To'] = ', '.join(to_list)
    msg['Subject'] = Header(subject or '(无主题)', 'utf-8')
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)

    html_body = body_html or ''

    def _extract_inline_images(content_html):
        inline_parts = []
        counter = [0]
        def repl(m):
            counter[0] += 1
            dataurl = m.group(1)
            cid = 'inline_%d' % counter[0]
            inline_parts.append({'cid': cid, 'dataurl': dataurl})
            return 'src="cid:%s"' % cid
        new_html = re.sub(r'src="(data:image/[^"]+)"', repl, content_html, flags=re.I)
        return new_html, inline_parts

    html_body, inline_images = _extract_inline_images(html_body)
    msg.attach(MIMEText(html_body or '', 'html', 'utf-8'))

    # 内嵌图片 (CID)
    for img in inline_images:
        try:
            dataurl = img['dataurl']
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

    # 普通附件
    if has_att:
        for att in attachments:
            try:
                name = att.get('name', 'attachment')
                data = base64.b64decode(att.get('data', ''))
                ctype = att.get('content_type', 'application/octet-stream')
                main, sub = (ctype.split('/', 1) + [''])[:2]
                part = MIMEBase(main, sub)
                part.set_payload(data)
                from email import encoders
                encoders.encode_base64(part)
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

        try:
            _save_to_sent(acc, msg.as_string())
        except Exception:
            pass

        return True, '发送成功'
    except Exception as e:
        return False, '发送失败: %s' % e


def _save_to_sent(acc, raw_msg):
    """保存到已发送"""
    a = MailAccount(acc['email'], acc['password'], acc['imap_host'],
                    acc['imap_port'], acc.get('imap_ssl', 1))
    try:
        a.connect()
        folders = a.list_folders()
        sent = None
        for f in folders:
            name = (f.get('name') or '').lower()
            if name in ('已发送', '已发邮件', 'sent', 'sent items', 'sent mail'):
                sent = f['raw']
                break
        if sent:
            a.append_message(sent, raw_msg.encode('utf-8') if isinstance(raw_msg, str) else raw_msg, flags=('Seen',))
    except Exception:
        pass
    finally:
        a.logout()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
                'proxy_url': storage.get_setting('proxy_url', os.environ.get('MAILHUB_PROXY', '')),
                'sync_status': _SYNC_STATUS,
            })
        elif path == '/api/sync/status':
            self._send(200, _SYNC_STATUS)
        elif path == '/api/health':
            self._send(200, {'ok': True})
        elif path == '/api/unread':
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

        if AUTH_PASSWORD and not _is_authed(self):
            self._send(401, {'error': '未登录'})
            return

        if path == '/api/account':
            email = (data.get('email') or '').strip()
            password = data.get('password') or ''
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

            try:
                a = MailAccount(email, password, imap_host, imap_port, imap_ssl)
                a.connect()
                a.logout()
            except Exception as e:
                self._send(400, {'error': _friendly_imap_error(e)})
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
            if 'proxy_url' in data:
                storage.set_setting('proxy_url', (data.get('proxy_url') or '').strip())
            sync_range = int(data.get('sync_range') or 0)
            if 'sync_range' in data:
                storage.set_setting('sync_range', sync_range)
                start_background_sync(sync_range)
            self._send(200, {
                'ok': True,
                'sync_range': int(storage.get_setting('sync_range', '0') or 0),
                'proxy_url': storage.get_setting('proxy_url', ''),
                'sync_status': _SYNC_STATUS
            })

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


def _get_index_html():
    # 动态实时读取前端 HTML，无需每次重启服务
    html_path = os.path.join(os.path.dirname(__file__), 'frontend', 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        return f.read().encode('utf-8')


def _sanitize_accounts(accounts):
    safe = []
    for a in accounts:
        a = dict(a)
        a.pop('password', None)
        safe.append(a)
    return safe


def main():
    storage.init_db()
    start_unread_refresher()
    start_auto_sync()
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print('MailHub 已启动： http://127.0.0.1:%d' % PORT)
    server.serve_forever()


if __name__ == '__main__':
    main()
