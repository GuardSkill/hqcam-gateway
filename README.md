# hqcam-gateway · 海雀摄像头本地化网关

把海雀（Alcidae / Haique）智能摄像头 **2S 3K** 从云端解耦：在本地直接建立 relay 会话、
解密媒体流，对外提供标准 HTTP 接口（HEVC / MPEG-TS / WAV / 快照 / 云台 / 对讲），
可直接接入 Home Assistant、Frigate、VLC 或任意 NVR。

> **本镜像不含任何个人信息。** 账号、密码、设备 ID 全部在运行时通过环境变量注入，
> 镜像本身可以公开发布、被任何人拉取。

---

## 功能

| 能力 | 说明 |
| --- | --- |
| 实时视频 | HEVC 2880×1620，解密后输出 Annex-B / MPEG-TS |
| 实时音频 | G.711 A-law 16 kHz 单声道，解码为 WAV |
| 音视频合流 | `/av.ts`（HEVC + AAC），HA / Frigate 直接可用 |
| 快照 | `/snapshot.jpg` |
| 云台 | 8 方向 + 变焦 + 灯光 |
| **对讲** | 向摄像头喇叭喊话（A-law 16 kHz），支持 WAV 上传 / TTS |
| 通用 RPC | 明文 RPC 直通 + 自动解密设备响应 |
| 录像 | 可选角色，按 TS 包边界分段落盘，带保留策略 |
| HA 桥 | 可选角色，MQTT 自动发现生成实体 |

## 访问令牌（★ 强烈建议开启）

网关默认**没有任何鉴权**：同网段任何人都能看画面、控云台、喊话，而且 `/status.json`
会明文吐出会话密钥。设置 `HQ_TOKEN` 后，所有端点都要求令牌：

```bash
# 生成一个随机令牌
python -c "import secrets;print(secrets.token_urlsafe(24))"
```

然后写进 `.env`：`HQ_TOKEN=你生成的令牌`。三种携带方式都认：

| 方式 | 例子 |
| --- | --- |
| URL 查询参数 | `http://<IP>:8899/av.ts?token=<TOKEN>` |
| 请求头 | `Authorization: Bearer <TOKEN>` |
| 请求头 | `X-Token: <TOKEN>` |

- 打开状态页：`http://<IP>:8899/?token=<TOKEN>`（页面内所有链接会自动带上令牌）
- 录像器 / MQTT 桥容器也会读取 `HQ_TOKEN` 自动附加，无需额外配置
- **留空 = 不鉴权**，仅适合完全可信的内网

## HTTP 接口

```
GET  /                     状态页
GET  /status.json          JSON 状态
GET  /live.hevc            原始 HEVC Annex-B
GET  /live.ts              MPEG-TS（仅视频）
GET  /av.ts                ★ MPEG-TS（HEVC + AAC 合流）
GET  /audio.wav            实时音频 WAV
GET  /audio.alaw           原始 A-law 字节流
GET  /snapshot.jpg         当前画面快照

GET  /ptz?dir=up|down|left|right|upleft|upright|downleft|downright|stop&ms=400
GET  /ptz/raw?ch=0&code=101&p1=0&p2=0
GET  /rpc?method=PtzPosition&payload=0800

GET  /vol                  读设备喇叭音量
GET  /vol?v=100            写设备喇叭音量
GET  /talk/tone?hz=1000&ms=1500&vol=100    发测试音
POST /talk                 body = 原始 A-law 16 kHz 单声道
POST /talk.wav             body = WAV 文件（自动转 16 kHz mono A-law）
```

## 快速开始

### 1. 直接跑（docker run）

```bash
docker run -d --name hqcam-gateway \
  --restart unless-stopped \
  -p 8899:8899 \
  -e HQ_USER=13800000000 \
  -e HQ_PASS=你的密码 \
  -e HQ_DEVICE=你的32位设备ID \
  -e HQ_APPDID=你的32位App标识 \
  -e HQ_TOKEN=你的访问令牌 \
  -v "$PWD/data:/data" \
  ghcr.io/your-github-name/hqcam-gateway:latest
```

### 2. docker compose（推荐）

```bash
cp .env.example .env
vi .env                 # 填 HQ_USER / HQ_PASS / HQ_DEVICE / HQ_APPDID / HQ_TOKEN / HQ_IMAGE
docker compose up -d
docker compose logs -f
```

打开 `http://<主机IP>:8899/?token=<TOKEN>` 即可看到状态页。

### 3. 本地构建

```bash
docker build -t hqcam-gateway:local .
docker run --env-file .env -p 8899:8899 hqcam-gateway:local
```

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `HQ_USER` | ✅ | 海雀 App 登录账号（手机号） |
| `HQ_PASS` | ✅ | 登录密码 |
| `HQ_DEVICE` | ✅ | 摄像头设备 ID（32 位） |
| `HQ_APPDID` | ✅ | App 设备标识（32 位，见下方说明） |
| `HQ_TOKEN` | | **访问令牌（强烈建议）**；留空 = 不鉴权 |
| `HQ_PORT` | | 容器内监听端口，默认 `8899` |
| `HQ_HOST` | | 监听地址，默认 `0.0.0.0` |
| `HQ_ROLE` | | `gateway`(默认) / `recorder` / `bridge` / `shell` |
| `HQ_REC_*` | | 录像参数，见 `.env.example` |
| `MQTT_*` | | MQTT 桥参数，见 `.env.example` |

> **`HQ_APPDID` 怎么拿？**
> 它是 App 首次启动时生成、并与你的账号绑定的设备标识（不是 APK 里的固定常量）。
> 抓一次 App 的登录/连接包，在 RelayInit 帧的 `SetConnInfo.f2.f2` 字段里就是它；
> 也可以从 App 的私有数据目录读取。

