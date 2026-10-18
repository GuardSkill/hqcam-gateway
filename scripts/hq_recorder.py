# -*- coding: utf-8 -*-
"""
海雀摄像头 · 分段录像器（把视频存到 fnOS / 任意目录）

架构：
  hq_live_server.py（取流网关，含音视频合流）  --/av.ts-->  hq_recorder.py（本脚本）

为什么按 TS 包边界切分：
  MPEG-TS 是 188 字节定长包的连续流，在包边界切分后**每一段都是独立可播的 .ts**，
  无需重编码、无需重新封装、不依赖 PyAV 的流管理 —— 最稳。
  （若需要 .mkv，用 --mkv 在收段后调用 remux 转换。）

特性：
  - 断流自动重连
  - 保留策略：最多 N 天 / 最多占用 N GB（自动删最旧）
  - 落盘目标可以是本地目录，也可以是 fnOS 的 SMB 共享（Windows: \\\\fnOS\\home\\cam）

用法：
  python scripts/hq_recorder.py --out "D:/recordings/cam"
  python scripts/hq_recorder.py --out "\\\\fnOS\\home\\cam" --seg 300 --keep-days 7 --keep-gb 100
  python scripts/hq_recorder.py --url http://127.0.0.1:8899/av.ts --out ./rec --seg 60 --mkv
"""
import sys
import os
import time
import glob
import argparse
import urllib.request

try:
    from hq_config import with_token
except ImportError:
    def with_token(u):
        return u

TS_PKT = 188


