# 部署到 fnOS（飞牛 OS）

fnOS 自带 Docker / Docker Compose，部署就是「放两个文件 + 一条命令」。

下面把 `<fnOS_IP>` 换成你 fnOS 的局域网地址（例如 `192.168.1.100`）。

---

## 一、准备

1. fnOS 上打开 **Docker**（应用中心里安装并启动）。
2. SSH 登录 fnOS：`ssh 你的用户名@<fnOS_IP>`。
3. 建工作目录：

```bash
mkdir -p ~/hqcam && cd ~/hqcam
```

> 如果后面要用**录像**角色，先建好数据目录并放开权限（容器内以 UID 10001 非 root 运行）：
> ```bash
> mkdir -p ~/hqcam/data/rec
> sudo chown -R 10001:10001 ~/hqcam/data
> ```

## 二、放两个文件

把仓库里的 `docker-compose.yml` 和 `.env.example` 拷过来
（用 `scp`、或用 fnOS 的文件管理器上传）：

```bash
# 在你自己电脑上执行
scp docker-compose.yml .env.example 你的用户名@<fnOS_IP>:~/hqcam/
```

然后在 fnOS 上：

```bash
cd ~/hqcam
cp .env.example .env
vi .env
```

`.env` 至少要改这 5 项：

```dotenv
HQ_USER=你的手机号
HQ_PASS=你的密码
HQ_DEVICE=你的32位设备ID
HQ_APPDID=你的32位App标识

# ★ 访问令牌：强烈建议设置，否则同网段任何人都能看画面/控云台/喊话
#   生成：python -c "import secrets;print(secrets.token_urlsafe(24))"
HQ_TOKEN=你生成的随机令牌

# 镜像地址改成你自己的 GHCR 路径
HQ_IMAGE=ghcr.io/你的github用户名/hqcam-gateway:latest

# 可选：录像
HQ_REC_SEG=300
HQ_REC_KEEP_DAYS=7
HQ_REC_KEEP_GB=200
```

## 三、拉镜像并启动

```bash
cd ~/hqcam
docker compose pull
docker compose up -d
docker compose logs -f          # Ctrl-C 退出日志
```

首次启动要完成云端登录 + relay 握手，约 5–15 秒。看到类似
`[*] HTTP 网关 http://0.0.0.0:8899/` 就成功了。

> ### ⚠️ 国内网络：GHCR 直连很慢 / 卡死
>
> fnOS（以及多数国内 NAS）的 Docker 只给 **Docker Hub** 配了镜像加速，
> **`ghcr.io` 是直连**，国内实测会卡在几十 KB 甚至完全不动。
>
> 解决办法：把 `.env` 里的镜像地址换成国内 GHCR 镜像站：
>
> ```dotenv
> # 南京大学 GHCR 镜像（实测可用，首次拉取会自动回源缓存）
> HQ_IMAGE=ghcr.nju.edu.cn/<你的github用户名>/hqcam-gateway:latest
> ```
>
> 其它可尝试的镜像站（可用性随时间变化，自行替换域名即可）：
> `ghcr.m.daocloud.io`、`ghcr.dockerproxy.com`。
>
> 换完再 `docker compose pull`。镜像内容与 `ghcr.io` 完全一致（按 digest 校验）。
>
> 想永久生效也可以给 Docker 加 `registry-mirrors`，但注意
> **`registry-mirrors` 只对 Docker Hub 生效，不作用于 `ghcr.io`**，
> 所以对 GHCR 只能改镜像名。
>
> ### 备选：干脆在 fnOS 上本地构建
>
> 如果镜像站也慢，直接用源码构建（fnOS 的 Docker Hub 加速是可用的，
> 基础镜像 `python:3.12-slim` 实测 50 秒拉完）：
>
> ```bash
> cd /vol1/1000
> git clone https://github.com/<你的github用户名>/hqcam-gateway.git
> cd hqcam-gateway
> sudo docker build \
>   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
>   -t ghcr.io/<你的github用户名>/hqcam-gateway:latest .
> ```
>
> 构建完镜像名与 compose 里的一致，compose 会直接使用本地镜像，不再去拉。
>
> 注：fnOS 的 `/vol1` 是 **ACL 权限**（文件基础权限位为 `000`），
> 早期版本的 Dockerfile 会让容器里的文件"存在但不可读"
> （报错 `cannot open /app/entrypoint.sh: Permission denied`）。
> 现在的 Dockerfile 已显式 `chmod`，不受影响；若你自行改过，请保留那行 `chmod`。

## 四、验证

浏览器打开（`<TOKEN>` 换成你 `.env` 里的 HQ_TOKEN）：

```
http://<fnOS_IP>:8899/?token=<TOKEN>
```

或命令行自测：

```bash
# 状态
curl "http://<fnOS_IP>:8899/status.json?token=<TOKEN>"

# 音量（对讲不出声多半是这里为 0）
curl "http://<fnOS_IP>:8899/vol?v=100&token=<TOKEN>"

# 对讲测试音
curl "http://<fnOS_IP>:8899/talk/tone?hz=1000&ms=3000&vol=100&token=<TOKEN>"

# 云台
curl "http://<fnOS_IP>:8899/ptz?dir=left&ms=500&token=<TOKEN>"
```

接入 Home Assistant / Frigate 用 `http://<fnOS_IP>:8899/av.ts?token=<TOKEN>`。

## 五、开录像（可选）

```bash
docker compose --profile recorder up -d
ls -lh ~/hqcam/data/rec        # 分段 .ts 文件
```

录像落盘在 `~/hqcam/data/rec`，保留策略由 `.env` 的
`HQ_REC_KEEP_DAYS` / `HQ_REC_KEEP_GB` 控制。

## 六、接 Home Assistant（可选）

前提：HA 里已配好 MQTT broker。

```bash
# 在 .env 里填好 MQTT_HOST 等
docker compose --profile bridge up -d
```

HA 会自动发现 `camera.hqcam`、`button.hqcam_ptz_*` 等实体。

## 七、常用运维

```bash
cd ~/hqcam

docker compose ps                 # 状态
docker compose logs -f --tail=100 # 日志
docker compose restart            # 重启
docker compose down               # 停止并删除容器（数据保留在 ./data）
docker compose pull && docker compose up -d   # 升级镜像
```

**升级后如果镜像地址变了**，先 `docker compose down` 再 `up -d`。

---

## 排错

| 现象 | 处理 |
| --- | --- |
| 容器 `unhealthy` | 首次握手需 5–15s；持续不健康看 `docker compose logs -f` |
| 日志报「缺少必填配置」 | `.env` 里 4 个必填项没填全 |
| 登录失败 / 拿不到 token | 账号密码错，或 `HQ_APPDID` 与该账号不匹配 |
| 有画面没声音 | `curl ".../vol?v=100&token=<TOKEN>"` 先把喇叭音量设上去 |
| 一直 401 | 已设 `HQ_TOKEN` 但请求没带令牌，URL 加 `?token=<TOKEN>` |
| 端口被占 | `.env` 里改 `HQ_HTTP_PORT` |
| fnOS 重启后容器没起 | 已设 `restart: unless-stopped`；确认 Docker 服务随系统启动 |

## 说明

- `.env` 含账号密码，**不要提交到 git、不要分享**。
- 镜像本身不含任何个人信息，可以安全地公开分发。
