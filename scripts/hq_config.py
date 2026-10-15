# -*- coding: utf-8 -*-
"""海雀摄像头网关 · 集中配置

**所有个人信息一律从外部注入**，代码里不留任何账号/设备信息，
以便镜像可以公开发布（GHCR），个人信息只存在于部署机的 .env / config.json。

取值优先级（先到先用）：
  1. 环境变量（推荐，docker-compose 的 .env 会注入）
  2. config.json（可选，路径由 HQ_CONFIG 指定；默认 <项目根>/config.json）
  3. 内置的非敏感默认值（APPDID / CLIENT / VER 是 App 常量，不是个人信息）

环境变量一览
------------
  必填
    HQ_USER        云账号（手机号）
    HQ_PASS        云账号密码
    HQ_DEVICE      设备 ID（32 位）
  可选
    HQ_APPDID      App 设备 ID（默认用 App 内置常量）
    HQ_CLIENT      客户端 ID（默认用 App 内置常量）
    HQ_VER         客户端版本号（默认用 App 内置常量）
    HQ_TEMPLATE    RelayInit 模板路径（默认 <项目根>/assets/relayinit_template.bin）
    HQ_PORT        HTTP 端口（默认 8899）
    HQ_HOST        监听地址（默认 0.0.0.0）
    HQ_TOKEN       HTTP 访问令牌（**强烈建议设置**；留空 = 不鉴权，仅限内网）
    HQ_LOGIN_RETRY 登录失败重试次数（默认 3）
    HQ_CONFIG      config.json 路径（默认 <项目根>/config.json）

用法
----
    from hq_config import cfg
    cfg.user, cfg.pwd, cfg.device, cfg.app_did
"""
import os
import json

# ---------------------------------------------------------------- 常量（非敏感）
# 以下均为 App 客户端内置常量（已确认出现在 haique APK 的 dex 里），
# 全体用户共用，不属于个人信息。
DEFAULT_CLIENT = '52f7eaf24f5371c4fb02ce54d046b8d1'
DEFAULT_VER = 31216          # 对应 App 版本 3.12.16

