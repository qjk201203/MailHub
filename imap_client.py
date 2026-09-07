import os
import re
import email
import imaplib
import socket
import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
try:
    from . import config
except ImportError:
    import config

CRLF = chr(13) + chr(10)


def _parse_date(raw):
    """把邮件 Date 头解析成标准可排序格式 YYYY-MM-DD HH:MM:SS（本地时区）。
    解析失败时返回原始字符串（兜底）。"""
    if not raw:
        return ''
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return raw
        # 归一化到本地时区（naive），保证跨时区排序正确
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return raw

def _imap_utf7_decode(s):
    """IMAP Modified UTF-7 解码"""
    if not s:
        return ''
    try:
        res = []
        parts = s.split('&')
        res.append(parts[0])
        for p in parts[1:]:
            if '-' in p:
                b64, rest = p.split('-', 1)
                if b64 == '':
                    res.append('&')
                else:
                    b64_pad = b64.replace(',', '/')
                    pad = len(b64_pad) % 4
                    if pad:
                        b64_pad += '=' * (4 - pad)
                    import base64
                    try:
                        raw = base64.b64decode(b64_pad)
                        res.append(raw.decode('utf-16-be'))
                    except Exception:
                        res.append('&' + b64 + '-')
                res.append(rest)
            else:
                res.append('&' + p)
        return ''.join(res)
    except Exception:
        return s

def _decode_hdr(v):
    if not v:
        return ''
    try:
        parts = decode_header(v)
        res = []
        for text, enc in parts:
            if isinstance(text, bytes):
                enc = enc or 'utf-8'
                try:
                    res.append(text.decode(enc, 'replace'))
                except Exception:
                    res.append(text.decode('utf-8', 'replace'))
            else:
                res.append(str(text))
        return ''.join(res)
    except Exception:
        return str(v)

def _friendly_imap_error(e):
    msg = str(e)
    if 'Unsafe Login' in msg:
        return '网易邮箱拒绝登录，请使用专属客户端授权密码。'
    if 'authentication failed' in msg.lower() or 'login failed' in msg.lower():
        return '邮箱账号或密码/授权码错误。'
    return msg

class _ProxyIMAP4(imaplib.IMAP4):
    _proxy = None
    def _create_socket(self, timeout=None):
        if not self._proxy:
            return super()._create_socket(timeout)
        import urllib.parse
        p = urllib.parse.urlparse(self._proxy)
        ph = p.hostname or '127.0.0.1'
        pp = p.port or 7890
        sock = socket.create_connection((ph, pp), timeout)
        req = 'CONNECT ' + str(self.host) + ':' + str(self.port) + ' HTTP/1.1' + CRLF + 'Host: ' + str(self.host) + ':' + str(self.port) + CRLF + CRLF
        sock.sendall(req.encode('utf-8'))
        resp = b''
        while (CRLF + CRLF).encode() not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        if not resp.startswith(b'HTTP/1.1 200') and not resp.startswith(b'HTTP/1.0 200'):
            sock.close()
            raise Exception('代理隧道建立失败: %s' % resp[:64])
        return sock

class _ProxyIMAP4_SSL(imaplib.IMAP4_SSL):
    _proxy = None
    def _create_socket(self, timeout=None):
        if not self._proxy:
            return super()._create_socket(timeout)
        import urllib.parse, ssl
        p = urllib.parse.urlparse(self._proxy)
        ph = p.hostname or '127.0.0.1'
        pp = p.port or 7890
        sock = socket.create_connection((ph, pp), timeout)
        req = 'CONNECT ' + str(self.host) + ':' + str(self.port) + ' HTTP/1.1' + CRLF + 'Host: ' + str(self.host) + ':' + str(self.port) + CRLF + CRLF
        sock.sendall(req.encode('utf-8'))
        resp = b''
        while (CRLF + CRLF).encode() not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        if not resp.startswith(b'HTTP/1.1 200') and not resp.startswith(b'HTTP/1.0 200'):
            sock.close()
            raise Exception('代理隧道建立失败: %s' % resp[:64])
        ctx = ssl.create_default_context()
        return ctx.wrap_socket(sock, server_hostname=self.host)

