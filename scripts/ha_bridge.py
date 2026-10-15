# -*- coding: utf-8 -*-
"""海雀摄像头 · Home Assistant MQTT 桥

作用：把本地网关（hq_live_server.py）暴露给 Home Assistant，
      通过 MQTT 自动发现（MQTT Discovery）**零配置**生成实体：

  - camera.hqcam           每 N 秒抓一帧 snapshot.jpg 推到 hqcam/camera/image
  - button.hqcam_ptz_*     8 个云台方向 + stop，按下 -> hqcam/ptz/set
  - sensor.hqcam_status    网关状态（帧数/流量/音频/在线），JSON 属性
  - binary_sensor.hqcam_online

HA 侧只需要在 configuration.yaml 里配好 mqtt:（指向同一个 broker），
其余实体自动出现。

用法：
  python scripts/ha_bridge.py --broker 192.168.0.10 --port 1883 \\
      [--user mqtt] [--password xxx] [--gw http://127.0.0.1:8899] [--snap-interval 10]

依赖：paho-mqtt（已装在 .workbuddy-ai 的隔离 venv 中）
"""
import sys
import os
import json
import time
import argparse
import threading
import urllib.request

try:
    from hq_config import with_token
except ImportError:
    def with_token(u):
        return u

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print('!! 需要 paho-mqtt:  pip install paho-mqtt')
    sys.exit(2)

NODE = 'hqcam'
DISCOVERY = 'homeassistant'
DEV = {
    'identifiers': [NODE],
    'name': '海雀摄像头 2S 3K',
    'manufacturer': 'Alcidae (本地化)',
    'model': 'HQ 2S 3K (HEVC 2880x1620 + A-law 16k)',
    'sw_version': 'local-gateway 1.0',
}
AVAIL_TOPIC = '%s/availability' % NODE
PTZ_CMD_TOPIC = '%s/ptz/set' % NODE
IMG_TOPIC = '%s/camera/image' % NODE
STATUS_TOPIC = '%s/status' % NODE

DIRS = ['up', 'down', 'left', 'right', 'upleft', 'upright', 'downleft', 'downright', 'stop']
DIR_CN = {'up': '上', 'down': '下', 'left': '左', 'right': '右',
          'upleft': '左上', 'upright': '右上', 'downleft': '左下',
          'downright': '右下', 'stop': '停止'}


def http_get(url, timeout=8):
    with urllib.request.urlopen(with_token(url), timeout=timeout) as r:
        return r.read()


def http_get_json(url, timeout=8):
    return json.loads(http_get(url, timeout).decode('utf-8', 'replace'))