## 角色（同一个镜像，不同启动方式）

```bash
# 网关（默认）
docker run ... hqcam-gateway

# 分段录像 -> 挂载点 /rec
docker run ... -e HQ_ROLE=recorder -v /你的/录像目录:/rec hqcam-gateway

# Home Assistant MQTT 桥
docker run ... -e HQ_ROLE=bridge -e MQTT_HOST=192.168.0.10 hqcam-gateway

# 进容器排查
docker run -it ... hqcam-gateway shell
```

用 compose 时通过 profile 启动附加服务：

```bash
docker compose --profile recorder up -d
docker compose --profile bridge up -d
```

## 录像（滚动保留）

录像按 **5 分钟一段**切成独立 `.ts`，每段都能单独播放。默认**按日期分子目录**，
回看时按天找就行：

```
/vol2/1000/hqcam-rec/
├── 2026-10-16/
│   ├── cam_20261016_000000.ts
│   └── cam_20261016_000500.ts
├── 2026-10-17/
└── 2026-10-18/
```

保留策略有**三道保险**，任意一道触发都会删最旧的片段：

| 变量 | 含义 | 建议 |
| --- | --- | --- |
| `HQ_REC_HOST_DIR` | **录像落在宿主的哪个目录**（可指向另一块盘） | `/vol2/1000/hqcam-rec` |
| `HQ_REC_KEEP_DAYS` | 最多保留几天（滚动覆盖） | `3` |
| `HQ_REC_KEEP_GB` | 最多占多少 GB | 磁盘的 80%，或 `0` 不限制 |
| `HQ_REC_MIN_FREE_GB` | 磁盘可用低于此值就强删最旧 | `10` |
| `HQ_REC_SEG` | 每段秒数 | `300` |
| `HQ_REC_DATE_DIRS` | `1`=按日期分子目录（默认） `0`=平铺 | `1` |
| `HQ_REC_UID` / `HQ_REC_GID` | 录像文件属主（必须和宿主目录一致） | fnOS：`1000:1001` |

**空间估算**（实测 3K HEVC ≈ 230 KB/s，300s 一段约 67 MB）：

```
1 小时 ≈ 800 MB     1 天 ≈ 19 GB     3 天 ≈ 57 GB
```

**注意**：清理是在**录制过程中**周期性执行的（`HQ_REC_CLEANUP_EVERY`，默认 300 秒），
不是只在断流后才做 —— 所以长时间不断流也不会把盘写满。清理完会顺手删掉空的
日期目录，不会留下一堆空文件夹。旧的平铺文件（`/rec/cam_*.ts`）同样能被识别清理，
切换 `HQ_REC_DATE_DIRS` 不会漏掉历史文件。

## 接入 Home Assistant

```yaml
# 若已开启 HQ_TOKEN，所有 URL 后面加 ?token=<TOKEN>
camera:
  - platform: generic
    name: 海雀 2S 3K
    stream_source: http://<网关IP>:8899/av.ts?token=<TOKEN>
    still_image_url: http://<网关IP>:8899/snapshot.jpg?token=<TOKEN>

rest_command:
  hqcam_ptz:
    url: "http://<网关IP>:8899/ptz?dir={{ dir }}&ms={{ ms | default(400) }}&token=<TOKEN>"
  hqcam_talk_beep:
    url: "http://<网关IP>:8899/talk/tone?hz=1000&ms=1500&vol=100&token=<TOKEN>"
```

## 部署到 fnOS

见 [deploy/fnos.md](deploy/fnos.md)。核心三步：

1. 在 fnOS 上建目录，放入 `docker-compose.yml` + `.env`（填你的信息）
2. `docker compose up -d`
3. 浏览器打开 `http://<fnOS_IP>:8899/`

## 常见问题

**拉镜像卡住 / 极慢（国内）？**
fnOS 等国内 NAS 的 Docker 只给 Docker Hub 配了加速，**`ghcr.io` 是直连**，
常卡在几十 KB/s 甚至不动。把 `.env` 的 `HQ_IMAGE` 换成国内 GHCR 镜像站即可：

```dotenv
HQ_IMAGE=ghcr.nju.edu.cn/<你的github用户名>/hqcam-gateway:latest
```

（其它可试：`ghcr.m.daocloud.io`、`ghcr.dockerproxy.com`。注意 `registry-mirrors`
只对 Docker Hub 生效，管不到 `ghcr.io`，所以只能改镜像名。）

**没有声音？**
设备喇叭音量可能为 0。先设一次：

```bash
curl "http://<网关IP>:8899/vol?v=100&token=<TOKEN>"
curl "http://<网关IP>:8899/talk/tone?hz=1000&ms=3000&vol=100&token=<TOKEN>"
```

**一直返回 401？**
说明设置了 `HQ_TOKEN` 但请求没带令牌。在 URL 加 `?token=<TOKEN>`，
或加请求头 `Authorization: Bearer <TOKEN>`。

**`/snapshot.jpg` 返回 503？**
镜像是旧版（缺 Pillow）。503 响应体会带上真实原因，例如
`no snapshot yet (ImportError("No module named 'PIL'"))`。
拉最新镜像或本地重建即可：

```bash
sudo docker build -t ghcr.io/<你的github用户名>/hqcam-gateway:latest .
```

**容器起来了但一直 unhealthy？**
首次启动需要完成云端登录 + relay 握手，约 5–15 秒；`start_period` 已给到 25 秒。
若持续不健康，看日志：`docker compose logs -f hqcam-gateway`。

**登录失败？**
确认账号密码正确、且该账号已在该 App 中绑定这台设备。

## 说明

本项目为个人研究用途，用于把自己购买的设备接入自建系统。请遵守当地法律法规，
仅操作自己拥有或已获授权的设备。
