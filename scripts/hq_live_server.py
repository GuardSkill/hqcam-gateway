#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海雀摄像头 · 本地化直播网关（完全去 App）

功能
  - 自建 relay 会话：云登录 -> GetConnHelp -> RelayInit -> StartVideo
  - 实时解密媒体帧 -> HEVC Annex-B
  - HTTP 端点：
      GET /             状态页
      GET /status.json  JSON 状态
      GET /live.hevc    原始 HEVC Annex-B 流
      GET /live.ts      MPEG-TS 流（仅视频，VLC / HA / Frigate 可直接吃）
      GET /av.ts        ★ 音视频合流 MPEG-TS（HEVC + AAC），推荐给 HA/Frigate
      GET /audio.wav    实时音频 WAV（16kHz/16bit/单声道，G.711 A-law 解码）
      GET /audio.alaw   原始 A-law 字节流
      GET /snapshot.jpg 当前画面快照
      GET /ptz?dir=up|down|left|right|upleft|upright|downleft|downright|stop&ms=400
      GET /ptz/raw?ch=0&code=101&p1=0&p2=0
      GET /rpc?method=PtzPosition&payload=0800   通用明文 RPC（自动解密响应）
      ★ 对讲上行（新增）
      POST /talk         body = 原始 A-law 16kHz 单声道字节
      POST /talk.wav     body = WAV 文件（自动转 16kHz 单声道 A-law）
      GET  /talk/tone?hz=1000&ms=1000   发一段测试音
  - 云台：PtzCtrl {f1=ch_no, f2=code, f6=para1, f7=para2}

对讲上行（2026-10-14 攻克）
  完整流程 = StartTalkback RPC -> tag=0x2000 音频帧 -> StopTalkback RPC
  实测 App 对讲帧：408B = 24B 头 + 48B env + 336B ct，明文 320B = A-law 20ms@16kHz，50fps。
  设备对 StartTalkback 的响应 {f1=101, f2=16000, f3=16, f4=1} 即 16kHz/16bit/单声道。
  关键：**必须先发 StartTalkback**，否则设备不播。

用法
  python scripts/hq_live_server.py [--port 8899] [--host 0.0.0.0]