class Bridge:
    def __init__(self, a):
        self.a = a
        self.gw = a.gw.rstrip('/')
        # paho-mqtt 2.x 需要显式 callback API 版本；1.x 没有该参数 —— 两种都兼容
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                      client_id='hqcam-bridge')
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id='hqcam-bridge')
        if a.user:
            self.client.username_pw_set(a.user, a.password)
        self.client.will_set(AVAIL_TOPIC, 'offline', retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    # ---------------- MQTT ----------------
    def on_connect(self, c, u, flags, rc):
        print('[*] MQTT 已连接 rc=%s' % rc)
        c.publish(AVAIL_TOPIC, 'online', retain=True)
        c.subscribe(PTZ_CMD_TOPIC, qos=0)
        self.publish_discovery()

    def pub(self, topic, payload, retain=True, qos=0):
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def publish_discovery(self):
        base = '%s/%s/%s' % (DISCOVERY, NODE, NODE)
        # ---- camera（MQTT 图片相机）----
        cam = {
            'name': '海雀直播',
            'unique_id': '%s_cam' % NODE,
            'device': DEV,
            'availability_topic': AVAIL_TOPIC,
            'topic': IMG_TOPIC,
            'image_encoding': 'b64',
        }
        # ---- 状态传感器 ----
        st = {
            'name': '海雀状态',
            'unique_id': '%s_status' % NODE,
            'device': DEV,
            'availability_topic': AVAIL_TOPIC,
            'state_topic': STATUS_TOPIC,
            'value_template': "{{ value_json.state }}",
            'json_attributes_topic': STATUS_TOPIC,
            'icon': 'mdi:cctv',
        }
        # ---- 在线 binary_sensor ----
        online = {
            'name': '海雀在线',
            'unique_id': '%s_online' % NODE,
            'device': DEV,
            'state_topic': AVAIL_TOPIC,
            'payload_on': 'online',
            'payload_off': 'offline',
            'device_class': 'connectivity',
        }
        self.pub(base + '/camera/config', json.dumps(cam))
        self.pub(base + '/sensor/config', json.dumps(st))
        self.pub(base + '/binary_sensor/config', json.dumps(online))
        # ---- 云台按钮 ----
        for d in DIRS:
            cfg = {
                'name': '云台·%s' % DIR_CN[d],
                'unique_id': '%s_ptz_%s' % (NODE, d),
                'device': DEV,
                'availability_topic': AVAIL_TOPIC,
                'command_topic': PTZ_CMD_TOPIC,
                'payload_press': d,
                'icon': 'mdi:arrow-collapse-all' if d == 'stop' else 'mdi:arrow-%s-bold' % {
                    'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
                    'upleft': 'top-left', 'upright': 'top-right',
                    'downleft': 'bottom-left', 'downright': 'bottom-right',
                }.get(d, 'up'),
            }
            self.pub(base + '/button_%s/config' % d, json.dumps(cfg))
        print('[*] MQTT 自动发现已发布（camera + sensor + binary_sensor + %d 按钮）' % len(DIRS))

    def on_message(self, c, u, msg):
        if msg.topic == PTZ_CMD_TOPIC:
            d = msg.payload.decode('utf-8', 'replace').strip().lower()
            if d not in DIRS:
                print('   [ptz] 忽略未知方向 %r' % d)
                return
            threading.Thread(target=self.do_ptz, args=(d,), daemon=True).start()

    # ---------------- 动作 ----------------
    def do_ptz(self, d):
        ms = 400
        try:
            r = http_get_json('%s/ptz?dir=%s&ms=%d' % (self.gw, d, ms))
            f7 = r[-1].get('f7') if isinstance(r, list) and r else None
            print('   [ptz] %s -> f7=%s' % (d, f7))
        except Exception as e:
            print('   [ptz] %s 失败 %r' % (d, e))

    def snap_loop(self):
        while True:
            try:
                jpg = http_get(self.gw + '/snapshot.jpg', timeout=15)
                import base64
                self.pub(IMG_TOPIC, base64.b64encode(jpg).decode(), retain=True)
                print('   [snap] %d 字节' % len(jpg))
            except Exception as e:
                print('   [snap] 失败 %r' % (e,))
            time.sleep(self.a.snap_interval)

    def status_loop(self):
        while True:
            try:
                s = http_get_json(self.gw + '/status.json')
                self.pub(STATUS_TOPIC, json.dumps(s, ensure_ascii=False), retain=True)
            except Exception as e:
                print('   [status] 失败 %r' % (e,))
            time.sleep(5)

    def run(self):
        print('[*] 连接 MQTT %s:%d ...' % (self.a.broker, self.a.port))
        self.client.connect(self.a.broker, self.a.port, 30)
        threading.Thread(target=self.snap_loop, daemon=True).start()
        threading.Thread(target=self.status_loop, daemon=True).start()
        try:
            self.client.loop_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.pub(AVAIL_TOPIC, 'offline', retain=True)
            self.client.disconnect()
            print('[*] 已断开')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--broker', required=True, help='MQTT broker 地址')
    ap.add_argument('--port', type=int, default=1883)
    ap.add_argument('--user', default=None)
    ap.add_argument('--password', default=None)
    ap.add_argument('--gw', default='http://127.0.0.1:8899', help='本地网关地址')
    ap.add_argument('--snap-interval', type=int, default=10, help='抓帧间隔秒')
    a = ap.parse_args()
    Bridge(a).run()


if __name__ == '__main__':
    main()
