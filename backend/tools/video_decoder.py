# -*- coding: utf-8 -*-
"""视频解码统一入口：按配置与实测性能选择 软件 / 各硬件解码器。

支持的解码器（程序启动时按实际硬件能力探测，见 :func:`probe_decoders`）：
- ``sw``            软件解码（OpenCV ffmpeg 后端，多线程）
- ``d3d11:<n>``     D3D11VA / DXVA2 硬解，n 为 DXGI 适配器索引（OpenCV 原生支持，
                    任意显卡通用：NVIDIA / Intel / AMD 均可）
- ``qsv:<n>``       Intel Quick Sync Video 硬解（ffmpeg 子进程流式管道，
                    n 为 QSV 设备索引；仅 Intel 核显/独显支持）
- ``nvdec:<n>``     NVIDIA NVDEC / CUDA 硬解（ffmpeg 子进程流式管道，
                    n 为 CUDA 设备索引；仅 NVIDIA 显卡支持）

背景（2026-11 在本机 GTX1070 + Intel UHD630 实测）：
- OCR 需要 CPU 侧 BGR 帧，硬解后的 GPU→CPU 回读开销很大：常见 H.264 视频
  软解反而更快；只有解码本身很重（高分辨率高码率 / HEVC 等）时硬解才有收益。
  因此 auto 模式在首次打开视频时做小样本基准测试（软解 vs 全部可用硬解器，
  含 seek），谁快用谁，结果按进程缓存。
- OpenCV 的 D3D11 路径支持随机 seek（CAP_PROP_POS_FRAMES/MSEC）；
  QSV/NVDEC 的 ffmpeg 管道只支持顺序读，由 :class:`_FfmpegCapture` 包装成
  与 cv2.VideoCapture 兼容的接口（向前 seek 用顺序解码跳帧实现，向后 seek
  重启管道；对按帧率采样 / VSF 时间戳递增读帧的真实场景足够高效）。
- 硬解不改变解码结果（±1 级 YUV→BGR 转换差异，OCR 无影响），任何硬解失败
  都自动回退软解，不影响功能。

配置项 config.videoHwDecode：
- ``auto``（默认）：基准测试自动决定；
- ``sw``：始终软解；
- ``d3d11:<n>`` / ``qsv:<n>`` / ``nvdec:<n>``：强制指定解码器（失败仍回退软解）。
- 旧值 ``on`` 迁移为 ``auto``，``off`` 迁移为 ``sw``。
总开关 config.hardwareAcceleration=False 时一律软解。
"""
import os
import re
import subprocess
import threading
import time

import cv2
import numpy as np

# 进程级缓存
_decision_cache = {}          # {(video_path, mode): decoder_id}
_probe_cache = None           # 启动探测结果 [{'id','label','kind','device'}]
_probe_lock = threading.Lock()

# 基准测试参数：从视频中段取若干帧（含 seek），足以反映真实读取模式
_BENCH_FRAMES = 12
_BENCH_MAX_SECONDS = 2.5

_VENDOR_NAMES = {4318: 'NVIDIA', 32902: 'Intel', 4098: 'AMD', 5140: 'Qualcomm'}


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


def _dxgi_adapters():
    """枚举 DXGI 适配器，返回 [{'index','description','vendor_id','discrete'}]。

    优先用 onnxruntime DML 设备元数据（含 DxgiAdapterNumber）；不可用时
    回退 ctypes 直接调 DXGI。软件适配器（Basic Render Driver）被跳过。
    """
    adapters = []
    try:
        from backend.tools.onnx_ocr import get_dml_ep_devices
        for ep in get_dml_ep_devices():
            meta = dict(ep.device.metadata or {})
            try:
                idx = int(meta.get('DxgiAdapterNumber', ''))
            except (TypeError, ValueError):
                continue
            adapters.append({
                'index': idx,
                'description': meta.get('Description', '').strip() or f'GPU (DXGI #{idx})',
                'vendor_id': ep.device.vendor_id,
                'discrete': meta.get('Discrete') == '1',
            })
    except Exception:
        pass
    if not adapters:
        adapters = _dxgi_adapters_ctypes()
    # 独显优先，其次按 DXGI 索引
    adapters.sort(key=lambda a: (not a['discrete'], a['index']))
    return adapters


