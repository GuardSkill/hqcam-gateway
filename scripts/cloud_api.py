# -*- coding: utf-8 -*-
"""
海雀摄像头 v5 云 API 直连客户端 — 本地连接 PSK 获取 (纯 stdlib)

  UserLogin            -> access_token
  GetDevLocalModeConf  -> LocalConf.localSecurityKey  (本地连接 PSK)

★ 个人信息（账号 / 密码 / 设备 ID）**不在本文件内**，一律从 hq_config 读取，
  后者优先取环境变量（HQ_USER / HQ_PASS / HQ_DEVICE ...），其次 config.json。
  这样镜像可以公开发布，凭证只存在于部署机。

关键坑:
  1) JSON 必须紧凑序列化 separators=(',',':')，否则服务端返回 404 / code=2002
  2) 不能走环境代理(直连)，且 urllib 会被 WAF 拒，用 http.client 手动发
"""
import http.client, ssl, json, time, uuid, gzip, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hq_config import cfg

HOST = 'app-api-32.ihaique.net'


class Api:
    def __init__(self, user=None, pwd=None, app_did=None, client=None, ver=None):
        # 允许显式传入（多账号场景）；否则走全局配置
        self.user = user if user is not None else cfg.user
        self.pwd = pwd if pwd is not None else cfg.pwd
        self.app_did = app_did if app_did is not None else cfg.app_did
        self.client = client if client is not None else cfg.client
        self.ver = int(ver if ver is not None else cfg.ver)
        self.cookies = {}
        self._ctx = ssl.create_default_context()

    def _req(self, path, payload, token='0.notoken'):
        conn = http.client.HTTPSConnection(HOST, 443, context=self._ctx, timeout=25)
        hdrs = {
            'Content-Type': 'application/json; charset=UTF-8',
            'Accept': '*/*',
            'User-Agent': 'okhttp/4.11.0',
            'Accept-Encoding': 'gzip',
            'Connection': 'Keep-Alive',
            'Host': HOST,
        }
        if self.cookies:
            hdrs['Cookie'] = '; '.join('%s=%s' % kv for kv in self.cookies.items())
        body = json.dumps(payload, separators=(',', ':')).encode()
        url = '%s?client_id=%s&token=%s' % (path, self.client, token)
        conn.request('POST', url, body=body, headers=hdrs)
        r = conn.getresponse()
        raw = r.read()
        for k, v in r.getheaders():
            if k.lower() == 'set-cookie':
                nv = v.split(';')[0]
                if '=' in nv:
                    n, val = nv.split('=', 1)
                    self.cookies[n] = val
        conn.close()
        if r.status != 200:
            raise RuntimeError('HTTP %s %s' % (r.status, raw[:200]))
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
        return json.loads(raw.decode('utf8'))

    def _payload(self, cmd, body):
        now = int(time.time() * 1000)
        return {"body": body, "dana_time": now, "app_did": self.app_did,
                "app_request_tracer": str(uuid.uuid4()), "cmd": cmd,
                "page": 0, "page_size": 0, "request_id": now % 100000,
                "version_code": self.ver}

    def login(self):
        if not (self.user and self.pwd and self.app_did):
            cfg.require()
        body = {"app_core": 1, "app_did": self.app_did, "app_lang": "zh-Hans",
                "client_id": self.client, "client_type": 2,
                "ddp_cipher_suites": [6, 7], "location": ",", "phone_code": "86",
                "push_id": "", "terminal_check": 0, "terminal_device_id": self.app_did,
                "terminal_device_name": cfg.terminal_name,
                "terminal_device_type": "3414",
                "user_name": self.user, "user_pass": self.pwd, "user_type": 2}
        return self._req('/v5/user', self._payload('UserLogin', body))

    def get_local_conf(self, token, devs):
        return self._req('/v5/device',
                         self._payload('GetDevLocalModeConf', {"deviceIds": devs}),
                         token=token)

    def report_local_mode(self, token, dev, mode):
        return self._req('/v5/device',
                         self._payload('ReportDevLocalMode',
                                       {"deviceId": dev, "localMode": mode}),
                         token=token)


# 向后兼容：老脚本 `from cloud_api import Api, DEVICE`
DEVICE = cfg.device
APPDID = cfg.app_did
USER = cfg.user
PASS = cfg.pwd


def main():
    cfg.require()
    api = Api()
    r = api.login()
    b = r.get('body') or {}
    tok = b.get('access_token') or ''
    print('[login] code=%s user=%s' % (r.get('code'), b.get('user_name')))
    print('[login] access_token = %s' % (tok[:24] + '...' if len(tok) > 24 else tok))
    print('[login] secret_key   = %s' % ('(已获取)' if b.get('secret_key') else '(无)'))
    r2 = api.get_local_conf(tok, [cfg.device])
    print('\n[GetDevLocalModeConf]')
    print(json.dumps(r2, ensure_ascii=False, indent=2)[:2000])
    try:
        c = r2['body']['confs'][0]
        print('\n*** localMode=%s online=%s deviceId=%s'
              % (c.get('localMode'), c.get('online'), c.get('deviceId')))
    except Exception as e:
        print('parse err', e)


if __name__ == '__main__':
    main()
