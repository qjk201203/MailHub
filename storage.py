# -*- coding: utf-8 -*-
"""MailHub - 账号存储（SQLite）和聚合逻辑"""

import sqlite3
import os

# 支持通过环境变量指定数据目录（docker 部署时指向挂载卷）
_DATA_DIR = os.environ.get('MAILHUB_DATA_DIR', os.path.dirname(__file__))
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, 'mailhub.db')


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            imap_host TEXT NOT NULL,
            imap_port INTEGER DEFAULT 993,
            imap_ssl INTEGER DEFAULT 1,
            smtp_host TEXT DEFAULT '',
            smtp_port INTEGER DEFAULT 465,
            smtp_ssl INTEGER DEFAULT 1,
            display_name TEXT DEFAULT ''
        )
    ''')
    # 兼容旧库：补新列
    for col, default in (('imap_ssl', '1'), ('smtp_ssl', '1')):
        try:
            db.execute('ALTER TABLE accounts ADD COLUMN %s INTEGER DEFAULT %s' % (col, default))
        except Exception:
            pass
    # 邮件列表缓存表：{account, folder} 的邮件头，用于增量拉取
    db.execute('''
        CREATE TABLE IF NOT EXISTS mail_cache (
            account TEXT NOT NULL,
            folder TEXT NOT NULL,
            seq TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account, folder, seq)
        )
    ''')
    # 邮件正文全量缓存表：{account, folder, seq} 的全文，点开秒开
    db.execute('''
        CREATE TABLE IF NOT EXISTS body_cache (
            account TEXT NOT NULL,
            folder TEXT NOT NULL,
            seq TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account, folder, seq)
        )
    ''')
    # 应用设置表（key-value）
    db.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    db.commit()
    db.close()


def add_account(email, password, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, display_name=''):
    db = get_db()
    cur = db.execute('''
        INSERT OR REPLACE INTO accounts
        (email, password, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, display_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (email, password, imap_host, imap_port, imap_ssl, smtp_host, smtp_port, smtp_ssl, display_name))
    db.commit()
    db.close()
    return cur.lastrowid


def list_accounts():
    db = get_db()
    rows = db.execute('SELECT * FROM accounts ORDER BY id').fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_account(email):
    db = get_db()
    row = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
    db.close()
    return dict(row) if row else None


def delete_account(email):
    db = get_db()
    db.execute('DELETE FROM accounts WHERE email=?', (email,))
    db.execute('DELETE FROM mail_cache WHERE account=?', (email,))
    db.commit()
    db.close()


# ---- 邮件列表缓存（磁盘持久化，用于增量拉取） ----

def save_mail_cache(account, folder, mails):
    """批量保存某账号某文件夹的邮件头列表到缓存"""
    import json
    db = get_db()
    for m in mails:
        db.execute('''
            INSERT OR REPLACE INTO mail_cache (account, folder, seq, data)
            VALUES (?, ?, ?, ?)
        ''', (account, folder, str(m.get('uid', '')), json.dumps(m, ensure_ascii=False)))
    db.commit()
    db.close()


def load_mail_cache(account, folder, limit=50):
    """读取某账号某文件夹缓存的邮件（按 seq 倒序取最新 limit 封）"""
    import json
    db = get_db()
    rows = db.execute('''
        SELECT data FROM mail_cache
        WHERE account=? AND folder=?
        ORDER BY CAST(seq AS INTEGER) DESC LIMIT ?
    ''', (account, folder, limit)).fetchall()
    db.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r['data']))
        except Exception:
            continue
    return out


def get_max_cached_seq(account, folder):
    """获取某账号某文件夹已缓存的最大序号（用于增量判断）"""
    db = get_db()
    row = db.execute('''
        SELECT MAX(CAST(seq AS INTEGER)) as mx FROM mail_cache
        WHERE account=? AND folder=?
    ''', (account, folder)).fetchone()
    db.close()
    return row['mx'] if row and row['mx'] else 0


def update_mail_read_status(account, folder, seq, is_read):
    """更新单个邮件头缓存里的 is_read 字段（已读标记后同步缓存，避免刷新又变回未读）"""
    import json
    db = get_db()
    row = db.execute('''
        SELECT data FROM mail_cache
        WHERE account=? AND folder=? AND seq=?
    ''', (account, folder, str(seq))).fetchone()
    if row:
        try:
            data = json.loads(row['data'])
            data['is_read'] = bool(is_read)
            db.execute('''
                UPDATE mail_cache SET data=? WHERE account=? AND folder=? AND seq=?
            ''', (json.dumps(data, ensure_ascii=False), account, folder, str(seq)))
            db.commit()
        except Exception:
            pass
    db.close()


# ---- 邮件正文缓存（磁盘持久化，点开秒开） ----

def save_body(account, folder, seq, mail_dict):
    """保存单封邮件全文到正文缓存"""
    import json
    db = get_db()
    db.execute('''
        INSERT OR REPLACE INTO body_cache (account, folder, seq, data)
        VALUES (?, ?, ?, ?)
    ''', (account, folder, str(seq), json.dumps(mail_dict, ensure_ascii=False)))
    db.commit()
    db.close()


def load_body(account, folder, seq):
    """读取缓存的正文"""
    import json
    db = get_db()
    row = db.execute('''
        SELECT data FROM body_cache WHERE account=? AND folder=? AND seq=?
    ''', (account, folder, str(seq))).fetchone()
    db.close()
    if row:
        try:
            return json.loads(row['data'])
        except Exception:
            return None
    return None


# ---- 应用设置 ----

def get_setting(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    db.close()
    return row['value'] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    db.commit()
    db.close()