def _dxgi_adapters_ctypes():
    """无 onnxruntime 时的 DXGI 枚举兜底（仅 Windows）。"""
    result = []
    try:
        import ctypes
        dxgi = ctypes.windll.dxgi

        class GUID(ctypes.Structure):
            _fields_ = [('Data1', ctypes.c_ulong), ('Data2', ctypes.c_ushort),
                        ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]

        class Vtbl(ctypes.Structure):
            _fields_ = [('f', ctypes.c_void_p * 32)]

        class DESC1(ctypes.Structure):
            _fields_ = [('Description', ctypes.c_wchar * 128),
                        ('VendorId', ctypes.c_uint), ('DeviceId', ctypes.c_uint),
                        ('SubSysId', ctypes.c_uint), ('Revision', ctypes.c_uint),
                        ('DedicatedVideoMemory', ctypes.c_size_t),
                        ('DedicatedSystemMemory', ctypes.c_size_t),
                        ('SharedSystemMemory', ctypes.c_size_t),
                        ('AdapterLuid', ctypes.c_int64), ('Flags', ctypes.c_uint)]

        iid = GUID(0x770aae78, 0xf26f, 0x4dba,
                   (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return result
        vt = ctypes.cast(ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                         ctypes.POINTER(Vtbl)).contents
        enum = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.POINTER(ctypes.c_void_p))(vt.f[12])
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            if enum(factory, i, ctypes.byref(adapter)) != 0:
                break
            avt = ctypes.cast(ctypes.cast(adapter, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                              ctypes.POINTER(Vtbl)).contents
            get_desc = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                          ctypes.POINTER(DESC1))(avt.f[10])
            desc = DESC1()
            get_desc(adapter, ctypes.byref(desc))
            if not (desc.Flags & 0x2):  # DXGI_ADAPTER_FLAG_SOFTWARE
                result.append({
                    'index': i,
                    'description': desc.Description.strip(),
                    'vendor_id': desc.VendorId,
                    'discrete': desc.DedicatedVideoMemory > 512 * 1024 * 1024,
                })
            i += 1
    except Exception:
        pass
    return result


def _ffmpeg_device_ok(spec):
    """用 -init_hw_device + 1 帧 lavfi 源快速探测 ffmpeg 硬件设备能否创建。

    不依赖任何视频文件；设备创建失败时 stderr 含 'Device creation failed'
    或 'Failed to set value'。返回 True/False。
    """
    exe = _ffmpeg_exe()
    args = [exe, '-hide_banner', '-loglevel', 'error',
            '-init_hw_device', spec,
            '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.04:r=25',
            '-frames:v', '1', '-f', 'null', '-']
    try:
        r = subprocess.run(args, capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        err = r.stderr.decode('utf-8', 'replace')
        return not ('Device creation failed' in err or 'Failed to set value' in err)
    except Exception:
        return False


def probe_decoders(force=False):
    """探测本机实际可用的解码器，返回 [{'id','label','kind','device'}]。

    程序启动时调用一次（结果进程内缓存）。探测项：
    - 每个真实 DXGI 适配器的 D3D11 硬解（用样帧验证，失败不列出）；
    - Intel QSV：对每个 QSV 设备索引做 ffmpeg 设备创建探测；
    - NVIDIA NVDEC：对每个 CUDA 设备索引做 ffmpeg 设备创建探测。
    始终包含 ``sw``（软件解码）作为兜底，排在首位。
    """
    global _probe_cache
    with _probe_lock:
        if _probe_cache is not None and not force:
            return list(_probe_cache)
        decoders = [{'id': 'sw', 'label': '', 'kind': 'sw', 'device': None}]
        adapters = _dxgi_adapters()

        # D3D11：OpenCV 原生路径，用合成样帧验证真实可用性
        sample = _make_sample_video()
        for ad in adapters:
            if sample and _d3d11_works(sample, ad['index']):
                vendor = _VENDOR_NAMES.get(ad['vendor_id'], '')
                label = f"D3D11 硬解 · {ad['description']}"
                decoders.append({'id': f"d3d11:{ad['index']}", 'label': label,
                                 'kind': 'd3d11', 'device': ad['index']})
        if sample:
            try:
                os.remove(sample)
            except OSError:
                pass

        # QSV：仅当存在 Intel 显卡时探测（避免无谓的子进程开销）
        if any(a['vendor_id'] == 32902 for a in adapters):
            for i in range(4):
                if _ffmpeg_device_ok(f'qsv=qs:{i}'):
                    intel = next((a for a in adapters if a['vendor_id'] == 32902), None)
                    name = intel['description'] if intel else 'Intel GPU'
                    decoders.append({'id': f'qsv:{i}', 'label': f'Intel QSV · {name}',
                                     'kind': 'qsv', 'device': i})
                else:
                    break  # QSV 设备索引连续，失败即止

        # NVDEC：仅当存在 NVIDIA 显卡时探测
        if any(a['vendor_id'] == 4318 for a in adapters):
            for i in range(4):
                if _ffmpeg_device_ok(f'cuda=cu:{i}'):
                    nv = next((a for a in adapters if a['vendor_id'] == 4318), None)
                    name = nv['description'] if nv else 'NVIDIA GPU'
                    decoders.append({'id': f'nvdec:{i}', 'label': f'NVIDIA NVDEC · {name}',
                                     'kind': 'nvdec', 'device': i})
                else:
                    break

        _probe_cache = decoders
        return list(decoders)


def probe_done():
    """启动探测是否已完成（供 GUI 判断能否直接取结果）。"""
    return _probe_cache is not None


def is_valid_decoder_id(value):
    """判断配置值是否为合法的解码器 id（不触发探测）。

    合法值：``auto`` / ``sw`` / ``d3d11:<n>`` / ``qsv:<n>`` / ``nvdec:<n>``。
    只做格式校验；具体设备是否存在由启动迁移用探测结果核对。
    """
    if value in ('auto', 'sw'):
        return True
    m = re.fullmatch(r'(d3d11|qsv|nvdec):(\d{1,2})', str(value or ''))
    return m is not None


def normalize_mode(value):
    """把配置值归一化为 decide() 认识的 mode。

    兼容旧值：``on``→``auto``、``off``→``sw``；非法值一律按 ``auto``。
    """
    value = str(value or '').strip()
    if value == 'off':
        return 'sw'
    if value == 'on' or not is_valid_decoder_id(value):
        return 'auto'
    return value


def _make_sample_video():
    """生成 2 帧合成 H.264 样片（临时文件），用于 D3D11 可用性验证。"""
    try:
        import tempfile
        exe = _ffmpeg_exe()
        path = os.path.join(tempfile.gettempdir(), f'vse_probe_{os.getpid()}.mp4')
        r = subprocess.run(
            [exe, '-y', '-hide_banner', '-loglevel', 'error',
             '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=0.1:r=10',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', path],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return path if r.returncode == 0 and os.path.exists(path) else None
    except Exception:
        return None


def _d3d11_works(path, device):
    """验证指定 DXGI 设备的 D3D11 硬解真实可用（打开 + 读 1 帧 + 属性确认）。"""
    cap = _open_cv2(path, 'd3d11', device)
    try:
        if not cap.isOpened():
            return False
        if cap.get(cv2.CAP_PROP_HW_ACCELERATION) != cv2.VIDEO_ACCELERATION_D3D11:
            return False
        ok, _ = cap.read()
        return ok
    finally:
        cap.release()


def _open_cv2(path, kind='sw', device=None):
    """OpenCV 打开视频：sw 软解 / d3d11 硬解指定 DXGI 设备。"""
    if kind == 'd3d11' and device is not None:
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_D3D11,
                  cv2.CAP_PROP_HW_DEVICE, int(device)]
    else:
        params = [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE]
    try:
        return cv2.VideoCapture(path, cv2.CAP_FFMPEG, params)
    except cv2.error:
        return cv2.VideoCapture()


def _probe_video(path):
    """用 ffmpeg -i 解析视频元数据：(width, height, fps, frame_count, pix_fmt)。"""
    try:
        exe = _ffmpeg_exe()
        r = subprocess.run([exe, '-hide_banner', '-i', path],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        err = r.stderr.decode('utf-8', 'replace')
        w = h = 0
        fps = 0.0
        count = 0
        pix = 'nv12'
        m = re.search(r'Video:.*?,\s*(\d{2,5})x(\d{2,5})', err)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        m = re.search(r'(\d+(?:\.\d+)?)\s*fps', err)
        if m:
            fps = float(m.group(1))
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', err)
        if m and fps > 0:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            count = int(secs * fps)
        m = re.search(r'Video:.*?,\s*([a-z0-9]+(?:10|12|16)[a-z0-9]*)\s*[\(,]', err)
        if m:
            pix = 'p010'  # 10/12/16bit 一律按 p010 回传
        return w, h, fps, count, pix
    except Exception:
        return 0, 0, 0.0, 0, 'nv12'


class _FfmpegCapture:
    """ffmpeg 硬解流式管道包装器，接口与 cv2.VideoCapture 兼容。

    QSV / NVDEC 通过 ffmpeg 子进程解码并以 rawvideo(BGR24) 管道回传。
    只支持顺序解码：
    - ``read()`` 顺序取下一帧；
    - ``set(POS_FRAMES/POS_MSEC, n)`` 向前 seek 用顺序解码跳帧实现，
      向后 seek 重启管道（真实提取场景按时间递增读帧，重启极少发生）；
    - ``get()`` 返回启动时探测的元数据；``release()`` 终止子进程。
    """

    def __init__(self, path, kind, device):
        self._path = path
        self._kind = kind
        self._device = device
        self._proc = None
        self._pm_key = None      # ProcessManager 中的注册键
        self._pos = 0            # 已解码帧数（下一帧的帧号）
        self._pending = None     # 打开时预读的首帧（read 时先返回它）
        self._w, self._h, self._fps, self._count, self._pix = _probe_video(path)
        self._frame_bytes = self._w * self._h * 3
        self._opened = self._w > 0 and self._h > 0
        if self._opened:
            self._start(0)
            # 预读首帧验证真实可解码（设备创建失败/格式不支持时 ffmpeg 会
            # 立即退出）；失败则视为打开失败，由 open_capture 回退软解。
            # 成功则缓存首帧，read() 先返回它，保证帧号从 0 开始。
            if self._opened:
                ok, frame = self._read_raw()
                if ok:
                    self._pending = frame
                else:
                    self._opened = False
                    self._stop()

    def _args(self, start_sec):
        exe = _ffmpeg_exe()
        args = [exe, '-hide_banner', '-loglevel', 'error']
        if self._kind == 'qsv':
            args += ['-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv',
                     '-hwaccel_device', str(self._device)]
        else:  # nvdec
            args += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
                     '-hwaccel_device', str(self._device)]
        args += ['-i', self._path]
        if self._kind in ('qsv', 'nvdec'):
            args += ['-vf', f'hwdownload,format={self._pix}']
        # seek 必须用输出侧 -ss（解码后按时间戳丢帧）。输入侧 -ss 会把时间戳
        # 对齐到关键帧：QSV 实测在 GOP 中段会整段错位（帧 24-200 偏差 11-17 级），
        # 输出侧 -ss 则逐帧精确（±1 级硬解转换差异）。代价是从头解码到目标位置，
        # 但向后 seek 在真实提取流程中极少发生（按时间递增读帧）。
        if start_sec > 0:
            args += ['-ss', f'{start_sec:.6f}']
        args += ['-an', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
        return args

    def _start(self, frame_no):
        self._stop()
        self._pending = None
        start_sec = frame_no / self._fps if self._fps > 0 else 0
        try:
            self._proc = subprocess.Popen(
                self._args(start_sec), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._pos = frame_no
            # 交给进程管理器托管：程序退出/关闭窗口时统一终止，避免残留
            try:
                from backend.tools.process_manager import ProcessManager
                self._pm_key = f'ffmpeg_decode_{id(self)}'
                ProcessManager.instance().add_process(self._proc, self._pm_key)
            except Exception:
                self._pm_key = None
        except Exception:
            self._proc = None
            self._opened = False

    def _stop(self):
        if self._proc is not None:
            if self._pm_key is not None:
                try:
                    from backend.tools.process_manager import ProcessManager
                    ProcessManager.instance().remove_process(self._pm_key)
                except Exception:
                    pass
                self._pm_key = None
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def isOpened(self):
        return self._opened and self._proc is not None

    def _read_raw(self):
        """从管道读一帧。返回 (ok, frame)；frame 为可写副本。

        管道按整帧字节数阻塞读取；ffmpeg 异常退出/挂死时 read 会一直阻塞，
        因此用守护线程 + 超时兜底，超时即终止子进程并报告失败。
        """
        if self._proc is None or self._frame_bytes <= 0:
            return False, None
        holder = {}

        def _reader():
            try:
                holder['buf'] = self._proc.stdout.read(self._frame_bytes)
            except Exception:
                holder['buf'] = b''

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(30)
        if t.is_alive():
            # 超时：标记关闭并终止子进程（reader 线程会因管道关闭而退出）
            self._opened = False
            self._stop()
            return False, None
        buf = holder.get('buf', b'')
        if len(buf) < self._frame_bytes:
            return False, None
        # 必须 copy：np.frombuffer 得到只读数组，而下游（main.py 画选区框、
        # 水印标注等）会对帧做 cv2.rectangle/putText 原地绘制
        frame = np.frombuffer(buf, np.uint8).reshape(self._h, self._w, 3).copy()
        return True, frame

    def read(self):
        if self._pending is not None:
            frame, self._pending = self._pending, None
            self._pos += 1
            return True, frame
        if not self.isOpened():
            return False, None
        ok, frame = self._read_raw()
        if ok:
            self._pos += 1
        return ok, frame

    def set(self, prop, value):
        if not self._opened:
            return False
        if prop == cv2.CAP_PROP_POS_FRAMES:
            target = int(value)
        elif prop == cv2.CAP_PROP_POS_MSEC:
            target = int(value / 1000.0 * self._fps) if self._fps > 0 else 0
        else:
            return False
        target = max(0, target)
        if target == self._pos:
            return True
        if target < self._pos:
            # 向后 seek：重启管道（-ss 输入定位到目标帧）
            self._start(target)
            return self.isOpened()
        # 向前 seek：顺序解码跳帧（比逐帧重启进程快得多）
        while self._pos < target:
            ok, _ = self.read()
            if not ok:
                return False
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h)
        if prop == cv2.CAP_PROP_FPS:
            return float(self._fps)
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self._count)
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self._pos)
        if prop == cv2.CAP_PROP_POS_MSEC:
            return self._pos / self._fps * 1000.0 if self._fps > 0 else 0.0
        if prop == cv2.CAP_PROP_HW_ACCELERATION:
            return float(cv2.VIDEO_ACCELERATION_ANY)
        return 0.0

    def release(self):
        self._stop()
        self._opened = False


class _PrefetchedCapture:
    """cv2.VideoCapture 包装器：缓存打开时预读的首帧，read() 先返回它。

    用于 D3D11 硬解的首帧预检：open_capture 先读一帧确认硬解真实可用
    （某些视频首帧即触发 DXGI 设备移除），预检通过后把首帧缓存起来，
    保证调用方 read() 仍从第 0 帧开始，帧号不丢失。其余接口透传。
    """

    def __init__(self, cap, first_frame):
        self._cap = cap
        self._pending = first_frame

    def read(self):
        if self._pending is not None:
            frame, self._pending = self._pending, None
            return True, frame
        return self._cap.read()

    def isOpened(self):
        return self._cap.isOpened()

    def set(self, prop, value):
        self._pending = None
        return self._cap.set(prop, value)

    def get(self, prop):
        return self._cap.get(prop)

    def release(self):
        self._pending = None
        return self._cap.release()


def _open_any(path, decoder_id):
    """按解码器 id 打开视频，返回 capture（cv2.VideoCapture 或 _FfmpegCapture）。"""
    if decoder_id == 'sw' or not decoder_id:
        return _open_cv2(path)
    kind, _, dev = decoder_id.partition(':')
    device = int(dev) if dev else 0
    if kind == 'd3d11':
        return _open_cv2(path, 'd3d11', device)
    if kind in ('qsv', 'nvdec'):
        return _FfmpegCapture(path, kind, device)
    return _open_cv2(path)


def _run_with_timeout(fn, timeout):
    """在守护线程里执行 fn，超时返回 (False, None)。

    用于隔离 OpenCV D3D11 硬解的潜在挂起：某些视频（如 HEVC）在中段 seek
    时可能触发 DXGI 设备移除（staging texture 创建失败 887a0005），此后
    cv2 的 read/set 会在 C++ 层无限阻塞。守护线程不阻塞进程退出；超时即
    弃用该硬解器（回退软解），不影响功能。
    """
    holder = {}

    def _worker():
        try:
            holder['result'] = fn()
        except Exception as e:
            holder['error'] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    if 'error' in holder:
        return False, None
    return True, holder.get('result')


def _verify_hw_decoder(path, decoder_id, timeout=12):
    """验证硬解器对该视频真实可用：打开 + 顺序读若干帧。

    用顺序读而非 seek 验证：真实提取按时间递增读帧，顺序解码能力才是关键；
    且 QSV/NVDEC 的向后 seek 需从头解码到目标位置（长视频极慢），不适合做
    可用性探针。D3D11 的 seek 挂起风险由基准阶段的超时保护兜底。
    整体带超时保护，超时即弃用该硬解器。
    """
    holder = {}

    def _work():
        cap = _open_any(path, decoder_id)
        holder['cap'] = cap
        try:
            if not cap.isOpened():
                return False
            for _ in range(5):
                ok, _ = cap.read()
                if not ok:
                    return False
            return True
        finally:
            cap.release()

    done, result = _run_with_timeout(_work, timeout)
    if not done:
        # 超时：worker 可能仍卡在 read/set，主动释放已打开的 capture，
        # 避免 ffmpeg 子进程 / D3D11 设备泄漏
        cap = holder.get('cap')
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
    return bool(done and result)


def _bench_one(path, decoder_id, frame_count, fps):
    """小样本基准：打开 + 从头顺序读若干帧。返回 (耗时秒, 是否成功)。

    采用顺序读而非中段 seek：真实提取（按帧率采样 / VSF 时间戳递增）以顺序
    读帧为主，顺序解码吞吐才是决定因素；QSV/NVDEC 的向后 seek 需从头解码，
    若用 seek 基准会严重失真。D3D11 路径整体套超时保护（防止 DXGI 设备移除
    后 cv2 挂起拖死 auto 决策）；QSV/NVDEC 的 _FfmpegCapture 自带读帧超时。
    """
    kind = decoder_id.partition(':')[0]
    holder = {}

    def _work():
        t0 = time.perf_counter()
        cap = _open_any(path, decoder_id)
        holder['cap'] = cap
        if not cap.isOpened():
            cap.release()
            return float('inf'), False
        try:
            for _ in range(_BENCH_FRAMES):
                ok, _f = cap.read()
                if not ok:
                    break
                if time.perf_counter() - t0 > _BENCH_MAX_SECONDS:
                    break
            return time.perf_counter() - t0, True
        finally:
            cap.release()

    if kind == 'd3d11':
        done, result = _run_with_timeout(_work, _BENCH_MAX_SECONDS + 8)
        if not done:
            cap = holder.get('cap')
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            return float('inf'), False
        if result is None:
            return float('inf'), False
        return result
    return _work()


def decide(video_path, frame_count=0, fps=0, mode='auto'):
    """决定该视频用哪个解码器，返回 decoder_id（'sw' 表示软解）。

    结果按 (path, mode) 进程内缓存；mode='sw' 恒软解；指定具体解码器时
    直接返回（打开失败由 open_capture 回退）；'auto' 对软解与全部已探测
    硬解器做基准测试择优（硬解需快 >10% 才启用）。
    """
    key = (str(video_path), mode)
    if key in _decision_cache:
        return _decision_cache[key]

    result = 'sw'
    if mode not in ('sw', 'off'):
        if mode in ('auto', 'on'):
            candidates = [d['id'] for d in probe_decoders() if d['id'] != 'sw']
            # 先验证各硬解器对该视频真实可用（格式不支持 / seek 挂起的剔除）
            working = [did for did in candidates if _verify_hw_decoder(video_path, did)]
            if working:
                sw_time, sw_ok = _bench_one(video_path, 'sw', frame_count, fps)
                best, best_time = None, float('inf')
                for did in working:
                    t, ok = _bench_one(video_path, did, frame_count, fps)
                    if ok and t < best_time:
                        best, best_time = did, t
                if sw_ok and best is not None and best_time < sw_time * 0.9:
                    result = best
        else:
            # 强制指定解码器（d3d11:n / qsv:n / nvdec:n）
            result = mode
    _decision_cache[key] = result
    return result


def open_capture(video_path, frame_count=0, fps=0):
    """按配置打开视频，返回 (cap, decoder_id)。

    decoder_id='sw' 表示软解。硬解打开失败自动回退软解，保证功能不受影响。
    调用方与 cv2.VideoCapture 完全兼容（read/set/get/release）。
    """
    mode = 'auto'
    try:
        from backend.config import config
        if not config.hardwareAcceleration.value:
            mode = 'sw'
        else:
            mode = normalize_mode(config.videoHwDecode.value)
    except Exception:
        pass

    did = decide(video_path, frame_count, fps, mode)
    if did != 'sw':
        kind = did.partition(':')[0]
        if kind in ('qsv', 'nvdec'):
            # _FfmpegCapture 打开时已预读首帧验证，isOpened 即代表可解码
            cap = _open_any(video_path, did)
            if cap.isOpened():
                return cap, did
            cap.release()
        else:
            # D3D11：首帧预检（带超时），确认真实可解码再交付；
            # 预检读出的首帧缓存进 _PrefetchedCapture，帧号不丢失。
            # 防止强制选中在该视频上损坏的设备把挂起带到提取阶段。
            holder = {}

            def _preflight():
                cap = _open_any(video_path, did)
                holder['cap'] = cap
                if not cap.isOpened():
                    return None
                ok, frame = cap.read()
                if not ok:
                    return None
                return frame

            done, frame = _run_with_timeout(_preflight, 12)
            cap = holder.get('cap')
            if done and frame is not None and cap is not None:
                return _PrefetchedCapture(cap, frame), did
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    return _open_cv2(video_path), 'sw'


def describe(decoder_id):
    """返回解码方式描述（供日志展示）。"""
    if not decoder_id or decoder_id == 'sw':
        return '软件解码'
    for d in probe_decoders():
        if d['id'] == decoder_id:
            return f"硬件解码 ({d['label']})"
    return f'硬件解码 ({decoder_id})'
