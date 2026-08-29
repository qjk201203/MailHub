# -*- coding: utf-8 -*-
"""
MailHub - IMAP 客户端封装

关键点：处理网易(163/126/yeah)邮箱的特殊要求 —— 登录成功后必须发送
IMAP ID 命令声明客户端身份，否则 SELECT 会报 "Unsafe Login"。
其它邮箱的 ID 命令可选，但统一发送也无害（符合 RFC 2971）。
"""

import imaplib
import email
import base64
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime


def _imap_utf7_decode(s):
    """解码 IMAP modified UTF-7 文件夹名（&xxx- 形式），还原中文等 Unicode"""
    if not s:
        return s
    if isinstance(s, bytes):
        s = s.decode('utf-8', 'ignore')
    # 逐个匹配 &xxx- 段落并替换
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '&':
            # 找结束符 -
            end = s.find('-', i)
            if end == -1:
                out.append(s[i:])
                break
            if end == i:  # "&-" 表示字面 &
                out.append('&')
                i = end + 1
                continue
            b64 = s[i+1:end]
            try:
                raw = base64.b64decode(b64 + '=' * (-len(b64) % 4))
                out.append(raw.decode('utf-16-be'))
            except Exception:
                out.append(s[i:end+1])
            i = end + 1
        else:
            # 普通字符，一直走到下一个 & 或结尾
            j = s.find('&', i)
            if j == -1:
                out.append(s[i:])
                break
            out.append(s[i:j])
            i = j
    return ''.join(out)


def _lower_emails(s):
    """将地址头中的邮箱地址转为小写，但保留显示名原样（如 'Name <A@B.com>' -> 'Name <a@b.com>'）"""
    if not s:
        return s
    s = str(s)
    # 匹配 <...> 里的邮箱，转小写（保留 < 前可能是 display name）
    def repl_angle(m):
        inner = m.group(1)
        # inner 里通常就是 "[display] email"，只小写邮箱 token
        em = re.search(r'([^@\s<>]+@[^\s<>]+)', inner)
        if em:
            return '<' + inner[:em.start()] + em.group(1).lower() + inner[em.end():] + '>'
        return '<' + inner.lower() + '>'
    s = re.sub(r'<([^>]+@[^>]+)>', repl_angle, s)
    # 处理不带尖括号的纯邮箱地址 token 转小写
    s = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', lambda m: m.group(0).lower(), s)
    return s