# 注意：App 设备标识（app_did / terminal_device_id）**不是** APK 常量，
# 而是 App 首次启动时生成并绑定到账号的，属于个人信息 —— 因此没有默认值，
# 必须通过 HQ_APPDID 注入。

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 项目根（scripts 的上一级）


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class Config(object):
    """配置读取器（惰性、可重复读，便于容器里 reload）"""

    def __init__(self):
        self.reload()

    # -- 内部 ------------------------------------------------------------
    @staticmethod
    def _env(*names):
        for n in names:
            v = os.environ.get(n)
            if v is not None and v != '':
                return v
        return None

    def reload(self):
        cfg_path = self._env('HQ_CONFIG') or os.path.join(ROOT, 'config.json')
        self._file = _read_json(cfg_path) if os.path.exists(cfg_path) else {}
        self._cfg_path = cfg_path

    def _get(self, env_names, json_key, default=None):
        v = self._env(*env_names)
        if v is not None:
            return v
        v = self._file.get(json_key)
        if v is not None and v != '':
            return v
        return default

    # -- 必填 ------------------------------------------------------------
    @property
    def user(self):
        return self._get(('HQ_USER',), 'user')

    @property
    def pwd(self):
        return self._get(('HQ_PASS', 'HQ_PASSWORD'), 'pass')

    @property
    def device(self):
        return self._get(('HQ_DEVICE', 'HQ_DEVICE_ID'), 'device')

    # -- 可选（有内置默认）------------------------------------------------
    @property
    def app_did(self):
        """App 设备标识（个人信息，无默认值，必须由 HQ_APPDID 注入）"""
        return self._get(('HQ_APPDID', 'HQ_APP_DID'), 'app_did')

    @property
    def client(self):
        return self._get(('HQ_CLIENT', 'HQ_CLIENT_ID'), 'client', DEFAULT_CLIENT)

    @property
    def ver(self):
        try:
            return int(self._get(('HQ_VER',), 'ver', DEFAULT_VER))
        except Exception:
            return DEFAULT_VER

    @property
    def terminal_name(self):
        """登录时上报的终端机型名（纯遥测，非必填；默认通用值）"""
        return self._get(('HQ_TERMINAL_NAME',), 'terminal_name', 'Android')

    @property
    def template(self):
        p = self._get(('HQ_TEMPLATE',), 'template')
        return p or os.path.join(ROOT, 'assets', 'relayinit_template.bin')

    @property
    def port(self):
        try:
            return int(self._get(('HQ_PORT', 'PORT'), 'port', 8899))
        except Exception:
            return 8899

    @property
    def host(self):
        return self._get(('HQ_HOST',), 'host', '0.0.0.0')

    @property
    def token(self):
        """HTTP 访问令牌（可选但**强烈建议**）。

        设置后，网关所有端点都必须携带令牌：
          - URL 查询参数：`?token=<TOKEN>`
          - 或请求头：`Authorization: Bearer <TOKEN>` / `X-Token: <TOKEN>`

        留空 = 完全不做鉴权（仅适合完全可信的内网）。此时同网段任何人
        都能看画面、控云台、喊话，且 `/status.json` 会明文吐出会话 sk。
        """
        return self._get(('HQ_TOKEN', 'HQ_ACCESS_TOKEN', 'HQ_API_TOKEN'), 'token')

    # -- 校验 ------------------------------------------------------------
    def missing(self):
        """返回缺失的必填项名称列表（空 = 配置完整）"""
        miss = []
        if not self.user:
            miss.append('HQ_USER')
        if not self.pwd:
            miss.append('HQ_PASS')
        if not self.device:
            miss.append('HQ_DEVICE')
        if not self.app_did:
            miss.append('HQ_APPDID')
        return miss

    def require(self):
        """缺失必填项时抛异常（附带清晰指引）"""
        miss = self.missing()
        if miss:
            raise RuntimeError(
                '缺少必填配置: %s\n'
                '  请设置环境变量，或提供 %s\n'
                '  例: export HQ_USER=13800000000 HQ_PASS=你的密码 '
                'HQ_DEVICE=<32位设备ID> HQ_APPDID=<32位App标识>\n'
                '  （HQ_APPDID 是 App 首次启动时生成并绑定账号的设备标识，'
                '可从 App 数据目录或一次抓包里取得）'
                % (', '.join(miss), self._cfg_path))

    def masked(self):
        """用于日志的安全摘要（不泄露密码/完整账号）"""

        def m(v):
            if not v:
                return '(未设置)'
            s = str(v)
            return s if len(s) <= 4 else (s[:3] + '***' + s[-2:])

        return {
            'user': m(self.user),
            'pass': '(已设置)' if self.pwd else '(未设置)',
            'device': m(self.device),
            'app_did': m(self.app_did),
            'template': self.template,
            'template_exists': os.path.exists(self.template),
            'host': self.host,
            'port': self.port,
            'token': '(已设置, %d 字符)' % len(self.token) if self.token else '(未设置 → 不鉴权)',
        }


cfg = Config()


def with_token(url):
    """给网关 URL 附加访问令牌（若已配置 HQ_TOKEN），供桥接/录像等客户端复用。"""
    t = cfg.token
    if not t:
        return url
    from urllib.parse import quote
    sep = '&' if '?' in url else '?'
    return '%s%stoken=%s' % (url, sep, quote(t, safe=''))


if __name__ == '__main__':
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] in ('--gen-token', 'gen-token'):
        import secrets
        print(secrets.token_urlsafe(24))
        raise SystemExit(0)
    print(json.dumps(cfg.masked(), ensure_ascii=False, indent=2))
    miss = cfg.missing()
    print('缺失必填:', miss or '无')
