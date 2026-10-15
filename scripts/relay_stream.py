#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海雀摄像头 · 直播 relay 取流客户端（去云的完整数据面）

全链路（纯 Python / stdlib + pycryptodome）:
  1. 云 API UserLogin        -> access_token
  2. 云 API GetConnHelp      -> 当前 relay 地址 (relay_serv:relay_port)
  3. RelayInit (原握手帧换 token) -> 响应 f10.f1 = security_key (每会话动态下发)
  4. dk = PBKDF2_HMAC_SHA256(security_key, device_id, 1, 32)
  5. relay 下行媒体帧 (tag 40 00, encryptflag 0x66) ->
     env  = AES-CBC-dec(dk[0:16], dk[16:32], frame[0x18:0x48])
     data = AES-CBC-dec(env[0:16], env[16:32], frame[0x48:0x48+enc_len])
  6. 周期性 keepalive (00 00 00 05 2e)

输出: pcap/live.hevc (持续追加的 HEVC Annex-B) + pcap/live.raw (原始密文)
"""
import sys, os, socket, struct, json, hashlib, time, threading, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256

from cloud_api import Api, DEVICE
from relayinit_build import enc_field, enc_var, parse

KEEPALIVE = struct.pack('>I', 5) + b'.'
HEVC_START = (b'\x00\x00\x00\x01',)


def recv_exact(s, n, tmo=20.0):
    s.settimeout(tmo)
    buf = b''
    while len(buf) < n:
        try:
            d = s.recv(min(65536, n - len(buf)))
        except socket.timeout:
            return None if not buf else buf
        if not d:
            return None
        buf += d
    return buf


def recv_frame(s, tmo=20.0):
    """DIP 帧: [BE32 len(含4字节头)][proto]"""
    h = recv_exact(s, 4, tmo)
    if not h or len(h) < 4:
        return None
    n = struct.unpack('>I', h)[0]
    if n < 4 or n > 16 * 1024 * 1024:
        return None
    b = recv_exact(s, n - 4, tmo)
    return b if b is not None else None


def build_relayinit(uid, token, device=None, app_did=None):
    """生成 RelayInit 请求帧（委托 relayinit_build：脱敏模板 + 运行时注入凭证）。"""
    from relayinit_build import build_relayinit as _build
    return _build(uid, token, device, app_did)


def get_relay_addr():
    api = Api()
    r = api.login()
    b = r.get('body') or {}
    tok, uid = b.get('access_token'), b.get('user_id')
    r2 = api._req('/v5/device',
                  api._payload('GetConnHelp', {'device_ids': [DEVICE]}), token=tok)
    sv = (r2.get('body') or {}).get('servs') or []
    if not sv:
        raise RuntimeError('GetConnHelp empty: %s' % json.dumps(r2)[:300])
    s0 = sv[0]
    return tok, uid, (s0.get('relay_serv'), int(s0.get('relay_port') or 8434))


def handshake(relay, uid, tok):
    s = socket.create_connection(relay, timeout=15)
    s.sendall(build_relayinit(uid, tok))
    resp = recv_frame(s, 15.0)
    if resp is None:
        raise RuntimeError('no RelayInit response')
    fields = parse(resp)
    key = None
    for f, wt, v in fields:
        if f == 10 and wt == 2:
            for f2, wt2, v2 in parse(v):
                if f2 == 1 and wt2 == 2:
                    key = v2.decode()
    if not key:
        raise RuntimeError('no security_key in response: %s' % resp[:120].hex())
    return s, key


def iter_media(buf, start=0, tags=(0x40,)):
    """yield media frames from a byte buffer; resync over control msgs

    tags: 0x40=视频(tag 0x4000) / 0x20=音频(tag 0x2000)
    ★ 2026-10-14 修正：原实现硬编码只认 0x40，导致音频帧被整体丢弃
      （表现为「StartAudio 返回正常但收不到音频」）。
    """
    off = start
    n = len(buf)
    while off + 4 <= n:
        if buf[off] in tags and buf[off + 1] == 0x00 and off + 0x18 <= n:
            tl = (buf[off + 2] << 8) | buf[off + 3]
            flag = int.from_bytes(buf[off + 0x10:off + 0x14], 'little')
            if 0x48 <= tl < 0x20000 and off + tl <= n and flag in (0x65, 0x66):
                yield off, buf[off:off + tl]
                off += tl
                continue
        if buf[off:off + 3] == b'\x00\x00\x00' and off + 4 <= n:
            clen = int.from_bytes(buf[off:off + 4], 'big')
            if 0 < clen < 0x100000:
                off += 4 + clen
                continue
        off += 1   # resync


def dec_frame(fr, dk):
    flag = struct.unpack_from('<I', fr, 0x10)[0]
    el = struct.unpack_from('<I', fr, 0x14)[0]
    if flag != 0x66 or el <= 0 or 0x48 + el > len(fr):
        return None
    env = fr[0x18:0x48]
    ept = AES.new(dk[:16], AES.MODE_CBC, iv=dk[16:32]).decrypt(env)
    if not (1 <= ept[-1] <= 16):
        return None
    ep = ept[:-ept[-1]]
    if len(ep) < 32:
        return None
    ct = fr[0x48:0x48 + el]
    pt = AES.new(ep[:16], AES.MODE_CBC, iv=ep[16:32]).decrypt(ct)
    if 1 <= pt[-1] <= 16:
        pt = pt[:-pt[-1]]
    return pt + fr[0x48 + el:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=60)
    ap.add_argument('--hevc', default='pcap/live.hevc')
    ap.add_argument('--raw', default='pcap/live.raw')
    ap.add_argument('--stat-every', type=float, default=5)
    a = ap.parse_args()

    print('[1] login + GetConnHelp ...')
    tok, uid, relay = get_relay_addr()
    print('    relay = %s:%d' % relay)

    print('[2] RelayInit handshake ...')
    s, key = handshake(relay, uid, tok)
    print('    security_key = %r' % key)
    dk = PBKDF2(key.encode(), DEVICE.encode(), dkLen=32, count=1, hmac_hash_module=SHA256)
    print('    dk = %s' % dk.hex())

    stop = threading.Event()

    def ka_worker():
        while not stop.is_set():
            try:
                s.sendall(KEEPALIVE)
            except Exception:
                return
            stop.wait(15)

    threading.Thread(target=ka_worker, daemon=True).start()

    print('[3] receiving media ...')
    t0 = time.time()
    raw_f = open(a.raw, 'wb')
    hevc_f = open(a.hevc, 'wb')
    pending = bytearray()
    nframe = 0
    okframe = 0
    nbytes = 0
    last_stat = t0
    try:
        while time.time() - t0 < a.seconds:
            d = s.recv(262144)
            if not d:
                print('[relay] EOF at %.1fs' % (time.time() - t0))
                break
            nbytes += len(d)
            raw_f.write(d)
            raw_f.flush()
            pending += d
            # 只处理完整帧
            consumed_upto = 0
            for off, fr in iter_media(bytes(pending)):
                consumed_upto = off + len(fr)
                nframe += 1
                pt = dec_frame(fr, dk)
                if pt:
                    hevc_f.write(pt)
                    hevc_f.flush()
                    okframe += 1
            if consumed_upto:
                del pending[:consumed_upto]
            now = time.time()
            if now - last_stat >= a.stat_every:
                last_stat = now
                print('  [%5.1fs] rx=%7dB frames=%d ok=%d hevc=%dB pend=%dB'
                      % (now - t0, nbytes, nframe, okframe,
                         hevc_f.tell(), len(pending)))
    finally:
        stop.set()
        raw_f.close()
        hevc_f.close()
        s.close()
    print('[done] rx=%d bytes, media frames=%d, decrypted=%d, hevc=%d bytes'
          % (nbytes, nframe, okframe, os.path.getsize(a.hevc)))


if __name__ == '__main__':
    main()