"""
import sys, os, io, re, json, time, struct, socket, queue, threading, hashlib, argparse
import select, signal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from fractions import Fraction

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
import av

from cloud_api import Api, DEVICE
from relayinit_build import enc_field, enc_var
from hq_config import cfg
import relay_stream as RS
import webui

SUF = b'DaNalEsZX'

# ★ HTTP 访问令牌（来自 HQ_TOKEN）。留空 = 不鉴权（仅限可信内网）。
ACCESS_TOKEN = cfg.token or ''
# 登录失败计数（只用于递增延时，防脚本爆破；成功即清零）
LOGIN_FAIL = {'n': 0}

# ★ 上游空闲阈值：这么多秒收不到任何媒体数据，就把所有拉流连接主动断开。
#   背景（2026-10-22 事故）：摄像头拔掉后上游永久连不上，而 /live.ts、/av.ts
#   等端点用无超时的 q.get() 等数据 -> 线程永久挂住；录像容器每 25 秒重连一次
#   -> 29 小时累积 1655 个挂死线程，CPU 打满 4 核、healthcheck 连续失败。
STREAM_IDLE = float(os.environ.get('HQ_STREAM_IDLE', '20'))

# /mjpeg 单连接最长存活秒数（兜底：万一客户端断开检测失效也能回收线程）
MJPEG_MAX_SECS = float(os.environ.get('HQ_MJPEG_MAX_SECS', '7200'))

# 上游重连退避：3s -> 6 -> 12 -> 24 -> 48 -> 60 封顶。
#   原来固定 3 秒且"正常 EOF"分支根本不等待，设备拔掉后会对云 API 疯狂登录
#   （29 小时 ~3.5 万次），CPU 全耗在 TLS 握手上。
RECONNECT_MIN = float(os.environ.get('HQ_RECONNECT_MIN', '3'))
RECONNECT_MAX = float(os.environ.get('HQ_RECONNECT_MAX', '60'))
# 连接存活超过这个秒数就认为"这次是成功的"，退避计数清零
RECONNECT_STABLE = float(os.environ.get('HQ_RECONNECT_STABLE', '30'))

# 离线占位帧缓存
_OFFLINE = {'jpg': None}


def offline_jpeg(w=960):
    """上游离线时给 /mjpeg 推的占位帧（深灰底 + 文案），只生成一次。"""
    if _OFFLINE['jpg'] is not None:
        return _OFFLINE['jpg']
    data = b''
    try:
        from PIL import Image, ImageDraw, ImageFont
        w = max(320, int(w))
        im = Image.new('RGB', (w, int(w * 9 / 16)), (22, 24, 30))
        d = ImageDraw.Draw(im)
        try:
            f = ImageFont.truetype('DejaVuSans.ttf', max(14, w // 40))
        except Exception:
            f = ImageFont.load_default()
        txt = 'CAMERA OFFLINE'
        try:
            bb = d.textbbox((0, 0), txt, font=f)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            tw, th = len(txt) * 8, 12
        d.text(((im.width - tw) // 2, (im.height - th) // 2), txt,
               fill=(140, 148, 165), font=f)
        b = io.BytesIO()
        im.save(b, 'JPEG', quality=70)
        data = b.getvalue()
    except Exception as e:
        print('[mjpeg] 占位帧生成失败 %r' % (e,), flush=True)
    _OFFLINE['jpg'] = data
    return data


def peer_gone(conn):
    """非阻塞探测客户端是否已断开（MJPEG 是单向流，客户端只会发 FIN）。"""
    try:
        r, _, _ = select.select([conn], [], [], 0)
        if not r:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b''
    except Exception:
        return True


def render_login(err=''):
    return webui.LOGIN_HTML.replace('{{ERR}}', err)

KEEPALIVE = struct.pack('>I', 5) + b'.'
PAY_STARTVIDEO = bytes.fromhex('08011001204b3000')   # ch_no=1 client_type=1 quality=75 vstrm=0

PTZ = {
    'stop': 100, 'up': 101, 'down': 102, 'left': 103, 'right': 104,
    'upleft': 105, 'downleft': 106, 'upright': 107, 'downright': 108,
    'zoomin': 113, 'zoomout': 114, 'lighton': 127, 'lightoff': 128,
    'lock': 141, 'unlock': 142,
}

# ---------------------------------------------------------------- protobuf
def rdv(b, i):
    r = 0; s = 0
    while i < len(b):
        c = b[i]; i += 1
        r |= (c & 0x7f) << s
        if not (c & 0x80):
            return r, i
        s += 7
    return None, i


def parse(body):
    i = 0; out = []
    while i < len(body):
        key, i = rdv(body, i)
        if key is None:
            break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rdv(body, i)
        elif wt == 2:
            l, i = rdv(body, i); v = body[i:i + l]; i += l
        elif wt == 5:
            v = body[i:i + 4]; i += 4
        elif wt == 1:
            v = body[i:i + 8]; i += 8
        else:
            break
        out.append((fn, wt, v))
    return out


def g(fl, k, d=None):
    for f, wt, v in fl:
        if f == k:
            return v
    return d


def unpad(p):
    n = p[-1]
    return p[:-n] if 1 <= n <= 16 and p[-n:] == bytes([n]) * n else None


def plain_rpc(method, payload=b'', session=1, f5=1):
    b = (enc_var(1, 2) + enc_field(2, b'0.1.2') + enc_field(3, method.encode())
         + enc_var(5, f5) + enc_var(7, 0) + enc_field(9, b'')
         + enc_field(10, payload) + enc_var(11, session)
         + enc_var(13, 0) + enc_var(25, 12))
    return struct.pack('>I', len(b) + 4) + b


# ---------------------------------------------------------------- hub
class Hub:
    def __init__(self):
        self.subs = []
        self.lock = threading.Lock()
        self.stats = {'frames': 0, 'bytes': 0, 'au': 0, 'start': time.time(),
                      'reconnects': 0, 'sk': '', 'relay': ''}
        # 最近一次收到数据的时间（流端点据此判断上游是否还活着）
        self.last_data = time.time()

    def subscribe(self):
        q = queue.Queue(maxsize=600)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def broadcast(self, au):
        self.stats['au'] += 1
        self.last_data = time.time()
        with self.lock:
            dead = []
            for q in self.subs:
                try:
                    q.put_nowait(au)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subs.remove(q)


HUB = Hub()


RXLOG = {'src': [], 'asrc': []}
RXMAX = 40


def rx_log(who, body):
    q = RXLOG[who]
    q.append((time.time(), body))
    if len(q) > RXMAX:
        del q[:len(q) - RXMAX]


AHUB = Hub()          # 音频订阅中心（第二条 relay 连接，tag=0x2000）

# 流端点哨兵：_END = 上游已离线，应主动断开；None = 本轮没数据，继续等
_END = object()


def backoff_delay(fails):
    """重连退避秒数：3 / 6 / 12 / 24 / 48 / 60(封顶)。"""
    if fails <= 0:
        return RECONNECT_MIN
    return min(RECONNECT_MAX, RECONNECT_MIN * (2 ** min(fails - 1, 6)))


def q_next(q, hub, idle=None, timeout=1.0):
    """带空闲超时的订阅队列取数（避免上游掉线后线程永久挂住）。

    返回 数据块 / None（本轮无数据，继续）/ _END（上游 idle 秒无数据，断开）。
    """
    idle = STREAM_IDLE if idle is None else idle
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return _END if time.time() - hub.last_data > idle else None
SNAP = {'jpg': None, 'small': None, 'ts': 0, 'want': threading.Event(),
        'lock': threading.Lock(), 'err': None}


def alaw2lin(a):
    """G.711 A-law 8bit -> 16bit 线性 PCM"""
    a ^= 0x55
    seg = (a & 0x70) >> 4
    t = (a & 0x0F) << 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if (a & 0x80) else -t


# A-law 256 项查表 -> 16bit LE 字节（音频热路径用）
ALAW_PCM = [struct.pack('<h', alaw2lin(i)) for i in range(256)]


# ---------------------------------------------------------------- 对讲上行
# A-law 段端点表（13bit 域），标准 ITU-T G.711 / Sun 实现
_ALAW_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def lin2alaw(pcm_val):
    """16bit 线性 PCM -> G.711 A-law 字节（标准 ITU-T G.711 / Sun linear2alaw）

    与标准库 audioop.lin2alaw 逐字节一致（已比对 4000 随机样本）。
    """
    pcm_val = int(pcm_val) >> 3          # 16bit -> 13bit（算术右移）
    if pcm_val >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm_val = -pcm_val - 1
    seg = 8
    for i, v in enumerate(_ALAW_SEG_AEND):
        if pcm_val <= v:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    if seg < 2:
        aval |= (pcm_val >> 1) & 0x0F
    else:
        aval |= (pcm_val >> seg) & 0x0F
    return aval ^ mask


def alaw_tone(hz, ms, rate=16000, amp=0.35):
    """生成一段 A-law 正弦测试音"""
    import math
    n = int(rate * ms / 1000.0)
    out = bytearray()
    for i in range(n):
        s = int(amp * 32767 * math.sin(2 * math.pi * hz * i / rate))
        out.append(lin2alaw(s))
    return bytes(out)


TALK_CHUNK = 320          # 每帧 A-law 字节数 = 20ms @ 16kHz（与 App 一致）
TALK_RATE = 16000
DEFAULT_TALK_VOL = 80     # ★ 对讲前确保喇叭音量 >= 该值（设备音量可能是 0 = 静音）


def build_uplink_audio_frame(alaw, dk, ts=0, mode='onekey'):
    """构造 tag=0x2000 的**上行**音频帧。

    实测 App 上行帧（408B）：
      2000 0198 01650100 00000000 00000000 66000000 50010000 <env48> <ct336>

    ★ 固件依据（libdanale_sdk_device.so, danavideo_decrypt_av_data @0x926db4）：
      媒体帧头偏移 0x10 = encrypt_flag，分支：
        flag=0    明文，0x18 之后全部是数据
        flag=1    AES-CBC(dk[0:16], iv=dk[16:32], frame[0x18:0x18+el]) + PKCS5 unpad，
                  再把 frame[0x18+el:] 之后的多余字节按明文追加
                  （**不需要 onekey**，用的是 conn+0x620 的会话密钥）
        flag=100  32B devframe-key 信封 + ct
        flag=101/102  “onekey” 模式：env48 用 PBKDF2(A,B,1,32)[:16] 解，ct 用
                  danavideo_get_decrypt_devframe_key 解；若 conn[0x628]==0 则
                  **直接返回 conn[0x620]（会话 dk）**；反之要求 onekey 已启用

    mode:
      'onekey' : flag=102，env48 + ct（= App 实测形态）
      'dk'     : flag=1，无 env，ct 直接用会话 dk
      'plain'  : flag=0，不加密
    """
    pad = 16 - (len(alaw) % 16)
    el = len(alaw) + pad
    if mode == 'onekey':
        ep = os.urandom(32)
        epad = 16 - (len(ep) % 16)
        env = AES.new(dk[:16], AES.MODE_CBC, iv=dk[16:32]).encrypt(
            ep + bytes([epad]) * epad)
        ct = AES.new(ep[:16], AES.MODE_CBC, iv=ep[16:32]).encrypt(
            alaw + bytes([pad]) * pad)
        flag = 102
        body = env + ct
    elif mode == 'dk':
        ct = AES.new(dk[:16], AES.MODE_CBC, iv=dk[16:32]).encrypt(
            alaw + bytes([pad]) * pad)
        flag = 1
        body = ct
    elif mode == 'plain':
        flag = 0
        el = len(alaw)
        body = alaw
    else:
        raise ValueError('bad mode %r' % mode)
    total = 0x18 + len(body)
    h = (b'\x20\x00' + struct.pack('>H', total) + b'\x01\x65\x01\x00'
         + struct.pack('<I', ts) + b'\x00\x00\x00\x00'
         + struct.pack('<I', flag) + struct.pack('<I', el))
    return h + body


def _flag(q, name):
    """查询参数布尔开关：?name=1 / =true / =yes / =on 均视为真"""
    v = (q.get(name) or [''])[0]
    return str(v).lower() in ('1', 'true', 'yes', 'on', 'y', 't')


def wav_to_alaw(data, rate=TALK_RATE):
    """WAV 字节 -> 16kHz 单声道 A-law（16bit 直接转，其它位深先转 16bit）"""
    import wave
    try:
        w = wave.open(io.BytesIO(data), 'rb')
    except Exception:
        return None
    nch = w.getnchannels()
    sw = w.getsampwidth()
    sr = w.getframerate()
    raw = w.readframes(w.getnframes())
    w.close()
    # -> 16bit
    if sw == 1:
        pcm = [((b - 128) << 8) for b in raw]
    elif sw == 2:
        pcm = list(struct.unpack('<%dh' % (len(raw) // 2), raw))
    elif sw == 4:
        v = struct.unpack('<%di' % (len(raw) // 4), raw)
        pcm = [x >> 16 for x in v]
    else:
        return None
    # 多声道 -> 单声道
    if nch > 1:
        pcm = [sum(pcm[i:i + nch]) // nch for i in range(0, len(pcm) - nch + 1, nch)]
    # 重采样到 16kHz（最近邻）
    if sr != rate and sr > 0 and pcm:
        n = int(len(pcm) * rate / sr)
        pcm = [pcm[min(len(pcm) - 1, int(i * sr / rate))] for i in range(n)]
    return bytes(lin2alaw(x) for x in pcm)


# ---------------------------------------------------------------- source
class Source(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.sock = None
        self.SK = b''
        self.dk = b''
        self.slk = threading.Lock()
        self.waiters = {}
        self.wlock = threading.Lock()
        self.sess = 80000
        self.pend = bytearray()
        self.buf = bytearray()
        self.au = []
        self.search = 0
        self.cur = None
        self.decoder = None
        self.state = 'init'

    # ---- 网络 ----
    def connect(self):
        tok, uid, relay = RS.get_relay_addr()
        s, key = RS.handshake(relay, uid, tok)
        self.sock = s
        self.SK = key.encode()
        self.dk = PBKDF2(self.SK, DEVICE.encode(), dkLen=32, count=1,
                         hmac_hash_module=SHA256)
        HUB.stats['sk'] = key
        HUB.stats['relay'] = '%s:%d' % relay
        s.settimeout(1.0)
        print('    [src] relay=%s:%d SK=%r' % (relay[0], relay[1], key))

    def send(self, method, payload=b'', session=None, wait=3.0):
        if session is None:
            self.sess += 1
            session = self.sess
        q = queue.Queue()
        with self.wlock:
            self.waiters[session] = q
        with self.slk:
            self.sock.sendall(plain_rpc(method, payload, session=session))
        try:
            r = q.get(timeout=wait)
        except queue.Empty:
            r = None
        with self.wlock:
            self.waiters.pop(session, None)
        return session, r

    def send_bytes(self, data):
        """在视频连接上直接发原始字节（对讲帧可能要走这条）"""
        with self.slk:
            try:
                self.sock.sendall(data)
                return True
            except Exception as e:
                print('    [src] send_bytes 失败 %r' % (e,))
                return False

    # ---- 解密 ----
    def decode_resp(self, body):
        fl = parse(body)
        f5 = g(fl, 5, 0); f11 = g(fl, 11, 0); f13 = g(fl, 13, 0); f7 = g(fl, 7, 0)
        if not isinstance(f5, int): f5 = 0
        if not isinstance(f11, int): f11 = 0
        r = {'f5': f5, 'f7': f7, 'sess': f11, 'f13': f13}
        if f13 == 41:
            p1 = hashlib.pbkdf2_hmac('sha256', self.SK, struct.pack('>II', f5, f11), 1, 48)
            f18 = g(fl, 18, b'')
            meth = None
            if isinstance(f18, bytes) and len(f18) == 16:
                meth = unpad(AES.new(p1[:16], AES.MODE_CBC, iv=p1[16:32]).decrypt(f18))
            r['method'] = meth
            pay = None
            if meth:
                p2 = hashlib.pbkdf2_hmac('sha256', self.SK,
                                         struct.pack('>II', f5, f11) + meth + SUF, 1, 32)
                f10 = g(fl, 10, b'')
                if isinstance(f10, bytes) and len(f10) >= 16 and len(f10) % 16 == 0:
                    pay = unpad(AES.new(p2[:16], AES.MODE_CBC, iv=p2[16:32]).decrypt(f10))
            r['payload'] = pay
            r['fields'] = parse(pay) if pay else None
        else:
            r['method'] = g(fl, 3, b'')
            r['payload'] = g(fl, 10, b'')
            r['fields'] = parse(g(fl, 10, b'')) if isinstance(g(fl, 10, b''), bytes) else None
        return r

    # ---- HEVC 分包 ----
    def on_nal(self, nal):
        t = (nal[0] >> 1) & 0x3f
        prev_vcl = bool(self.au) and ((self.au[-1][0] >> 1) & 0x3f) < 32
        if self.au and ((t < 32 and prev_vcl) or t == 32):
            data = b''.join(b'\x00\x00\x00\x01' + n for n in self.au)
            self.au = []
            HUB.broadcast(data)
            if SNAP['want'].is_set():
                self.try_decode(data)
        self.au.append(nal)

    def feed(self, data):
        self.buf += data
        while True:
            idx = self.buf.find(b'\x00\x00\x01', self.search)
            if idx < 0:
                self.search = max(self.search, len(self.buf) - 3)
                return
            if self.cur is not None:
                nal = bytes(self.buf[self.cur:idx]).rstrip(b'\x00')
                if nal:
                    self.on_nal(nal)
            del self.buf[:idx + 3]
            self.cur = 0
            self.search = 0

    def try_decode(self, au):
        try:
            if self.decoder is None:
                self.decoder = av.CodecContext.create('hevc', 'r')
            pk = av.Packet(au)
            frames = self.decoder.decode(pk)
            for fr in frames:
                img = fr.to_image()
                b = io.BytesIO()
                img.save(b, 'JPEG', quality=82)
                full = b.getvalue()
                # 缩略图：给 /mjpeg 预览用（3K 原图直接推 MJPEG 太吃带宽）
                small = full
                try:
                    w = 960
                    im2 = img.resize((w, int(img.height * w / img.width))) \
                        if img.width > w else img
                    b2 = io.BytesIO()
                    im2.save(b2, 'JPEG', quality=75)
                    small = b2.getvalue()
                except Exception:
                    pass
                with SNAP['lock']:
                    SNAP['jpg'] = full
                    SNAP['small'] = small
                    SNAP['ts'] = time.time()
                SNAP['want'].clear()
                return True
        except Exception as e:
            # 不再静默吞掉：首次失败打日志，并把错误暴露给 /snapshot.jpg
            if SNAP.get('err') != repr(e):
                SNAP['err'] = repr(e)
                print('[snap] 解码/编码失败: %r' % (e,), flush=True)
            return False
        return False

    # ---- 主循环 ----
    def run(self):
        fails = 0
        while not self.stop_flag.is_set():
            t_conn = 0.0
            err = None
            try:
                self.state = 'connecting'
                self.connect()
                t_conn = time.time()
                self.state = 'live'
                fails = 0
                last_ka = [0.0]
                _, r = self.send('StartVideo', PAY_STARTVIDEO, wait=5.0)
                print('    [src] StartVideo -> %s' % (r.get('f7') if r else 'timeout'))
                while not self.stop_flag.is_set():
                    now = time.time()
                    if now - last_ka[0] >= 10:
                        last_ka[0] = now
                        with self.slk:
                            try:
                                self.sock.sendall(KEEPALIVE)
                            except Exception:
                                err = 'keepalive failed'
                                break
                    try:
                        d = self.sock.recv(262144)
                    except socket.timeout:
                        continue
                    if not d:
                        err = 'EOF'
                        break
                    HUB.stats['bytes'] += len(d)
                    self.pend += d
                    self.consume()
            except Exception as e:
                err = e
            finally:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.pend = bytearray()
                self.buf = bytearray()
                self.au = []
                self.cur = None
                self.search = 0
                self.decoder = None
            if self.stop_flag.is_set():
                break
            lived = (time.time() - t_conn) if t_conn else 0.0
            if lived > RECONNECT_STABLE:
                fails = 0
            else:
                fails += 1
            self.state = 'error'
            HUB.stats['reconnects'] += 1
            delay = backoff_delay(fails)
            print('    [src] %r  存活 %.0fs -> %.0fs 后重连（连续失败 %d）'
                  % (err, lived, delay, fails), flush=True)
            self.stop_flag.wait(delay)

    def consume(self):
        b = bytes(self.pend)
        off = 0
        n = len(b)
        consumed = 0
        while off + 4 <= n:
            # 媒体帧 40 00 + BE16 len
            if b[off] == 0x40 and b[off + 1] == 0x00 and off + 0x18 <= n:
                tl = (b[off + 2] << 8) | b[off + 3]
                if 0x48 <= tl <= 0x20000:
                    if off + tl > n:
                        break
                    fr = b[off:off + tl]
                    HUB.stats['frames'] += 1
                    pt = RS.dec_frame(fr, self.dk)
                    if pt:
                        self.feed(pt)
                    off += tl
                    consumed = off
                    continue
            clen = int.from_bytes(b[off:off + 4], 'big')
            if 4 <= clen <= 0x200000:
                if off + clen > n:
                    break
                body = b[off + 4:off + clen]
                rx_log('src', body)
                try:
                    r = self.decode_resp(body)
                    fl = r.get('fields') or []
                    print('    [src] <- rpc len=%d f11=%s f13=%s f7=%s f3=%r'
                          % (clen, r.get('sess'), g(fl, 13, None),
                             r.get('f7'), g(fl, 3, None)))
                    with self.wlock:
                        w = self.waiters.get(r['sess'])
                    if w is not None:
                        w.put(r)
                except Exception:
                    pass
                off += clen
                consumed = off
                continue
            break
        if consumed:
            del self.pend[:consumed]


SRC = Source()


class AudioSource(threading.Thread):
    """第二条 relay 连接：RelayInit -> StartAudio -> tag=0x2000 -> A-law 广播。

    媒体帧与视频同构，仅 tag 不同（0x2000），帧头 24B:
      tag(2) | BE16 总长(2) | 4B | LE32 ts(ms) | 4B | LE32 flag=0x66 | LE32 el
    载荷 0x18..0x48 = env(48B)，0x48..0x48+el = 密文（同一 dk 解密）。
    编码: G.711 A-law / 16000 Hz / 单声道 / 960 样本每帧 / 帧间隔 60ms
    """
    ALAW_RATE = 16000

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = threading.Event()
        self.sock = None
        self.SK = b''
        self.dk = b''
        self.pend = bytearray()
        self.state = 'init'
        self.wlock = threading.Lock()
        self.sess = 90000
        self.waiters = {}
        self.talk_state = 'idle'

    def connect(self):
        tok, uid, relay = RS.get_relay_addr()
        s, key = RS.handshake(relay, uid, tok)
        self.sock = s
        self.SK = key.encode()
        self.dk = PBKDF2(self.SK, DEVICE.encode(), dkLen=32, count=1,
                         hmac_hash_module=SHA256)
        AHUB.stats['sk'] = key
        AHUB.stats['relay'] = '%s:%d' % relay
        s.settimeout(1.0)
        print('    [asrc] relay=%s:%d SK=%r' % (relay[0], relay[1], key))

    def decode_resp(self, body):
        """解密设备在音频连接上回的加密响应（与 Source.decode_resp 同构）。

        ★ 2026-10-15 补：原来音频连接**从不解密响应**，导致 resp_method 恒 None、
          音量等只存在于 payload 里的值读不到。方案（已实测验证）：
            method  = AES-CBC(PBKDF2(SK, f5‖f11, 1, 48))        解 f18
            payload = AES-CBC(PBKDF2(SK, f5‖f11‖method‖'DaNalEsZX', 1, 32)) 解 f10
        """
        fl = parse(body)
        f5 = g(fl, 5, 0); f11 = g(fl, 11, 0); f13 = g(fl, 13, 0); f7 = g(fl, 7, 0)
        if not isinstance(f5, int): f5 = 0
        if not isinstance(f11, int): f11 = 0
        r = {'f5': f5, 'f7': f7, 'sess': f11, 'f13': f13,
             'f9': g(fl, 9, None)}
        if f13 == 41:
            p1 = hashlib.pbkdf2_hmac('sha256', self.SK, struct.pack('>II', f5, f11), 1, 48)
            f18 = g(fl, 18, b'')
            meth = None
            if isinstance(f18, bytes) and len(f18) == 16:
                meth = unpad(AES.new(p1[:16], AES.MODE_CBC, iv=p1[16:32]).decrypt(f18))
            r['method'] = meth
            pay = None
            if meth:
                p2 = hashlib.pbkdf2_hmac('sha256', self.SK,
                                         struct.pack('>II', f5, f11) + meth + SUF, 1, 32)
                f10 = g(fl, 10, b'')
                if isinstance(f10, bytes) and len(f10) >= 16 and len(f10) % 16 == 0:
                    pay = unpad(AES.new(p2[:16], AES.MODE_CBC, iv=p2[16:32]).decrypt(f10))
            r['payload'] = pay
            r['fields'] = parse(pay) if pay else None
        else:
            r['method'] = g(fl, 3, b'')
            r['payload'] = g(fl, 10, b'')
            r['fields'] = parse(g(fl, 10, b'')) if isinstance(g(fl, 10, b''), bytes) else None
        return r

    def consume(self):
        b = bytes(self.pend)
        off = 0
        n = len(b)
        consumed = 0
        while off + 4 <= n:
            if b[off] == 0x20 and b[off + 1] == 0x00 and off + 0x18 <= n:
                tl = (b[off + 2] << 8) | b[off + 3]
                if 0x48 <= tl <= 0x20000:
                    if off + tl > n:
                        break
                    fr = b[off:off + tl]
                    AHUB.stats['frames'] += 1
                    pt = RS.dec_frame(fr, self.dk)
                    if pt:
                        AHUB.stats['bytes'] += len(pt)
                        AHUB.broadcast(pt)          # A-law 原始字节
                    off += tl
                    consumed = off
                    continue
            clen = int.from_bytes(b[off:off + 4], 'big')
            if 4 <= clen <= 0x200000:
                if off + clen > n:
                    break
                body = b[off + 4:off + clen]
                rx_log('asrc', body)
                try:                            # RPC 响应 -> 派发给等待者
                    fl = parse(body)
                    f11 = g(fl, 11, None)
                    f13 = g(fl, 13, None)
                    q = self.waiters.get(f11)
                    print('    [asrc] <- rpc len=%d f11=%s f13=%s f7=%s f3=%r'
                          % (clen, f11, f13, g(fl, 7, None), g(fl, 3, None)))
                    if q is not None:
                        q.put(self.decode_resp(body))
                    elif self.waiters:
                        print('    [asrc] rpc resp 未匹配 sess=%s f7=%s (等待 %s)'
                              % (f11, g(fl, 7, None), list(self.waiters)))
                except Exception as e:
                    print('    [asrc] rpc resp 解析失败 %r' % (e,))
                off += clen
                consumed = off
                continue
            break
        if consumed:
            del self.pend[:consumed]

    # ---- RPC ----
    def send_bytes(self, data):
        with self.wlock:
            try:
                self.sock.sendall(data)
                return True
            except Exception as e:
                print('    [asrc] send_bytes 失败 %r' % (e,))
                return False

    def send_rpc(self, method, payload=b'', wait=4.0):
        if self.sock is None:
            return None
        self.sess += 1
        sess = self.sess
        q = queue.Queue()
        self.waiters[sess] = q
        try:
            with self.wlock:
                self.sock.sendall(plain_rpc(method, payload, session=sess))
        except Exception as e:
            self.waiters.pop(sess, None)
            print('    [asrc] %s 发送失败 %r' % (method, e))
            return None
        try:
            r = q.get(timeout=wait)
        except queue.Empty:
            r = None
        self.waiters.pop(sess, None)
        return r

    # ---- 对讲上行（只负责发音频帧）----
    def send_frames(self, alaw, pace=True):
        """把 A-law(16kHz) 按 320B/20ms 切片发到音频连接。"""
        if self.sock is None or self.state != 'live':
            return 0
        pad = (-len(alaw)) % TALK_CHUNK
        data = alaw + (b'\xd5' * pad)      # 0xd5 = A-law 静音
        n = 0
        with self.wlock:
            for i in range(0, len(data), TALK_CHUNK):
                chunk = data[i:i + TALK_CHUNK]
                try:
                    self.sock.sendall(build_uplink_audio_frame(chunk, self.dk))
                except Exception as e:
                    print('    [asrc] talk 发送失败 %r' % (e,))
                    break
                n += 1
                if pace:
                    time.sleep(0.020)
        AHUB.stats['talk_frames'] = AHUB.stats.get('talk_frames', 0) + n
        return n

    def run(self):
        fails = 0
        while not self.stop_flag.is_set():
            t_conn = 0.0
            err = None
            try:
                self.state = 'connecting'
                self.connect()
                t_conn = time.time()
                self.state = 'live'
                fails = 0
                with self.wlock:
                    self.sock.sendall(plain_rpc('StartAudio', bytes.fromhex('0800'), session=1))
                print('    [asrc] StartAudio 已发送')
                last_ka = 0.0
                while not self.stop_flag.is_set():
                    now = time.time()
                    if now - last_ka >= 10:
                        last_ka = now
                        with self.wlock:
                            try:
                                self.sock.sendall(KEEPALIVE)
                            except Exception:
                                err = 'keepalive failed'
                                break
                    try:
                        d = self.sock.recv(262144)
                    except socket.timeout:
                        continue
                    if not d:
                        err = 'EOF'
                        break
                    self.pend += d
                    self.consume()
            except Exception as e:
                err = e
            finally:
                try:
                    self.sock.close()
                except Exception:
                    pass
            if self.stop_flag.is_set():
                break
            lived = (time.time() - t_conn) if t_conn else 0.0
            if lived > RECONNECT_STABLE:
                fails = 0
            else:
                fails += 1
            self.state = 'error'
            AHUB.stats['reconnects'] += 1
            delay = backoff_delay(fails)
            print('    [asrc] %r  存活 %.0fs -> %.0fs 后重连（连续失败 %d）'
                  % (err, lived, delay, fails), flush=True)
            self.stop_flag.wait(delay)


ASRC = AudioSource()


def _is_ack(r):
    """判断 send_rpc 的返回是不是设备 ACK。

    ★ 2026-10-15 重写（ASRC 现在也走 decode_resp 解密）：
      设备对**已识别**的方法必回一条加密响应，其 f18 解密 = 方法名（回显）；
      未被识别的方法**完全无响应**（超时）。
      因此判据：收到响应 且 无错误码 f7（0/缺省）= 成功。
      ★ 注意 f7=20145 = **payload 非法/缺失**（例如 StartTalkback 必须带 `0800`，
        空 payload 会被拒），不是鉴权问题。
    """
    if not r:
        return False
    return r.get('f7') in (None, 0)


def _start_talkback(via):
    """发送 StartTalkback，返回 (via_used, ack_bool, detail)"""
    payload = bytes.fromhex('0800')
    if via in ('asrc', 'auto'):
        r = ASRC.send_rpc('StartTalkback', payload, wait=4.0)
        if _is_ack(r):
            return 'asrc', True, (r.get('fields') or [])
        if via == 'asrc':
            return 'asrc', False, None
    if via in ('both',):
        a = ASRC.send_rpc('StartTalkback', payload, wait=4.0)
        b = SRC.send('StartTalkback', payload, wait=5.0)[1]
        return 'both', bool(_is_ack(a) or _is_ack(b)), None
    _, r = SRC.send('StartTalkback', payload, wait=5.0)
    return 'src', _is_ack(r), (r.get('fields') or [])


def _stop_talkback(via):
    payload = bytes.fromhex('0800')
    if via in ('asrc', 'auto'):
        r = ASRC.send_rpc('StopTalkback', payload, wait=4.0)
        if _is_ack(r):
            return
    if via in ('src', 'mix', 'auto'):
        SRC.send('StopTalkback', payload, wait=3.0)


def _rpc_both(method, payload, via, wait=3.0):
    """按 via 指定通道发一个普通 RPC，返回是否 ACK。"""
    if via in ('asrc', 'auto', 'both', 'mix'):
        r = ASRC.send_rpc(method, payload, wait=wait)
        if _is_ack(r):
            return True
    if via in ('src', 'mix'):
        _, r = SRC.send(method, payload, wait=wait)
        return bool(_is_ack(r))
    return False


def _get_vol():
    """读设备音量（GetDeviceVol 响应 payload = {f1=音量}），失败返回 None。"""
    try:
        r = ASRC.send_rpc('GetDeviceVol', bytes.fromhex('0800'), wait=3.0)
    except Exception:
        return None
    if not r:
        return None
    fl = r.get('fields')
    if not fl:
        return None
    return g(fl, 1, None)


def _set_vol(v):
    """设置设备音量（0..100）。payload = {f1=通道0, f2=音量}。"""
    try:
        return bool(_rpc_both('SetDeviceVol', pb_var(1, 0) + pb_var(2, int(v)),
                              'asrc', wait=3.0))
    except Exception:
        return False


def do_talk(alaw, pace=True, via='auto', pre_stopaudio=False, post_startaudio=False,
            mode='onekey', vol=None):
    """完整对讲流程：[StopAudio] -> StartTalkback -> 音频帧 -> StopTalkback -> [StartAudio]

    pre_stopaudio : 半双工假设开关。StartTalkback 前先发 StopAudio 停掉下行音频流，
        让设备把音频通道独占给上行对讲（喇叭/线路输入不再被下行占用）。
    post_startaudio: 收尾时补一条 StartAudio，恢复下行推流（否则喇叭监听会一直停着）。

    ★ 实测依据（2026-10-14）：
      - App 遥测操作流水里有 `StartTalkBack_cmd_success_3` / `StopTalkBack_cmd_success_3`
      - 设备 RPC 方法名为 `StartTalkback` / `StopTalkback`（小写 b），
        响应 payload {f1=101, f2=16000, f3=16, f4=1} = 16kHz/16bit/单声道
      - 上行音频帧 408B = 24B 头 + 48B env + 336B 密文(320B A-law + 16B PKCS#7)，
        头与 App 抓包**逐字节一致**（402/402 帧验证）；20ms/帧、50 帧/秒
      - 设备在音频连接上会回 ACK（f13=41 + echo f11），不是「无响应」
      - App 的对讲时段里设备**停止推下行音频**（抓包 scan: 26 帧连续全为上行）

    via 参数：
      'auto' : 先试 RPC+帧都走音频连接（与 App 完全一致），ACK 失败才退回视频连接
      'asrc' : StartTalkback + 音频帧都走音频连接（= App 布局）
      'both' : StartTalkback 同时在两条连接发，帧走音频连接
      'mix'  : StartTalkback 走视频连接 + 帧走音频连接
      'src'  : 全部走视频连接
    """
    res = {'ok': False, 'via': via, 'start': None, 'frames': 0, 'stop': None}
    if SRC.state != 'live':
        res['reason'] = 'video connection not live'
        return res
    if ASRC.state != 'live':
        res['reason'] = 'audio connection not live'
        return res

    # ★ 音量：设备出厂/被 App 调过之后音量可能是 0，喇叭就永远不出声。
    #   默认确保音量 >= DEFAULT_TALK_VOL，可用 ?vol=N 覆盖。
    target_vol = DEFAULT_TALK_VOL if vol is None else max(0, min(100, int(vol)))
    cur = _get_vol()
    res['vol_before'] = cur
    if target_vol > 0 and (cur is None or cur < target_vol):
        res['vol_set'] = _set_vol(target_vol)
        time.sleep(0.2)
        res['vol_after'] = _get_vol()

    if pre_stopaudio:
        res['pre_stop'] = _rpc_both('StopAudio', bytes.fromhex('0800'), via, wait=3.0)
        time.sleep(0.10)

    vu, acked, fields = _start_talkback(via)
    res['via_used'] = vu
    res['start'] = 'ack' if acked else 'no-ack'
    if fields:
        res['start_fields'] = [[f, wt, (v.hex() if isinstance(v, bytes) else v)]
                               for f, wt, v in fields][:8]

    ASRC.talk_state = 'talking'
    time.sleep(0.20)
    res['frames'] = send_frames_via('asrc' if vu != 'src' else 'src',
                                    alaw, pace=pace, mode=mode)
    res['mode'] = mode

    _stop_talkback(vu)
    res['stop'] = 'sent'
    ASRC.talk_state = 'idle'
    if post_startaudio:
        res['post_start'] = _rpc_both('StartAudio', bytes.fromhex('0800'), vu, wait=3.0)
    res['ok'] = res['frames'] > 0
    return res


def send_frames_via(via, alaw, pace=True, mode='onekey'):
    """按 320B/20ms 切片，发到指定连接"""
    pad = (-len(alaw)) % TALK_CHUNK
    data = alaw + (b'\xd5' * pad)
    n = 0
    for i in range(0, len(data), TALK_CHUNK):
        chunk = data[i:i + TALK_CHUNK]
        if via == 'src':
            frame = build_uplink_audio_frame(chunk, SRC.dk, mode=mode)
            if not SRC.send_bytes(frame):
                break
        else:
            frame = build_uplink_audio_frame(chunk, ASRC.dk, mode=mode)
            if not ASRC.send_bytes(frame):
                break
        n += 1
        if pace:
            time.sleep(0.020)
    AHUB.stats['talk_frames'] = AHUB.stats.get('talk_frames', 0) + n
    return n


# ---------------------------------------------------------------- HTTP
class _Sink:
    def __init__(self, w):
        self.w = w

    def write(self, b):
        self.w(b)
        return len(b)

    def flush(self):
        pass

    def seekable(self):
        return False


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _hdr(self, ct, extra=None):
        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        # 用 ?token= 访问过页面时，顺手把令牌种进 Cookie，之后从图标进来就不用带令牌
        if getattr(self, '_ck_pending', False) and ACCESS_TOKEN:
            self.send_header('Set-Cookie', self._cookie_hdr())
            self._ck_pending = False
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    # ---- 访问令牌（HQ_TOKEN）------------------------------------------------
    def _cookie_hdr(self):
        return ('hq_token=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax'
                % ACCESS_TOKEN)

    def _tok_from_q(self, q):
        v = (q.get('token') or [None])[0]
        return v if v and v == ACCESS_TOKEN else None

    def _tok_ok(self, q):
        """校验访问令牌。未配置令牌时一律放行（仅限可信内网）。"""
        if not ACCESS_TOKEN:
            return True
        if self._tok_from_q(q):
            return True
        auth = (self.headers.get('Authorization') or '').strip()
        if auth[:7].lower() == 'bearer ' and auth[7:].strip() == ACCESS_TOKEN:
            return True
        if (self.headers.get('X-Token') or '').strip() == ACCESS_TOKEN:
            return True
        # Cookie（便于浏览器直接打开页面）
        ck = self.headers.get('Cookie') or ''
        for part in ck.split(';'):
            if '=' in part:
                k, _, val = part.strip().partition('=')
                if k == 'hq_token' and val == ACCESS_TOKEN:
                    return True
        return False

    def _wants_html(self):
        """浏览器在要「页面」而不是图片/流/API？"""
        if 'text/html' in (self.headers.get('Accept') or ''):
            return True
        return urlparse(self.path).path in ('/', '/index.html', '/login')

    def _deny(self):
        """未授权。

        浏览器打开页面 → 直接给**登录页**（否则飞牛图标点进来只会看到一段 401 文本）；
        图片/流/API 请求 → 保持 401 文本，别往 <img> 里塞 HTML。
        """
        if self._wants_html():
            b = render_login('').encode()
            self.send_response(401)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(b)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(b)
            except Exception:
                pass
            return
        b = ('访问被拒：缺少或错误的访问令牌 (401)\n'
             '  方式一：URL 加 ?token=<TOKEN>\n'
             '  方式二：请求头 Authorization: Bearer <TOKEN> 或 X-Token: <TOKEN>\n'
             '  方式三：浏览器访问 /login 输入令牌（会记住一年）\n'
             '  令牌由部署时的环境变量 HQ_TOKEN 决定。\n').encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('WWW-Authenticate', 'Bearer realm="hqcam"')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    # ---- 登录 / 登出 ----
    def _html(self, code, body, extra=None):
        b = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Connection', 'close')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def _redirect(self, to, extra=None):
        self.send_response(303)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def serve_login_get(self, q):
        # 令牌没启用、或已经带着正确令牌 → 不用登录
        if not ACCESS_TOKEN or self._tok_ok(q):
            return self._redirect('/')
        self._html(200, render_login(''))

    def serve_login_post(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        tok = ''
        if 'application/x-www-form-urlencoded' in (self.headers.get('Content-Type') or ''):
            f = parse_qs(body.decode('utf-8', 'ignore'))
            tok = (f.get('token') or [''])[0]
        else:
            tok = body.decode('utf-8', 'ignore').strip()
        if ACCESS_TOKEN and tok == ACCESS_TOKEN:
            LOGIN_FAIL['n'] = 0
            return self._redirect('/', {'Set-Cookie': self._cookie_hdr()})
        # 失败：轻微递增延时，够挡住脚本爆破，又不影响手输
        LOGIN_FAIL['n'] = min(LOGIN_FAIL['n'] + 1, 30)
        time.sleep(min(2.0, 0.3 + LOGIN_FAIL['n'] * 0.15))
        print('[login] 令牌错误（连续第 %d 次）来自 %s' % (LOGIN_FAIL['n'],
                                                          self.client_address[0]), flush=True)
        self._html(401, render_login('令牌不正确'))

    def serve_logout(self):
        self._redirect('/login', {
            'Set-Cookie': 'hq_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'})

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        # 登录端点必须在令牌校验之前（否则永远进不去）
        if p == '/login':
            return self.serve_login_get(q)
        if p == '/logout':
            return self.serve_logout()
        if not self._tok_ok(q):
            return self._deny()
        if ACCESS_TOKEN and self._tok_from_q(q):
            self._ck_pending = True
        try:
            if p in ('/', '/index.html'):
                self.serve_page()
            elif p == '/status.json':
                self.serve_status()
            elif p == '/live.hevc':
                self.serve_hevc()
            elif p == '/live.ts':
                self.serve_ts()
            elif p == '/av.ts':
                self.serve_av_ts()
            elif p == '/mjpeg':
                self.serve_mjpeg(q)
            elif p == '/audio.wav':
                self.serve_audio_wav()
            elif p == '/audio.alaw':
                self.serve_audio_alaw()
            elif p == '/snapshot.jpg':
                self.serve_snap()
            elif p == '/ptz':
                self.serve_ptz(q)
            elif p == '/talk/tone':
                self.serve_talk_tone(q)
            elif p == '/talk/probe':
                self.serve_talk_probe(q)
            elif p == '/vol':
                self.serve_vol(q)
            elif p == '/rpc':
                self.serve_rpc(q)
            elif p == '/rx.json':
                self.serve_rx(q)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == '/login':
            return self.serve_login_post()
        if not self._tok_ok(q):
            return self._deny()
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            if p == '/talk':                 # 原始 A-law 16kHz 单声道
                self.serve_talk(body, 'alaw', q)
            elif p == '/talk.wav':           # WAV 文件
                self.serve_talk(body, 'wav', q)
            elif p == '/talk/tone':
                self.serve_talk_tone(q)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 页面 ----
    def serve_page(self):
        """控制台（预览 / 云台 / 音量 / 对讲 / 状态）。页面本体在 webui.py。"""
        b = webui.APP_HTML.encode()
        self._hdr('text/html; charset=utf-8', {'Content-Length': str(len(b))})
        self.wfile.write(b)


    def serve_status(self):
        s = dict(HUB.stats)
        s['state'] = SRC.state
        s['uptime'] = round(time.time() - s['start'], 1)
        s['clients'] = len(HUB.subs)
        s['audio'] = {'state': ASRC.state, 'frames': AHUB.stats['frames'],
                      'bytes': AHUB.stats['bytes'], 'clients': len(AHUB.subs),
                      'codec': 'alaw', 'rate': AudioSource.ALAW_RATE,
                      'sk': AHUB.stats['sk'], 'relay': AHUB.stats['relay'],
                      'talk_frames': AHUB.stats.get('talk_frames', 0)}
        b = json.dumps(s, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    # ---- 流 ----
    def serve_hevc(self):
        self._hdr('video/hevc')
        q = HUB.subscribe()
        try:
            while True:
                au = q_next(q, HUB)
                if au is _END:
                    break
                if au is None:
                    continue
                self.wfile.write(au)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            HUB.unsubscribe(q)

    def serve_ts(self):
        self._hdr('video/mp2t')
        q = HUB.subscribe()
        out = None
        try:
            sink = _Sink(lambda b: self.wfile.write(bytes(b)))
            out = av.open(sink, 'w', format='mpegts')
            st = out.add_stream('hevc', rate=Fraction(25, 1))
            st.width, st.height = 2880, 1620
            st.time_base = Fraction(1, 90000)
            i = 0
            while True:
                au = q_next(q, HUB)
                if au is _END:
                    break
                if au is None:
                    continue
                pk = av.Packet(au)
                pk.stream = st
                pk.pts = pk.dts = int(i * 3600)
                i += 1
                out.mux(pk)
        except Exception as e:
            print('[ts] %r' % (e,))
        finally:
            HUB.unsubscribe(q)
            try:
                out.close()
            except Exception:
                pass

    def serve_av_ts(self):
        """★ 音视频合流 MPEG-TS（HEVC 2880x1620@25fps + AAC 16kHz 单声道）。

        视频走 HUB（HEVC 访问单元，流拷贝），音频走 AHUB（A-law -> 16bit PCM -> AAC）。
        HA / Frigate / VLC 用一条流即可同时拿到画面与声音。
        """
        self._hdr('video/mp2t')
        qv = HUB.subscribe()
        qa = AHUB.subscribe()
        out = None
        try:
            sink = _Sink(lambda b: self.wfile.write(bytes(b)))
            out = av.open(sink, 'w', format='mpegts')
            sv = out.add_stream('hevc', rate=Fraction(25, 1))
            sv.width, sv.height = 2880, 1620
            sv.time_base = Fraction(1, 90000)
            rate = AudioSource.ALAW_RATE
            sa = out.add_stream('aac', rate=rate, layout='mono', bit_rate=64000)
            sa.time_base = Fraction(1, rate)
            iv = 0
            ia = 0
            abuf = bytearray()
            while True:
                got = False
                # --- 视频：每个 HEVC AU 一包 ---
                while True:
                    try:
                        au = qv.get_nowait()
                    except queue.Empty:
                        break
                    if au is None:
                        return
                    pk = av.Packet(au)
                    pk.stream = sv
                    pk.pts = pk.dts = iv * 3600
                    iv += 1
                    out.mux(pk)
                    got = True
                # --- 音频：收 A-law 字节 ---
                while True:
                    try:
                        d = qa.get_nowait()
                    except queue.Empty:
                        break
                    if d is None:
                        return
                    abuf += d
                    got = True
                # AAC 固定 1024 样本/帧
                while len(abuf) >= 1024:
                    chunk = bytes(abuf[:1024])
                    del abuf[:1024]
                    fr = av.AudioFrame(format='s16', layout='mono', samples=1024)
                    fr.sample_rate = rate
                    fr.pts = ia * 1024
                    ia += 1
                    fr.planes[0].update(b''.join(ALAW_PCM[x] for x in chunk))
                    for pk in sa.encode(fr):
                        out.mux(pk)
                if not got:
                    # 上游离线则主动断开（否则这个连接会永久挂住，录像容器每次重连都漏一个）
                    if time.time() - max(HUB.last_data, AHUB.last_data) > STREAM_IDLE:
                        break
                    time.sleep(0.02)
        except Exception as e:
            print('[av.ts] %r' % (e,))
        finally:
            HUB.unsubscribe(qv)
            AHUB.unsubscribe(qa)
            try:
                out.close()
            except Exception:
                pass

    # ---- 音频 ----
    def serve_audio_alaw(self):
        """原始 G.711 A-law 字节流（16kHz 单声道）"""
        self._hdr('audio/basic')
        q = AHUB.subscribe()
        try:
            while True:
                d = q_next(q, AHUB)
                if d is _END:
                    break
                if d is None:
                    continue
                self.wfile.write(d)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            AHUB.unsubscribe(q)

    def serve_audio_wav(self):
        """实时 WAV（16kHz/16bit/单声道），VLC / ffplay / HA 可直接播放"""
        rate = AudioSource.ALAW_RATE
        hdr = (b'RIFF' + struct.pack('<I', 0xFFFFFFFF) + b'WAVE'
               + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16)
               + b'data' + struct.pack('<I', 0xFFFFFFFF))
        self._hdr('audio/wav')
        self.wfile.write(hdr)
        q = AHUB.subscribe()
        try:
            while True:
                d = q_next(q, AHUB)
                if d is _END:
                    break
                if d is None:
                    continue
                self.wfile.write(b''.join(struct.pack('<h', alaw2lin(x)) for x in d))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            AHUB.unsubscribe(q)

    def serve_snap(self):
        SNAP['want'].set()
        t0 = time.time()
        while time.time() - t0 < 8:
            with SNAP['lock']:
                j = SNAP['jpg']
            if j and time.time() - SNAP['ts'] < 5:
                self._hdr('image/jpeg', {'Content-Length': str(len(j))})
                self.wfile.write(j)
                return
            time.sleep(0.2)
        self.send_error(503, 'no snapshot yet' + (' (%s)' % SNAP['err'] if SNAP.get('err') else ''))

    def serve_mjpeg(self, q):
        """MJPEG 预览流（multipart/x-mixed-replace，浏览器 <img> 原生支持）。

        为什么不用 /av.ts 做预览：浏览器基本不支持 HEVC-in-TS，画面会黑屏。
        MJPEG 用的是解码后的缩略图（宽 960），2fps 在 NAS 上 CPU 占用很低。

        ★ 2026-10-22 修复：原来这里是个无出口的 while True ——
          上游掉线时永远等不到帧，只 sleep(0.5) 继续等，既不检测客户端断开
          也不检测上游状态。浏览器每次刷新/重开标签都会漏一个线程，29 小时
          累积 1655 个，CPU 被 GIL 争抢打满。现在：
            1) 每轮探测客户端是否已断开（select + MSG_PEEK）
            2) 上游离线时推"CAMERA OFFLINE"占位帧，不再空等
            3) 单连接最长 MJPEG_MAX_SECS 秒，兜底回收
        """
        try:
            fps = float((q.get('fps') or ['2'])[0])
        except ValueError:
            fps = 2.0
        fps = max(0.5, min(10.0, fps))
        try:
            w = int((q.get('w') or ['960'])[0])
        except ValueError:
            w = 960
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        last = 0.0
        t_start = time.time()
        interval = 1.0 / fps
        try:
            while True:
                if peer_gone(self.connection):
                    break
                if time.time() - t_start > MJPEG_MAX_SECS:
                    break
                SNAP['want'].set()
                t0 = time.time()
                got = None
                while time.time() - t0 < min(5.0, interval + 0.5):
                    with SNAP['lock']:
                        j = SNAP['small'] if w <= 1280 else SNAP['jpg']
                        ts = SNAP['ts']
                    if j and ts > last:
                        got, last = j, ts
                        break
                    if peer_gone(self.connection):
                        return
                    time.sleep(0.05)
                if got is None:
                    if time.time() - SNAP['ts'] > 3:
                        got = offline_jpeg(min(w, 960))
                    if not got:
                        time.sleep(0.5)
                        continue
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n'
                                 b'Content-Length: ' + str(len(got)).encode() + b'\r\n\r\n')
                self.wfile.write(got)
                self.wfile.write(b'\r\n')
                self.wfile.flush()
                time.sleep(max(0.0, interval - (time.time() - t0)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    # ---- 对讲上行 ----
    def serve_talk(self, body, kind, q=None):
        q = q or {}
        if kind == 'wav':
            alaw = wav_to_alaw(body)
            if alaw is None:
                self.send_error(400, 'bad wav')
                return
        else:
            alaw = body
        if not alaw:
            self.send_error(400, 'empty audio')
            return
        via = (q.get('via') or ['auto'])[0]
        mode = (q.get('mode') or ['onekey'])[0]
        if mode not in ('onekey', 'dk', 'plain'):
            mode = 'onekey'
        pso = _flag(q, 'pre_stopaudio')
        psa = _flag(q, 'post_startaudio')
        vol = (q.get('vol') or [None])[0]
        vol = int(vol) if vol is not None and str(vol).isdigit() else None
        r = do_talk(alaw, via=via, pre_stopaudio=pso, post_startaudio=psa,
                    mode=mode, vol=vol)
        r['alaw_bytes'] = len(alaw)
        r['seconds'] = round(len(alaw) / float(TALK_RATE), 2)
        r['note'] = 'StartTalkback -> 320B/20ms A-law 帧 -> StopTalkback'
        b = json.dumps(r, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    def serve_talk_probe(self, q):
        """对讲状态探针：只发 RPC，不发音频帧，采样下行音频帧速率。

        依据：App 抓包里对讲时段设备**停止推下行音频**（scan 26 帧连续全为上行）。
        如果我们的 StartTalkback 真的被设备接受，下行音频应当停涨。
        """
        via = (q.get('via') or ['asrc'])[0]
        hold = float((q.get('hold') or ['4.0'])[0])
        seq = []

        def snap(tag):
            seq.append({'tag': tag, 't': round(time.time() - s0, 2),
                        'audio_frames': AHUB.stats['frames'],
                        'audio_bytes': AHUB.stats['bytes']})
        s0 = time.time()
        snap('t0')
        time.sleep(1.0)
        snap('t1')
        r = None
        try:
            vu, acked, fields = _start_talkback(via)
            r = {'via_used': vu, 'ack': acked,
                 'fields': [[f, wt, (v.hex() if isinstance(v, bytes) else v)]
                            for f, wt, v in (fields or [])][:8]}
        except Exception as e:
            r = {'error': repr(e)}
        time.sleep(0.3)
        snap('start_ack')
        t = 0.0
        while t < hold:
            time.sleep(0.5)
            t += 0.5
            snap('talking')
        try:
            _stop_talkback(v)
        except Exception:
            pass
        time.sleep(0.3)
        snap('stop_ack')
        t = 0.0
        while t < 2.0:
            time.sleep(0.5)
            t += 0.5
            snap('after_stop')
        # 速率
        for i in range(1, len(seq)):
            seq[i]['d_frames'] = seq[i]['audio_frames'] - seq[i - 1]['audio_frames']
            seq[i]['d_t'] = round(seq[i]['t'] - seq[i - 1]['t'], 2)
            seq[i]['fps'] = round(seq[i]['d_frames'] / max(seq[i]['d_t'], 1e-6), 1)
        out = {'ok': True, 'via': via, 'start': r, 'samples': seq,
               'note': 'start_ack/talking 段 fps 掉到 ~0 => 设备确认进入对讲并停了下行音频'}
        b = json.dumps(out, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    def serve_talk_tone(self, q):
        hz = int((q.get('hz') or ['1000'])[0])
        ms = int((q.get('ms') or ['1000'])[0])
        via = (q.get('via') or ['auto'])[0]
        mode = (q.get('mode') or ['onekey'])[0]
        if mode not in ('onekey', 'dk', 'plain'):
            mode = 'onekey'
        pso = _flag(q, 'pre_stopaudio')
        psa = _flag(q, 'post_startaudio')
        ms = max(50, min(ms, 20000))
        amp = float((q.get('amp') or ['0.35'])[0])
        amp = max(0.01, min(amp, 0.99))
        vol = (q.get('vol') or [None])[0]
        vol = int(vol) if vol is not None and str(vol).isdigit() else None
        alaw = alaw_tone(hz, ms, amp=amp)
        r = do_talk(alaw, via=via, pre_stopaudio=pso, post_startaudio=psa,
                    mode=mode, vol=vol)
        r.update({'hz': hz, 'ms': ms, 'alaw_bytes': len(alaw), 'amp': amp})
        b = json.dumps(r, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    # ---- 控制 ----
    def serve_ptz(self, q):
        d = (q.get('dir') or ['stop'])[0].lower()
        ms = int((q.get('ms') or ['400'])[0])
        ch = int((q.get('ch') or ['0'])[0])
        code = PTZ.get(d)
        if code is None:
            self.send_error(400, 'bad dir %s' % d)
            return
        p1 = int((q.get('p1') or ['0'])[0])
        p2 = int((q.get('p2') or ['0'])[0])
        res = []
        if d != 'stop':
            pl = plain_payload(ch, code, p1, p2)
            _, r = SRC.send('PtzCtrl', pl, wait=3.0)
            res.append(('move', d, code, pl.hex(), r))
            time.sleep(ms / 1000.0)
        pl = plain_payload(ch, PTZ['stop'], 0, 0)
        _, r2 = SRC.send('PtzCtrl', pl, wait=3.0)
        res.append(('stop', 'stop', 100, pl.hex(), r2))
        out = []
        for nm, dd, cc, hx, r in res:
            out.append({'action': nm, 'dir': dd, 'code': cc, 'payload': hx,
                        'f7': r.get('f7') if r else None,
                        'resp': (r.get('payload').hex() if r and r.get('payload') else None)})
        b = json.dumps(out, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    def serve_rx(self, q):
        who = (q.get('who') or ['src'])[0]
        items = RXLOG.get(who) or []
        out = [{'t': t, 'hex': b.hex()} for t, b in items]
        data = json.dumps({'who': who, 'n': len(out), 'items': out},
                          ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(data))})
        self.wfile.write(data)

    def serve_vol(self, q):
        """设备音量读/写。

        ★ 2026-10-15 发现：`GetDeviceVol` 一开始返回 {f1=0} —— **喇叭音量是 0（静音）**，
          这正是「StartTalkback 成功 + 上行帧格式正确但喇叭不出声」的根因。
        `SetDeviceVol` payload = {f1=通道(0), f2=音量 0..100}，响应无 payload；
        `GetDeviceVol` 响应 payload = {f1=当前音量}。
        """
        want = (q.get('v') or q.get('vol') or [None])[0]
        res = {'read_before': _get_vol()}
        if want is not None:
            try:
                v = max(0, min(100, int(want)))
            except ValueError:
                v = None
            if v is not None:
                res['set_ok'] = _rpc_both('SetDeviceVol', pb_var(1, 0) + pb_var(2, v),
                                          'asrc', wait=3.0)
                res['set_to'] = v
                time.sleep(0.2)
        res['read_after'] = _get_vol()
        res['ok'] = True
        b = json.dumps(res, ensure_ascii=False).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)

    def serve_rpc(self, q):
        m = (q.get('method') or [''])[0]
        if not m:
            self.send_error(400, 'need method')
            return
        hx = (q.get('payload') or [''])[0]
        pl = bytes.fromhex(hx) if hx else b''
        via = (q.get('via') or ['src'])[0]
        if via == 'asrc':
            r = ASRC.send_rpc(m, pl, wait=4.0)
            if r is not None:
                r = {'f7': r.get('f7'), 'payload': r.get('payload'),
                     'method': r.get('method'), 'fields': r.get('fields')}
        else:
            _, r = SRC.send(m, pl, wait=4.0)
        d = {'method': m, 'payload': hx, 'via': via,
             'f7': r.get('f7') if r else None,
             'resp_method': r.get('method') if r else None,
             'resp_payload': (r.get('payload').hex() if r and r.get('payload') else None),
             'fields': ([[f, wt, (v.hex() if isinstance(v, bytes) else v)]
                         for f, wt, v in r['fields']] if r and r.get('fields') else None)}
        b = json.dumps(d, ensure_ascii=False, default=str).encode()
        self._hdr('application/json', {'Content-Length': str(len(b))})
        self.wfile.write(b)


def pb_var(f, v):
    if v < 0:
        v += 1 << 64
    out = b''
    while v >= 0x80:
        out += bytes([(v & 0x7f) | 0x80]); v >>= 7
    return bytes([f << 3 | 0]) + out + bytes([v])


def plain_payload(ch, code, p1=0, p2=0):
    """PtzCtrl_RpcRequest = {f1=ch_no, f2=code, f6=para1, f7=para2}"""
    return pb_var(1, ch) + pb_var(2, code) + pb_var(6, p1) + pb_var(7, p2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--host', default='0.0.0.0')
    a = ap.parse_args()

    # ★ 容器里本进程就是 PID 1，而 Docker 的 HEALTHCHECK 每 30 秒会在容器内
    #   exec 一个 python 进程，它的父进程就是 PID 1。python 不会 wait 这些
    #   非 subprocess 创建的进程 -> 每个 healthcheck 都留一个僵尸。
    #   置 SIGCHLD=SIG_IGN 让内核自动回收（本服务不依赖 subprocess 的返回值）。
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        print('[*] SIGCHLD=SIG_IGN：子进程自动回收（防僵尸堆积）')
    except Exception as e:
        print('[!] 设置 SIGCHLD 失败：%r' % (e,))

    print('[*] 启动取流源 ...')
    SRC.start()
    ASRC.start()
    print('[*] HTTP 网关 http://127.0.0.1:%d/' % a.port)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    SRC.stop_flag.set()
    ASRC.stop_flag.set()


if __name__ == '__main__':
    main()
