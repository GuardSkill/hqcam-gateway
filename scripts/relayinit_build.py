#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RelayInit 握手帧构建器（**脱敏模板** + 运行时注入真实凭证）

设计
----
`assets/relayinit_template.bin` 是从真机抓包里抽出的 RelayInit 请求帧，
但**已把全部个人信息替换成等长占位符**（见 TPL_* 常量），因此可以安全公开分发。

运行时 `build_relayinit()` 把占位符换回真实值：
  · uid / device / app_did 都是固定 32 字符 → **直接字节替换**
    （长度不变 ⇒ protobuf 长度前缀依旧正确，天然支持任意嵌套，
      不必理解 EchoTest / SetConnInfo 的内部结构）
  · access_token 长度可变 → **结构化重建** inner 的 f5 字段，
    并同步修正 inner(f10) 与顶层两层长度前缀

模板里被替换的 6 处个人信息
---------------------------
    内层 f4                     user_id      (32)
    内层 f5                     access_token (53, 可变长)
    内层 f8                     device_id    (32)
    内层 f30(EchoTest).f2.f1    device_id    (32)
    内层 f30(EchoTest).f2.f16   user_id      (32)
    内层 f30(SetConnInfo).f2.f2 app_did      (32)

对外接口
--------
    parse(b) / enc_field(f, d) / enc_var(f, v)      —— 通用 protobuf 工具（兼容旧调用）
    load_template(path=None)                        —— 读取模板（绝对路径，支持 HQ_TEMPLATE）
    build_relayinit(uid, token, device=None, app_did=None, template=None)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 占位符常量
# 必须等长于被替换的字段，且各不互为子串，避免误替换。
TPL_UID = b'UID' + b'_' * 29        # 32 字符
TPL_DEVICE = b'DEV' + b'_' * 29     # 32 字符
TPL_APPDID = b'APD' + b'_' * 29     # 32 字符
TPL_TOKEN = b'TOK' + b'_' * 50      # 53 字符

DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'assets', 'relayinit_template.bin')


