# syntax=docker/dockerfile:1
# =============================================================================
# 海雀摄像头本地化网关（Alcidae / Haique 2S 3K）
#   去云取流（HEVC 2880x1620 + A-law 16k）+ 云台控制 + 对讲
#
# 本镜像**不含任何个人信息**：账号 / 密码 / 设备 ID 一律在运行时通过环境变量注入。
# =============================================================================
FROM python:3.12-slim

# PyAV 的 manylinux wheel 自带 ffmpeg，无需安装系统 ffmpeg
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HQ_HOST=0.0.0.0 \
    HQ_PORT=8899 \
    HQ_TEMPLATE=/app/assets/relayinit_template.bin

WORKDIR /app

# 国内网络可换 PyPI 镜像：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r /app/requirements.txt \
    && rm -rf /root/.cache/pip

COPY scripts/ /app/scripts/
COPY assets/  /app/assets/
COPY entrypoint.sh /app/entrypoint.sh

# 显式设置权限（不要依赖 COPY 继承的 mode）。
# 某些 NAS 的数据卷（如 fnOS 的 /vol1）用 ACL，文件的基础权限位可能是 000，
# COPY 会把这个 000 原样带进镜像 → 文件在容器里"存在但不可读"
# （典型症状：/bin/sh: 0: cannot open /app/entrypoint.sh: Permission denied）。
RUN chmod -R a+rX /app && chmod 755 /app/entrypoint.sh

# 非 root 运行；/data 用于录像等持久化
RUN useradd -r -u 10001 -m hqcam && chown -R hqcam:hqcam /app
USER hqcam

EXPOSE 8899
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
p=os.environ.get('HQ_PORT','8899'); \
t=os.environ.get('HQ_TOKEN',''); \
u='http://127.0.0.1:'+p+'/status.json'+('?token='+t if t else ''); \
sys.exit(0 if urllib.request.urlopen(u,timeout=5).getcode()==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gateway"]