class MailAccount:
    def __init__(self, email_addr, password, imap_host, imap_port=993, imap_ssl=1):
        self.email_addr = email_addr
        self.password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_ssl = imap_ssl
        self.conn = None

    def _send_id_command(self):
        try:
            tag = self.conn._new_tag()
            cmd = tag + b' ID ("name" "Thunderbird" "version" "115.0" "vendor" "Mozilla" "support-url" "https://support.mozilla.org")' + CRLF.encode()
            self.conn.send(cmd)
            self.conn._get_response()
            return True
        except Exception:
            return False

    def connect(self):
        proxy = ''
        # 仅对海外或需要翻墙的邮箱（如 gmail, googlemail, yahoo 等）使用代理，国内邮箱（163/qq/126/yeah/aliyun等）强制直连
        is_domestic = any(dom in self.email_addr.lower() for dom in ('163.com', '126.com', 'yeah.net', 'qq.com', 'foxmail.com', 'aliyun.com', 'sina.com', 'sohu.com'))
        if not is_domestic:
            proxy = os.environ.get('MAILHUB_PROXY', '').strip()
            if not proxy:
                try:
                    from . import storage
                    proxy = storage.get_setting('proxy_url', '').strip()
                except Exception:
                    pass
        if self.imap_ssl == 1:
            if proxy:
                cls = type('ProxySSL', (_ProxyIMAP4_SSL,), {'_proxy': proxy})
                self.conn = cls(self.imap_host, self.imap_port)
            else:
                self.conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        elif self.imap_ssl == 2:
            if proxy:
                cls = type('ProxyPlain', (_ProxyIMAP4,), {'_proxy': proxy})
                self.conn = cls(self.imap_host, self.imap_port)
            else:
                self.conn = imaplib.IMAP4(self.imap_host, self.imap_port)
            self.conn.starttls()
        else:
            if proxy:
                cls = type('ProxyPlain', (_ProxyIMAP4,), {'_proxy': proxy})
                self.conn = cls(self.imap_host, self.imap_port)
            else:
                self.conn = imaplib.IMAP4(self.imap_host, self.imap_port)
        
        if any(x in self.email_addr for x in ('163.com', '126.com', 'yeah.net')):
            try:
                imaplib.Commands['ID'] = ('AUTH', 'NONAUTH', 'SELECTED')
                self.conn._command('ID', '("name" "Thunderbird" "version" "115.0" "vendor" "Mozilla")')
            except Exception:
                pass

        typ, dat = self.conn.login(self.email_addr, self.password)
        if typ != 'OK':
            raise Exception('IMAP 登录失败: %s' % dat)
        self._send_id_command()
        return True

    def _quote_folder(self, folder):
        f = folder or ''
        if len(f) >= 2 and f[0] == '"' and f[-1] == '"':
            return f
        if any(ch in f for ch in ' [],') or any(ord(c) > 127 for c in f):
            return '"' + f.replace('"', '\"') + '"'
        return f

    def select_folder(self, folder, readonly=True):
        return self.conn.select(self._quote_folder(folder), readonly=readonly)

    def list_folders(self):
        """列出邮箱文件夹（兼容 Gmail / 163 / QQ / Outlook / 标准 IMAP）"""
        if not self.conn:
            return []
        folders = []
        seen_raws = set()
        for ref, pattern in [('""', '*'), ('"[Gmail]"', '*'), ('"[Google Mail]"', '*')]:
            try:
                typ, dat = self.conn.list(ref, pattern)
                if typ != 'OK' or not dat:
                    continue
                for line in dat:
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode('utf-8', 'ignore')
                    m = re.search(r'\(([^)]*)\)\s+"?([^"]*)"?\s+(.+)$', line.strip())
                    if m:
                        flags_str, sep, raw_name = m.groups()
                        raw_name = raw_name.strip()
                        if raw_name.startswith('"') and raw_name.endswith('"'):
                            raw_name = raw_name[1:-1]
                    else:
                        m2 = re.search(r'"([^"]*)"$', line.strip())
                        if m2:
                            raw_name = m2.group(1).replace('"', '')
                        else:
                            parts = line.strip().split()
                            raw_name = parts[-1] if parts else ''
                    if raw_name and raw_name not in seen_raws:
                        if r'\Noselect' in line:
                            continue
                        seen_raws.add(raw_name)
                        decoded = _imap_utf7_decode(raw_name)
                        name_display = decoded
                        if raw_name in ('[Gmail]/All Mail', '[Google Mail]/All Mail'):
                            name_display = '所有邮件'
                        elif raw_name in ('[Gmail]/Sent Mail', '[Google Mail]/Sent Mail'):
                            name_display = '已发送'
                        elif raw_name in ('[Gmail]/Drafts', '[Google Mail]/Drafts'):
                            name_display = '草稿箱'
                        elif raw_name in ('[Gmail]/Spam', '[Google Mail]/Spam'):
                            name_display = '垃圾邮件'
                        elif raw_name in ('[Gmail]/Trash', '[Google Mail]/Trash'):
                            name_display = '已删除'
                        elif raw_name in ('[Gmail]/Starred', '[Google Mail]/Starred'):
                            name_display = '星标邮件'
                        folders.append({'name': name_display, 'raw': raw_name})
            except Exception:
                continue
        return folders

    def fetch_recent(self, folder='INBOX', limit=5000, light=True):
        """获取指定文件夹邮件（无时间限制，默认拉取全部/大批量索引）"""
        if not self.conn:
            return []
        try:
            typ, dat = self.select_folder(folder, readonly=True)
            if typ != 'OK':
                return []
        except Exception:
            return []

        typ, data = self.conn.search(None, 'ALL')
        if typ != 'OK' or not data or not data[0]:
            return []
        ids = data[0].split()
        if limit and limit > 0:
            ids = ids[-limit:] if ids else []
        if not ids:
            return []

        unseen_set = set()
        flagged_set = set()
        try:
            typ_u, data_u = self.conn.search(None, 'UNSEEN')
            if typ_u == 'OK' and data_u and data_u[0]:
                unseen_set = set(x.decode() for x in data_u[0].split())
        except Exception:
            pass

        try:
            typ_f, data_f = self.conn.search(None, 'FLAGGED')
            if typ_f == 'OK' and data_f and data_f[0]:
                flagged_set = set(x.decode() for x in data_f[0].split())
        except Exception:
            pass

        fetch_what = '(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])' if light else '(RFC822)'
        mails = []
        idstr = b','.join(ids)
        try:
            typ, msg_data = self.conn.fetch(idstr, fetch_what)
            if typ == 'OK' and msg_data:
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                        raw = item[1]
                        seq = b''
                        if isinstance(item[0], (bytes, bytearray)):
                            m = re.match(rb'(\d+)', item[0])
                            if m:
                                seq = m.group(1)
                        try:
                            msg = email.message_from_bytes(bytes(raw))
                            parsed = self._parse(msg, seq, header_only=light)
                            if parsed:
                                seq_str = seq.decode() if isinstance(seq, bytes) else str(seq)
                                parsed['is_read'] = (seq_str not in unseen_set)
                                parsed['flagged'] = (seq_str in flagged_set)
                                mails.append(parsed)
                        except Exception:
                            continue
        except Exception:
            pass

        if not mails and ids:
            for i in ids:
                try:
                    typ, md = self.conn.fetch(i, fetch_what)
                    if typ != 'OK' or not md or not md[0]:
                        continue
                    raw = md[0][1]
                    msg = email.message_from_bytes(raw)
                    parsed = self._parse(msg, i, header_only=light)
                    if parsed:
                        seq_str = i.decode() if isinstance(i, bytes) else str(i)
                        parsed['is_read'] = (seq_str not in unseen_set)
                        parsed['flagged'] = (seq_str in flagged_set)
                        mails.append(parsed)
                except Exception:
                    continue
        return mails

    def fetch_one_folder(self, folder, seq_num):
        if not self.conn:
            return None
        try:
            typ, dat = self.select_folder(folder, readonly=True)
            if typ != 'OK':
                return None
            typ, msg_data = self.conn.fetch(str(seq_num), '(RFC822)')
            if typ != 'OK' or not msg_data or not msg_data[0]:
                return None
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            return self._parse(msg, seq_num, header_only=False)
        except Exception:
            return None

    def fetch_bodies_batch(self, folder, seq_list):
        if not self.conn or not seq_list:
            return {}
        try:
            typ, dat = self.select_folder(folder, readonly=True)
            if typ != 'OK':
                return {}
            seq_list = [str(s) for s in seq_list]
            idstr = ','.join(seq_list)
            result = {}
            typ, msg_data = self.conn.fetch(idstr, '(RFC822)')
            if typ != 'OK':
                return result
            seq = None
            for item in msg_data:
                if isinstance(item, tuple):
                    if isinstance(item[0], (bytes, bytearray)):
                        m = re.match(rb'(\d+)', item[0])
                        if m:
                            seq = m.group(1).decode()
                    raw = item[1]
                    if seq is not None and isinstance(raw, (bytes, bytearray)):
                        try:
                            msg = email.message_from_bytes(bytes(raw))
                            result[seq] = self._parse(msg, seq.encode(), header_only=False)
                        except Exception:
                            pass
            return result
        except Exception:
            return {}

    def logout(self):
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _parse(self, msg, seq_num, header_only=True):
        out = {}
        seq_str = seq_num.decode() if isinstance(seq_num, bytes) else str(seq_num)
        out['uid'] = seq_str
        out['seq'] = seq_str
        out['from'] = _decode_hdr(msg.get('From', ''))
        out['to'] = _decode_hdr(msg.get('To', ''))
        out['cc'] = _decode_hdr(msg.get('Cc', ''))
        out['subject'] = _decode_hdr(msg.get('Subject', ''))
        # 日期统一解析为可排序的标准格式（YYYY-MM-DD HH:MM:SS），并归一化到本地时区
        raw_date = msg.get('Date', '')
        out['date'] = _parse_date(raw_date)
        out['message_id'] = msg.get('Message-ID', '')
        out['in_reply_to'] = msg.get('In-Reply-To', '')

        if header_only:
            out['preview'] = ''
            out['has_attachment'] = False
            return out

        text_parts = []
        html_parts = []
        attachments = []
        cid_map = {}

        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition') or '')
            fname = part.get_filename()
            cid = part.get('Content-ID', '').strip('<>')

            if fname:
                fname = _decode_hdr(fname)
            
            if 'attachment' in disp.lower() or fname:
                payload = part.get_payload(decode=True)
                if payload:
                    import base64
                    attachments.append({
                        'filename': fname or 'attachment',
                        'content_type': ctype,
                        'size': len(payload),
                        'data': base64.b64encode(payload).decode('utf-8')
                    })
            elif ctype == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    text_parts.append(self._decode_payload(payload, part.get_content_charset()))
            elif ctype == 'text/html':
                payload = part.get_payload(decode=True)
                if payload:
                    html_parts.append(self._decode_payload(payload, part.get_content_charset()))
            elif cid:
                payload = part.get_payload(decode=True)
                if payload:
                    import base64
                    b64 = base64.b64encode(payload).decode('utf-8')
                    cid_map[cid] = f'data:{ctype};base64,{b64}'

        full_html = ''.join(html_parts)
        if full_html and cid_map:
            for cid, data_uri in cid_map.items():
                full_html = full_html.replace(f'cid:{cid}', data_uri)

        out['text'] = ''.join(text_parts)
        out['html'] = full_html
        out['attachments'] = attachments
        out['has_attachment'] = len(attachments) > 0
        out['preview'] = (out['text'][:120] if out['text'] else re.sub(r'<[^>]+>', '', out['html'])[:120]).strip()
        return out

    def _decode_payload(self, raw, charset):
        encodings = [charset, 'utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'latin-1']
        for enc in encodings:
            if not enc:
                continue
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode('utf-8', 'ignore')