def human(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return '%.1f%s' % (n, u)
        n /= 1024.0
    return '%.1fPB' % n


def disk_free_gb(path):
    """目标目录所在文件系统的可用空间（GB）；取不到返回 -1"""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1024.0 ** 3
    except Exception:
        try:
            import shutil
            return shutil.disk_usage(path).free / 1024.0 ** 3
        except Exception:
            return -1.0


def dir_files(outdir, exts=('ts', 'mkv')):
    files = []
    for e in exts:
        files += glob.glob(os.path.join(outdir, 'cam_*.%s' % e))
    return files


def dir_bytes(outdir, exts=('ts', 'mkv')):
    tot = 0
    for p in dir_files(outdir, exts):
        try:
            tot += os.path.getsize(p)
        except OSError:
            pass
    return tot


def cleanup(outdir, keep_days=0, keep_gb=0, min_free_gb=0, protect=None,
            exts=('ts', 'mkv')):
    """滚动清理：① 超天数 ② 超容量 ③ 磁盘剩余不足。
    三道保险任意一道触发就删最旧的；protect = 正在写的片段，永不删。"""
    files = dir_files(outdir, exts)
    if protect:
        ap = os.path.abspath(protect)
        files = [p for p in files if os.path.abspath(p) != ap]
    files.sort(key=lambda p: os.path.getmtime(p))

    removed = []

    # ① 超过保留天数
    if keep_days > 0:
        cutoff = time.time() - keep_days * 86400
        keep = []
        for p in files:
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < cutoff:
                removed.append(p)
            else:
                keep.append(p)
        files = keep

    def sz(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    # ② 超过总容量上限
    if keep_gb > 0:
        total = sum(sz(p) for p in files)
        limit = keep_gb * 1024 ** 3
        i = 0
        while total > limit and i < len(files):
            total -= sz(files[i])
            removed.append(files[i])
            i += 1
        files = files[i:]

    # ③ 磁盘剩余空间低于底线（最后一道保险）
    if min_free_gb > 0:
        free = disk_free_gb(outdir)
        i = 0
        while free >= 0 and free < min_free_gb and i < len(files):
            free += sz(files[i]) / 1024.0 ** 3
            removed.append(files[i])
            i += 1
        files = files[i:]

    freed = 0
    n = 0
    for p in removed:
        try:
            freed += sz(p)
            os.remove(p)
            n += 1
        except Exception as e:
            print('   删除失败 %s %r' % (p, e))
    if n:
        print('  [清理] 删除 %d 个旧片段，释放 %s；现存 %d 段 / %s，磁盘可用 %.1f GB'
              % (n, human(freed), len(dir_files(outdir, exts)),
                 human(dir_bytes(outdir, exts)), disk_free_gb(outdir)))
    return n, freed


def remux_ts_to_mkv(ts_path, fps=25):
    """把 TS 段转成 MKV（视频/音频都流拷贝，不重编码）"""
    try:
        import av
        from fractions import Fraction
        out = ts_path[:-3] + '.mkv'
        inp = av.open(ts_path, 'r')
        oc = av.open(out, 'w', format='matroska')
        vst = next((s for s in inp.streams if s.type == 'video'), None)
        if vst is None:
            inp.close(); oc.close()
            return None
        ov = oc.add_stream_from_template(vst)
        ov.time_base = vst.time_base
        oa = None
        ast = next((s for s in inp.streams if s.type == 'audio'), None)
        if ast is not None:
            oa = oc.add_stream_from_template(ast)
            oa.time_base = ast.time_base
        for pkt in inp.demux():
            if pkt.size == 0:
                continue
            pkt.stream = ov if pkt.stream.type == 'video' else oa
            if pkt.stream is None:
                continue
            oc.mux(pkt)
        oc.close(); inp.close()
        os.remove(ts_path)
        return out
    except Exception as e:
        print('   remux 失败 %r' % (e,))
        return None


def uniq_name(outdir):
    base = time.strftime('%Y%m%d_%H%M%S')
    p = os.path.join(outdir, 'cam_%s.ts' % base)
    k = 1
    while os.path.exists(p):
        p = os.path.join(outdir, 'cam_%s_%d.ts' % (base, k))
        k += 1
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default='http://127.0.0.1:8899/av.ts')
    ap.add_argument('--out', default='./recordings')
    ap.add_argument('--seg', type=int, default=300, help='每段秒数')
    ap.add_argument('--keep-days', type=float, default=0, help='最多保留天数 (0=不限)')
    ap.add_argument('--keep-gb', type=float, default=0, help='最多占用 GB (0=不限)')
    ap.add_argument('--min-free-gb', type=float, default=0,
                    help='磁盘可用低于该值时强制删最旧片段 (0=关闭)')
    ap.add_argument('--mkv', action='store_true', help='收段后转成 .mkv')
    ap.add_argument('--cleanup-every', type=int, default=300,
                    help='清理间隔秒数（录制过程中也会执行）')
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)

    # 启动先清理一次（容器重启后立刻恢复滚动状态）
    cleanup(a.out, a.keep_days, a.keep_gb, a.min_free_gb)

    print('[*] 录像器启动')
    print('    拉流: %s' % a.url)
    print('    落盘: %s' % os.path.abspath(a.out))
    print('    分段: %ds' % a.seg)
    print('    保留: %s 天 / %s GB / 可用低于 %s GB 时强删'
          % (a.keep_days or '∞', a.keep_gb or '∞', a.min_free_gb or '关'))
    print('    当前: %d 段 / %s，磁盘可用 %.1f GB'
          % (len(dir_files(a.out)), human(dir_bytes(a.out)), disk_free_gb(a.out)))

    nseg = 0
    nbytes = 0
    last_clean = time.time()
    active = None

    def maybe_clean(force=False):
        """录制过程中周期性清理 —— 关键：不再只在断流后才清理"""
        nonlocal last_clean
        if not force and a.cleanup_every > 0 and time.time() - last_clean < a.cleanup_every:
            return
        last_clean = time.time()
        cleanup(a.out, a.keep_days, a.keep_gb, a.min_free_gb, protect=active)
        print('  [状态] 片段 %d 个 / %s，磁盘可用 %.1f GB'
              % (len(dir_files(a.out)), human(dir_bytes(a.out)), disk_free_gb(a.out)))

    while True:
        try:
            resp = urllib.request.urlopen(with_token(a.url), timeout=25)
            name = uniq_name(a.out)
            fh = open(name, 'wb')
            active = name
            nseg += 1
            print('  [新片段] %s' % name)
            buf = b''
            t0 = time.time()
            seg_bytes = 0
            while True:
                d = resp.read(65536)
                if not d:
                    print('  [断流] 服务端关闭')
                    break
                buf += d
                n = (len(buf) // TS_PKT) * TS_PKT
                if n:
                    fh.write(buf[:n])
                    seg_bytes += n
                    buf = buf[n:]
                if time.time() - t0 >= a.seg:
                    fh.close()
                    nbytes += seg_bytes
                    print('  [收片段] %s  %s  %.0fs' % (os.path.basename(name),
                                                       human(seg_bytes), time.time() - t0))
                    active = None
                    if a.mkv:
                        r = remux_ts_to_mkv(name)
                        if r:
                            print('    -> %s' % os.path.basename(r))
                    maybe_clean()
                    name = uniq_name(a.out)
                    fh = open(name, 'wb')
                    active = name
                    t0 = time.time()
                    seg_bytes = 0
                    nseg += 1
                    print('  [新片段] %s' % name)
                else:
                    maybe_clean()
            try:
                fh.close()
            except Exception:
                pass
            active = None
            nbytes += seg_bytes
            if seg_bytes < 1024:
                try:
                    os.remove(name)
                except Exception:
                    pass
                print('  [丢弃] 片段过小')
            else:
                print('  [收片段] %s  %s' % (os.path.basename(name), human(seg_bytes)))
                if a.mkv:
                    remux_ts_to_mkv(name)
            maybe_clean(force=True)
        except KeyboardInterrupt:
            print('\n[*] 用户中断')
            break
        except Exception as e:
            print('  [错误] %r  5s 后重连' % (e,))
            time.sleep(5)
            continue
        time.sleep(2)

    cleanup(a.out, a.keep_days, a.keep_gb, a.min_free_gb)
    print('[*] 已停止。共 %d 段 / %s' % (nseg, human(nbytes)))


if __name__ == '__main__':
    main()
