#!/usr/bin/env python3
"""前端结构 + 语法校验
1) 用 node --check 做真实 JS 语法检查（能发现多余/缺失的括号）
2) 重复函数定义检查
3) 顶层函数闭合检查
任何一项失败都会阻止提交。"""
import re, sys, os, subprocess, collections, tempfile

path = sys.argv[1] if len(sys.argv) > 1 else 'frontend/index.html'
src = open(path, encoding='utf-8').read()
ok = True

# 提取最后一个 <script> 块（主逻辑）
s = src.rfind('<script')
e = src.rfind('</script>')
if s < 0 or e < 0:
    print("❌ 找不到 <script> 块"); sys.exit(1)
inner = src[src.index('>', s)+1:e]
if not inner.strip():
    print("❌ 主 <script> 块为空"); sys.exit(1)

# ---------- 1) node --check 真实语法校验 ----------
node = None
for cand in ('node', '/usr/bin/node', '/usr/local/bin/node'):
    try:
        subprocess.run([cand, '--version'], capture_output=True, timeout=10)
        node = cand; break
    except Exception:
        continue
if node:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(inner); tmp = f.name
    r = subprocess.run([node, '--check', tmp], capture_output=True, text=True)
    os.unlink(tmp)
    if r.returncode != 0:
        print("❌ JS 语法错误（会导致全站脚本失效！）:")
        for ln in (r.stderr or '').split('\n')[:8]:
            if ln.strip(): print("   ", ln)
        ok = False
    else:
        print("✅ JS 语法正确（node --check）")
else:
    print("⚠️  未找到 node，跳过语法校验")

# ---------- 2) 重复函数 ----------
names = re.findall(r'^function\s+(\w+)', inner, re.M)
dups = {n: c for n, c in collections.Counter(names).items() if c > 1}
KNOWN_OK = set()
bad = {k: v for k, v in dups.items() if k not in KNOWN_OK}
if bad:
    print(f"❌ 重复函数定义: {bad}"); ok = False
else:
    print(f"✅ 无意外重复函数（白名单 {KNOWN_OK}）")

# ---------- 3) 顶层函数闭合 ----------
badfunc = []
for m in re.finditer(r'^function\s+(\w+)\s*\([^)]*\)\s*\{', inner, re.M):
    name = m.group(1); depth = 1; i = m.end(); n = len(inner); state = 'code'
    while i < n and depth > 0:
        ch = inner[i]
        if state == 'code':
            if ch == '/':
                k = i-1
                while k >= 0 and inner[k] in ' \t\n': k -= 1
                pv = inner[k] if k >= 0 else ''
                if pv and pv not in ')]}' and not pv.isalnum() and pv != '_':
                    j = i+1; cls = False
                    while j < n:
                        c2 = inner[j]
                        if c2 == '\\': j += 2; continue
                        if c2 == '[': cls = True
                        elif c2 == ']': cls = False
                        elif c2 == '/' and not cls: break
                        elif c2 == '\n': break
                        j += 1
                    i = j+1; continue
                j = inner.find('\n', i); i = n if j < 0 else j
            elif ch == "'": state = 'sq'
            elif ch == '"': state = 'dq'
            elif ch == '`': state = 'tpl'
            elif ch == '{': depth += 1
            elif ch == '}': depth -= 1
        elif state in ('sq','dq'):
            if ch == '\\': i += 1
            elif (state=='sq' and ch=="'") or (state=='dq' and ch=='"'): state='code'
        elif state == 'tpl':
            if ch == '\\': i += 1
            elif ch == '`': state = 'code'
        i += 1
    if depth != 0:
        badfunc.append((name, inner[:m.start()].count('\n')+1))
if badfunc:
    for n2, l in badfunc:
        print(f"❌ 函数 {n2}() (第{l}行) 未正常闭合，会吞并后续代码！")
    ok = False
else:
    print("✅ 所有顶层函数均正常闭合")

sys.exit(0 if ok else 1)