# ---------------------------------------------------------------- protobuf 工具
def _rv(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, i
        s += 7


def parse(b):
    """极简 protobuf 解析 -> [(field, wiretype, value)]，顺序保留"""
    i = 0
    out = []
    while i < len(b):
        key, i = _rv(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _rv(b, i)
            out.append((f, wt, v))
        elif wt == 2:
            ln, i = _rv(b, i)
            out.append((f, wt, b[i:i + ln]))
            i += ln
        elif wt == 5:
            out.append((f, wt, b[i:i + 4]))
            i += 4
        elif wt == 1:
            out.append((f, wt, b[i:i + 8]))
            i += 8
        else:
            raise ValueError('unsupported wiretype %d' % wt)
    return out


def _tag(f):
    key = f << 3
    return bytes([key]) if key < 128 else bytes([(key & 0x7f) | 0x80, key >> 7])


def enc_field(f, d):
    """length-delimited 字段"""
    key = (f << 3) | 2
    kb = bytes([key]) if key < 128 else bytes([(key & 0x7f) | 0x80, key >> 7])
    ln = len(d)
    lb = b''
    while True:
        b7 = ln & 0x7f
        ln >>= 7
        lb += bytes([b7 | (0x80 if ln else 0)])
        if not ln:
            return kb + lb + d


def enc_var(f, v):
    """varint 字段"""
    key = f << 3
    kb = bytes([key]) if key < 128 else bytes([(key & 0x7f) | 0x80, key >> 7])
    out = b''
    while True:
        b7 = v & 0x7f
        v >>= 7
        out += bytes([b7 | (0x80 if v else 0)])
        if not v:
            return kb + out


def _reenc(f, wt, v):
    """按原 wiretype 原样重编码（保持字节一致）"""
    if wt == 0:
        return enc_var(f, v)
    if wt == 2:
        return enc_field(f, v)
    if wt == 5:
        return _tag(f | 5) + v
    if wt == 1:
        return _tag(f | 1) + v
    raise ValueError('unsupported wiretype %d' % wt)


# ---------------------------------------------------------------- 模板
_TEMPLATE_CACHE = {}


def load_template(path=None):
    """读取（并缓存）脱敏模板。路径优先 HQ_TEMPLATE / config.json，其次默认 assets/。"""
    if path is None:
        try:
            from hq_config import cfg
            path = cfg.template
        except Exception:
            path = DEFAULT_TEMPLATE
    path = os.path.abspath(path)
    if path not in _TEMPLATE_CACHE:
        if not os.path.exists(path):
            raise RuntimeError(
                '找不到 RelayInit 模板: %s\n'
                '  请设置 HQ_TEMPLATE 指向 assets/relayinit_template.bin' % path)
        with open(path, 'rb') as f:
            _TEMPLATE_CACHE[path] = f.read()
    return _TEMPLATE_CACHE[path]


def _fit32(name, val):
    """固定 32 字符字段校验"""
    if val is None:
        raise ValueError('%s 不能为空' % name)
    b = val.encode() if isinstance(val, str) else val
    if len(b) != 32:
        raise ValueError('%s 必须是 32 字符（当前 %d）: %r' % (name, len(b), val))
    return b


def build_relayinit(uid, token, device=None, app_did=None, template=None):
    """用真实凭证生成 RelayInit 请求帧（含 4 字节大端长度前缀）。

    uid     : user_id（32 字符）
    token   : access_token（长度可变）
    device  : 设备 ID（32 字符）；缺省取配置
    app_did : App 设备 ID（32 字符）；缺省取配置
    """
    if device is None or app_did is None:
        try:
            from hq_config import cfg
            device = device if device is not None else cfg.device
            app_did = app_did if app_did is not None else cfg.app_did
        except Exception:
            pass
    if not token:
        raise ValueError('token 不能为空')
    if not device:
        raise ValueError('device 不能为空（请设置 HQ_DEVICE）')
    if not app_did:
        raise ValueError('app_did 不能为空（请设置 HQ_APPDID）')

    frame = load_template(template)

    # 1) 固定 32 字符的三项：字节级等长替换（不动长度前缀）
    b = frame
    b = b.replace(TPL_UID, _fit32('uid', uid))
    b = b.replace(TPL_DEVICE, _fit32('device', device))
    b = b.replace(TPL_APPDID, _fit32('app_did', app_did))

    # 2) token 可变长：结构化重建 inner 的 f5，并修正两层长度前缀
    tb = token.encode() if isinstance(token, str) else token
    body = b[4:]
    out = b''
    for f, wt, v in parse(body):
        if f == 10 and wt == 2:
            inner = b''
            for f2, wt2, v2 in parse(v):
                if f2 == 5 and wt2 == 2 and v2 == TPL_TOKEN:
                    inner += enc_field(5, tb)
                else:
                    inner += _reenc(f2, wt2, v2)
            out += enc_field(10, inner)
        else:
            out += _reenc(f, wt, v)
    return struct.pack('>I', len(out) + 4) + out


if __name__ == '__main__':
    # 自检：用抓包原帧里的真实凭证重建，应与原帧逐字节一致
    import re
    ORIG = sys.argv[1] if len(sys.argv) > 1 else 'pcap/hs/8434_0.bin'
    if not os.path.exists(ORIG):
        print('（未找到 %s，跳过自检）' % ORIG)
        sys.exit(0)
    raw = open(ORIG, 'rb').read()
    real = {}
    for f, wt, v in parse(raw[4:]):
        if f == 10 and wt == 2:
            for f2, wt2, v2 in parse(v):
                if f2 == 4:
                    real['uid'] = v2.decode()
                elif f2 == 5:
                    real['token'] = v2.decode()
                elif f2 == 8:
                    real['device'] = v2.decode()
    m = re.search(rb'SetConnInfo', raw)
    if m:
        c = re.search(rb'\x12\x20([0-9a-f]{32})', raw[m.end():])
        if c:
            real['app_did'] = c.group(1).decode()
    if 'app_did' not in real:
        print('无法从原帧解析 app_did，跳过自检')
        sys.exit(0)
    got = build_relayinit(real['uid'], real['token'], real['device'], real['app_did'])
    print('原帧长度 =', len(raw), ' 重建长度 =', len(got))
    print('逐字节一致 ?', got == raw)
    if got != raw:
        for i in range(min(len(got), len(raw))):
            if got[i] != raw[i]:
                print('首个差异 @', hex(i), got[i - 6:i + 6].hex(), 'vs', raw[i - 6:i + 6].hex())
                break
