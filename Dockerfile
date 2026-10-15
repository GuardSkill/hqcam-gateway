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

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /root/.cache/pip

COPY scripts/ /app/scripts/
COPY assets/  /app/assets/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

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