class MailAccount:
    """单个邮箱账号的 IMAP 连接与取信逻辑"""

    def __init__(self, email_addr, password, imap_host, imap_port=993, imap_ssl=1):
        self.email_addr = email_addr
        self.password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_ssl = imap_ssl  # 1=SSL直连, 2=STARTTLS, 0=明文
        self.conn = None

    def _send_id_command(self):
        """发送 IMAP ID 命令（声明客户端身份），根治网易 Unsafe Login"""
        try:
            # imaplib 标准库没内置 ID 命令，需用底层 send 手动发出
            # ID ("name" "MailHub" "version" "1.0" "vendor" "MailHub")
            tag = self.conn._new_tag()
            cmd = tag + b' ID ("name" "MailHub" "version" "1.0" "vendor" "MailHub")\r\n'
            self.conn.send(cmd)
            self.conn._get_response()
            return True
        except Exception:
            # ID 命令失败不致命，某些服务器不支持时忽略
            return False

    def connect(self):
        """建立 IMAP 连接并登录（支持 SSL / STARTTLS / 明文）"""
        if self.imap_ssl == 2:
            # STARTTLS：先明文连接再升级
            self.conn = imaplib.IMAP4(self.imap_host, self.imap_port)
            self.conn.starttls()
        elif self.imap_ssl == 0:
            self.conn = imaplib.IMAP4(self.imap_host, self.imap_port)
        else:
            self.conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        typ, dat = self.conn.login(self.email_addr, self.password)
        if typ != 'OK':
            raise Exception('IMAP 登录失败: %s' % dat)
        # 关键：登录后立即发送 ID 命令（163 必需）
        self._send_id_command()
        return True

    def list_folders(self):
        """列出邮箱文件夹，返回 [{name, raw}] 中文名正确解码"""
        typ, dat = self.conn.list()
        if typ != 'OK':
            return []
        folders = []
        for line in dat:
            if isinstance(line, bytes):
                line = line.decode('utf-8', 'ignore')
            # 文件夹名是最后一个引号包围的内容
            # 格式: (LIST flags) "/" "FolderName"
            m = re.search(r'"([^"]*)"$', line.strip())
            if not m:
                continue
            raw_name = m.group(1)
            # 转义的反斜杠引号
            raw_name = raw_name.replace('\\"', '"')
            decoded = _imap_utf7_decode(raw_name)
            folders.append({'name': decoded, 'raw': raw_name})
        return folders

    def fetch_recent(self, folder='INBOX', limit=50, light=True):
        """获取指定文件夹最近的邮件。

        light=True 时只拉邮件头(快，生成列表摘要)；
        light=False 时拉全文(慢，用于读正文)。
        """
        if not self.conn:
            return []
        try:
            typ, dat = self.conn.select(folder, readonly=True)
            if typ != 'OK':
                return []
        except Exception:
            return []

        # 搜索最近邮件
        typ, data = self.conn.search(None, 'ALL')
        if typ != 'OK':
            return []
        ids = data[0].split()
        # 取最新的 limit 封
        ids = ids[-limit:] if ids else []

        # 轻量模式只取必要 header 字段（更快），批量 fetch 一次取多个
        if light:
            fetch_what = '(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])'
        else:
            fetch_what = '(RFC822)'

        mails = []
        if not ids:
            return mails
        idstr = b','.join(ids)
        try:
            typ, msg_data = self.conn.fetch(idstr, fetch_what)
            if typ != 'OK':
                return mails
            # 批量返回是交替的 (header, raw) 元组和 b')' 结束符
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                    raw = item[1]
                    # 从 header 元组第一个元素解析序号（如 b'8270 (BODY[...]'）
                    seq = b''
                    if isinstance(item[0], (bytes, bytearray)):
                        m = re.match(rb'(\d+)', item[0])
                        if m:
                            seq = m.group(1)
                    try:
                        msg = email.message_from_bytes(bytes(raw))
                        mails.append(self._parse(msg, seq, header_only=light))
                    except Exception:
                        continue
        except Exception:
            # 批量失败时回退到逐个 fetch
            for i in ids:
                try:
                    typ, md = self.conn.fetch(i, fetch_what)
                    if typ != 'OK' or not md or not md[0]:
                        continue
                    raw = md[0][1]
                    msg = email.message_from_bytes(raw)
                    mails.append(self._parse(msg, i, header_only=light))
                except Exception:
                    continue
        return mails

    def fetch_one(self, account_check, seq_num):
        """按序号取单封邮件全文（用于读正文）"""
        if not self.conn:
            return None
        try:
            typ, msg_data = self.conn.fetch(seq_num, '(RFC822)')
            if typ != 'OK' or not msg_data or not msg_data[0]:
                return None
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            return self._parse(msg, seq_num, header_only=False)
        except Exception:
            return None

    def fetch_one_folder(self, folder, seq_num):
        """先 select 指定文件夹，再按序号取单封邮件全文"""
        if not self.conn:
            return None
        try:
            typ, dat = self.conn.select(folder, readonly=True)
            if typ != 'OK':
                return None
        except Exception:
            return None
        return self.fetch_one(None, seq_num)

    def fetch_bodies_batch(self, folder, seq_list):
        """批量取多封邮件全文（正文预取，速度快）。返回 {seq: mail_dict}"""
        if not self.conn or not seq_list:
            return {}
        try:
            typ, dat = self.conn.select(folder, readonly=True)
            if typ != 'OK':
                return {}
        except Exception:
            return {}
        seq_list = [str(s) for s in seq_list]
        idstr = ','.join(seq_list)
        result = {}
        try:
            typ, msg_data = self.conn.fetch(idstr, '(RFC822)')
            if typ != 'OK':
                return result
            seq = None
            for item in msg_data:
                if isinstance(item, tuple):
                    # item[0] 是序号标记如 b'8270 (RFC822 {...}'
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
        except Exception:
            pass
        return result

    def _decode_payload(self, part):
        """解码一个 part 的 payload，正确处理 GBK/GB2312 等中文编码"""
        payload = part.get_payload(decode=True)
        if payload is None:
            return ''
        charset = part.get_content_charset() or 'utf-8'
        # 尝试用声明的 charset 解码
        for enc in (charset, 'utf-8', 'gb18030', 'gbk', 'big5', 'latin-1'):
            try:
                return payload.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return payload.decode('utf-8', 'replace')

    def _parse(self, msg, uid, header_only=False):
        """解析 email 对象为 dict。header_only=True 时跳过正文解析（提速）"""
        def _dec(s):
            if not s:
                return ''
            try:
                return str(make_header(decode_header(s)))
            except Exception:
                return s

        # 解析正文（正确区分 text/plain 和 text/html）
        body_text = ''
        body_html = ''
        if not header_only:
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    cdisp = str(part.get('Content-Disposition', '')).lower()
                    if 'attachment' in cdisp:
                        continue
                    payload = self._decode_payload(part)
                    if ctype == 'text/plain':
                        body_text += payload
                    elif ctype == 'text/html':
                        body_html += payload
            else:
                # 非 multipart：根据 content-type 判断
                ctype = msg.get_content_type()
                payload = self._decode_payload(msg)
                if ctype == 'text/html':
                    body_html = payload
                else:
                    body_text = payload

        # 日期（保留完整时间戳，含时区偏移；另给列表用的短格式）
        date_str = msg.get('Date', '')
        dt = None
        try:
            dt = parsedate_to_datetime(date_str)
        except Exception:
            dt = None
        if dt:
            date_iso = dt.strftime('%Y-%m-%d %H:%M')          # 列表短格式
            full_date = dt.strftime('%Y-%m-%d %H:%M:%S')       # 阅读面板时间戳(到秒)
        else:
            date_iso = date_str
            full_date = date_str

        # 退信识别：发件人是邮件守护进程/退回的邮件
        from_raw = _dec(msg.get('From', ''))
        from_lower = from_raw.lower()
        is_bounce = any(k in from_lower for k in (
            'mailer-daemon', 'mail delivery', 'postmaster', 'mail delivery subsystem',
            'returned mail', 'undelivered mail', 'delivery status',
        ))

        return {
            'uid': uid.decode() if isinstance(uid, bytes) else str(uid),
            'from': _lower_emails(from_raw),
            'to': _lower_emails(_dec(msg.get('To', ''))),
            'cc': _lower_emails(_dec(msg.get('Cc', ''))),
            'subject': _dec(msg.get('Subject', '(无主题)')),
            'date': date_iso,
            'full_date': full_date,
            'text': body_text.strip(),
            'html': body_html.strip(),
            'has_attachment': 'attachment' in str(msg).lower(),
            'is_bounce': is_bounce,
        }

    def logout(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        finally:
            self.conn = None
