#!/usr/bin/env python3
"""前端结构校验：重复函数(致命) / 函数体是否意外嵌套(致命) / TDZ(警告)
用「顶格 function 的位置关系」判断结构，比裸扫花括号更可靠。"""
import re, sys, collections

path = sys.argv[1] if len(sys.argv) > 1 else 'frontend/index.html'
src = open(path, encoding='utf-8').read()
s = src.rfind('<script'); e = src.rfind('</script>')
if s < 0 or e < 0:
    print("❌ 找不到 <script> 块"); sys.exit(1)
inner = src[src.index('>', s)+1:e]
ok = True

# 1) 重复函数（白名单外即为致命错误）
names = re.findall(r'^function\s+(\w+)', inner, re.M)
dups = {n: c for n, c in collections.Counter(names).items() if c > 1}
KNOWN_OK = {'switchFolder'}
bad = {k: v for k, v in dups.items() if k not in KNOWN_OK}
if bad:
    print(f"❌ 重复函数定义: {bad}  （会导致函数被覆盖或吞并）"); ok = False
else:
    print(f"✅ 无意外重复函数（白名单 {KNOWN_OK}）")

# 2) 每个顶格 function 必须能在其后找到顶格 '}' 收尾
badfunc = []
for m in re.finditer(r'^function\s+(\w+)\s*\([^)]*\)\s*\{', inner, re.M):
    name = m.group(1); start = m.start()
    depth = 1; i = m.end(); n = len(inner)
    state = 'code'
    while i < n and depth > 0:
        ch = inner[i]
        if state == 'code':
            # 正则字面量：/ 前面是非标识符字符时视为正则，跳过整段，避免把其中的引号当字符串
            if ch == '/':
                k = i - 1
                while k >= 0 and inner[k] in ' \t\n': k -= 1
                prevc = inner[k] if k >= 0 else ''
                if prevc and prevc not in ')]}' and not prevc.isalnum() and prevc != '_':
                    j = i + 1; in_cls = False
                    while j < n:
                        c2 = inner[j]
                        if c2 == '\\': j += 2; continue
                        if c2 == '[': in_cls = True
                        elif c2 == ']': in_cls = False
                        elif c2 == '/' and not in_cls: break
                        elif c2 == '\n': break
                        j += 1
                    i = j + 1
                    continue
            if ch == "'": state = 'sq'
            elif ch == '"': state = 'dq'
            elif ch == '`': state = 'tpl'
            elif ch == '/' and i+1 < n and inner[i+1] == '/':
                j = inner.find('\n', i); i = n if j < 0 else j
            elif ch == '/' and i+1 < n and inner[i+1] == '*':
                j = inner.find('*/', i); i = n if j < 0 else j+1
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
        line = inner[:start].count('\n') + 1
        badfunc.append((name, line))

if badfunc:
    for n, l in badfunc:
        print(f"❌ 函数 {n}() (第{l}行) 未正常闭合，会吞并后续代码！")
    ok = False
else:
    print("✅ 所有顶层函数均正常闭合")

# 3) TDZ 风险（提示，不阻断）
for v in re.findall(r'^let\s+(\w+)', inner, re.M):
    d = inner.index(f'let {v}')
    u = inner.find(f'{v} =')
    if u != -1 and u < d:
        line = inner[:u].count('\n') + 1
        print(f"⚠️  TDZ: '{v}' 在声明前(第{line}行)被赋值（若在初始化前调用会报错）")

sys.exit(0 if ok else 1)
