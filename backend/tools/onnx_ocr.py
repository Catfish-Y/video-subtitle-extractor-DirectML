# -*- coding: utf-8 -*-
"""DirectML / ONNX Runtime 字幕 OCR 后端。

PaddleOCR 3.x 本身只支持 PaddlePaddle 的 ``gpu:0`` / ``cpu`` device，无法使用
Windows 的 DirectML 加速。本模块绕开 PaddlePaddle 推理，直接用官方发布的
PP-OCRv6 ONNX 模型 + onnxruntime 的 ``DmlExecutionProvider`` 完成检测与识别，
并提供与 :class:`backend.tools.ocr.OcrRecogniser` 完全一致的 ``predict`` 接口。

模型档位（与抽帧模式 mode 正交）：
- det（检测模型）：从帧图像中定位文字框；rec（识别模型）：把文字框识别成文字。
  两者在模型层面完全解耦，各有 ``tiny`` / ``small`` / ``medium`` 三档，由
  ``detModelTier`` / ``modelTier`` 两个配置项分别选择，可自由混搭。
- 三档 det + 三档 rec 共 6 个 ONNX 模型随发布包一起分发（``models/``），
  开箱即用；环境变量 ``VSE_ONNX_MODEL_DIR`` 可覆盖模型目录。

设计要点：
- 前后处理（DBNet 检测后处理、CTC 解码）内联实现，不依赖 paddlex 私有 API，
  降低 PaddleX 版本升级带来的破坏风险。
- 模型位于程序目录下的 ``models/``，随绿色版一起移动；缺失时可从官方源
  按需下载到该目录，环境变量 ``VSE_ONNX_MODEL_DIR`` 可覆盖。
- 若 DmlExecutionProvider 不可用或模型加载失败，构造时会抛出异常，调用方
  (:class:`OcrRecogniser`) 负责回退到 PaddlePaddle/CPU 路径。

参考实现：PaddleX ``text_detection``/``text_recognition`` 的 processor（Apache-2.0）。
"""
import math
import os
import tarfile
import urllib.request
from pathlib import Path

import cv2
import numpy as np

try:
    import pyclipper
except ImportError:  # pragma: no cover - 依赖由 requirements.txt 保证
    pyclipper = None

# 官方 ONNX 模型下载源
_MODEL_BASE = (
    'https://paddle-model-ecology.bj.bcebos.com/'
    'paddlex/official_inference_model/paddle3.0.0'
)
MODEL_TIERS = ('tiny', 'small', 'medium')

# 检测模型（det）与识别模型（rec）各自独立分档，可任意组合
_DET_MODEL_DIRS = {
    'tiny': 'PP-OCRv6_tiny_det_onnx',
    'small': 'PP-OCRv6_small_det_onnx',
    'medium': 'PP-OCRv6_medium_det_onnx',
}
_REC_MODEL_DIRS = {
    'tiny': 'PP-OCRv6_tiny_rec_onnx',
    'small': 'PP-OCRv6_small_rec_onnx',
    'medium': 'PP-OCRv6_medium_rec_onnx',
}

# 官方源名称 -> 用途说明，供 GUI 提示用户下载
MODEL_INFO = {
    'PP-OCRv6_tiny_det_onnx': 'Tiny（极速）文本检测模型',
    'PP-OCRv6_small_det_onnx': 'Small（均衡）文本检测模型',
    'PP-OCRv6_medium_det_onnx': 'Medium（精准）文本检测模型',
    'PP-OCRv6_tiny_rec_onnx': 'Tiny（极速）文本识别模型',
    'PP-OCRv6_small_rec_onnx': 'Small（均衡）文本识别模型',
    'PP-OCRv6_medium_rec_onnx': 'Medium（精准）文本识别模型',
}

# 检测参数（与官方 inference.yml 的 PostProcess 保持一致）
_DET_THRESH = 0.3
_DET_BOX_THRESH = 0.6
_DET_UNCLIP_RATIO = 1.5
_DET_MAX_CANDIDATES = 1000
_DET_LIMIT_SIDE_LEN = 960       # DetResizeForTest.resize_long

# 识别参数
_REC_IMAGE_SHAPE = (3, 48, 320)  # (C, H, W)


def _app_root():
    """返回程序根目录（绿色版解包后的 resources 目录）。

    源码运行时 ``__file__`` 为 ``<仓库>/backend/tools/onnx_ocr.py``，
    发布包运行时为 ``<程序目录>/resources/backend/tools/onnx_ocr.py``；
    两种情况下 ``parents[2]`` 都是与 ``config/`` 同级的应用根目录。
    """
    return Path(__file__).resolve().parents[2]


def _default_cache_dir():
    """返回 ONNX 模型缓存根目录（绿色版：程序目录下的 models 文件夹）。

    模型随程序走，不写入 C 盘用户目录；环境变量 ``VSE_ONNX_MODEL_DIR``
    仍可覆盖为任意自定义位置。
    """
    override = os.environ.get('VSE_ONNX_MODEL_DIR')
    if override:
        return Path(override).expanduser()
    return _app_root() / 'models'


def model_download_url(model_dir_name):
    """返回某个模型的官方下载 URL（供 GUI 提示用户手动下载）。"""
    return f'{_MODEL_BASE}/{model_dir_name}.tar'


def check_models(model_tier='small', det_model_tier=None, cache_dir=None):
    """检查指定档位所需模型是否齐全。

    :param model_tier: 识别模型档位（tiny/small/medium）
    :param det_model_tier: 检测模型档位，None 时与识别档位相同
    返回 ``(missing, directory)``：missing 为缺失的模型目录名列表，
    directory 为模型应放置的目录（绝对路径字符串）。
    """
    cache = Path(cache_dir).expanduser() if cache_dir else _default_cache_dir()
    rec_tier = model_tier if model_tier in MODEL_TIERS else 'small'
    det_tier = det_model_tier if det_model_tier in MODEL_TIERS else rec_tier
    required = [_DET_MODEL_DIRS[det_tier], _REC_MODEL_DIRS[rec_tier]]
    missing = [name for name in required if not (cache / name / 'inference.onnx').exists()]
    return missing, str(cache)


def list_dml_devices():
    """枚举可用的 DML 设备，返回 [{'index': int, 'name': str, 'vendor_id': int, 'device_id': int}]。

    设备顺序即 onnxruntime 的 device_id 索引。GPU 厂商根据 vendor_id 推断名称。
    无 onnxruntime 或枚举失败时返回空列表。
    """
    try:
        import onnxruntime as ort
        devices = []
        index = 0
        vendor_names = {4318: 'NVIDIA', 32902: 'Intel', 4098: 'AMD', 5140: 'Qualcomm'}
        for ep in ort.get_ep_devices():
            if 'Dml' not in ep.ep_name:
                continue
            hw = ep.device
            vendor = vendor_names.get(hw.vendor_id, f'Vendor({hw.vendor_id})')
            devices.append({
                'index': index,
                'name': f'{vendor} GPU #{index} (id={hw.device_id})',
                'vendor_id': hw.vendor_id,
                'device_id': hw.device_id,
            })
            index += 1
        return devices
    except Exception:
        return []


def _download_model(model_dir_name, cache_dir):
    """按需下载并解压一个官方 ONNX 模型目录，返回其中的 inference.onnx 路径。"""
    target_dir = cache_dir / model_dir_name
    onnx_path = target_dir / 'inference.onnx'
    if onnx_path.exists():
        return onnx_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    tar_path = cache_dir / f'{model_dir_name}.tar'
    if not tar_path.exists():
        url = f'{_MODEL_BASE}/{model_dir_name}.tar'
        print(f'[DirectML] 下载 ONNX 模型: {model_dir_name} ...')
        urllib.request.urlretrieve(url, tar_path)

    with tarfile.open(tar_path) as tf:
        # 仅解包普通文件，避免 tar 中的路径穿越风险
        safe_members = []
        for member in tf.getmembers():
            member_path = (target_dir / member.name).resolve()
            if target_dir.resolve() in member_path.parents and member.isfile():
                safe_members.append(member)
        tf.extractall(target_dir, members=safe_members)

    if not onnx_path.exists():
        raise RuntimeError(f'[DirectML] 模型解包后未找到 inference.onnx: {target_dir}')
    return onnx_path


def _read_char_dict(model_dir_name, cache_dir):
    """从随 ONNX 模型分发的 inference.yml 读取 CTC 字符字典。"""
    yml_path = cache_dir / model_dir_name / 'inference.yml'
    if not yml_path.exists():
        raise RuntimeError(f'[DirectML] 未找到字符字典配置: {yml_path}')
    # 必须用 YAML 解析器：数字/标点字符在 yml 中带引号（如 - '0'），
    # 手工切字符串会把引号也读进字典，导致数字被识别成 "'2''0'" 这类错误。
    try:
        import yaml
        with open(yml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        chars = config['PostProcess']['character_dict']
    except Exception:
        # 回退：无 PyYAML 时逐行解析并手动去除成对引号
        chars = []
        in_dict = False
        with open(yml_path, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.rstrip('\n')
                if 'character_dict:' in stripped:
                    in_dict = True
                    continue
                if in_dict:
                    if stripped.startswith('  - '):
                        value = stripped[4:]
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                            value = value[1:-1]
                        chars.append(value)
                    elif stripped.strip() and not stripped.startswith(' '):
                        break
    if not chars:
        raise RuntimeError(f'[DirectML] 字符字典为空: {yml_path}')
    # CTCLabelDecode(add_special_char) 行为：首位插入 blank(索引 0)，
    # use_space_char=True 时末尾再追加一个空格。字典长度须与模型输出类别数一致。
    char_list = ['blank'] + chars
    if ' ' not in char_list:
        char_list.append(' ')
    return char_list


class _DBPostProcess:
    """DBNet 检测后处理（内联自 PaddleX，避免私有 API 依赖）。"""

    def __init__(self, thresh=_DET_THRESH, box_thresh=_DET_BOX_THRESH,
                 unclip_ratio=_DET_UNCLIP_RATIO, max_candidates=_DET_MAX_CANDIDATES):
        self.thresh = thresh
        self.box_thresh = box_thresh
        self.unclip_ratio = unclip_ratio
        self.max_candidates = max_candidates
        self.min_size = 3

    def __call__(self, pred, src_h, src_w, ratio_h, ratio_w):
        """pred: 模型输出的单张概率图 (H, W)。返回 (boxes, scores)。"""
        segmentation = pred > self.thresh
        bitmap = segmentation.astype(np.uint8)
        scale_w = src_w / bitmap.shape[1]
        scale_h = src_h / bitmap.shape[0]

        outs = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = outs[0] if len(outs) == 2 else outs[1]
        boxes, scores = [], []
        for contour in contours[:self.max_candidates]:
            points, sside = self._mini_boxes(contour)
            if sside < self.min_size:
                continue
            score = self._box_score_fast(pred, np.array(points))
            if score < self.box_thresh:
                continue
            if pyclipper is None:
                expanded = np.array(contour)
            else:
                expanded = self._unclip(np.array(points, dtype=np.float32), self.unclip_ratio)
            box, sside = self._mini_boxes(expanded.reshape(-1, 1, 2))
            if sside < self.min_size + 2:
                continue
            box = np.array(box)
            box[:, 0] = np.clip(np.round(box[:, 0] * scale_w), 0, src_w)
            box[:, 1] = np.clip(np.round(box[:, 1] * scale_h), 0, src_h)
            boxes.append(box.astype(np.int32))
            scores.append(float(score))
        return boxes, scores

    @staticmethod
    def _mini_boxes(contour):
        rect = cv2.minAreaRect(contour)
        points = sorted(cv2.boxPoints(rect), key=lambda p: p[0])
        idx1, idx2, idx3, idx4 = 0, 1, 2, 3
        if points[1][1] > points[0][1]:
            idx1, idx4 = 0, 1
        else:
            idx1, idx4 = 1, 0
        if points[3][1] > points[2][1]:
            idx2, idx3 = 2, 3
        else:
            idx2, idx3 = 3, 2
        box = [points[idx1], points[idx2], points[idx3], points[idx4]]
        return box, min(rect[1])

    @staticmethod
    def _box_score_fast(bitmap, box):
        h, w = bitmap.shape[:2]
        box = box.copy()
        xmin = max(0, min(int(math.floor(box[:, 0].min())), w - 1))
        xmax = max(0, min(int(math.ceil(box[:, 0].max())), w - 1))
        ymin = max(0, min(int(math.floor(box[:, 1].min())), h - 1))
        ymax = max(0, min(int(math.ceil(box[:, 1].max())), h - 1))
        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        box[:, 0] -= xmin
        box[:, 1] -= ymin
        cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
        return cv2.mean(bitmap[ymin:ymax + 1, xmin:xmax + 1], mask)[0]

    @staticmethod
    def _unclip(points, unclip_ratio):
        area = cv2.contourArea(points)
        length = cv2.arcLength(points, True)
        if length == 0:
            return points
        distance = area * unclip_ratio / length
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(points, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        expanded = offset.Execute(distance)
        return np.array(expanded[0] if expanded else points)


class _CTCDecode:
    """CTC 贪心解码（内联自 PaddleX）。"""

    def __init__(self, char_list):
        # char_list 首位必须是 blank(索引 0)
        self.char_list = char_list

    def __call__(self, probs):
        """probs: (T, C) 概率矩阵。返回 (text, confidence)。"""
        idx = probs.argmax(axis=-1)
        conf = probs.max(axis=-1)
        selection = idx[1:] != idx[:-1]
        selection = np.concatenate(([True], selection))
        selection &= idx != 0  # 去掉 blank
        chars = [self.char_list[i] for i in idx[selection]]
        confidences = conf[selection]
        if len(confidences) == 0:
            confidences = np.array([0.0])
        return ''.join(chars), float(np.mean(confidences))


class OnnxOcrRecogniser:
    """基于 ONNX Runtime DirectML 的字幕 OCR 识别器。

    接口与 :class:`backend.tools.ocr.OcrRecogniser` 一致：
    ``predict(bgr_image) -> (dt_box, rec_res)``。

    :param providers: onnxruntime provider 列表；默认优先 DML，CPU 兜底。
    :param model_dir: 可选，自定义 ONNX 模型根目录（含六个模型子目录）。
    :param model_tier: 识别模型档位 ``tiny`` / ``small`` / ``medium``。
    :param det_model_tier: 检测模型档位，None 时与识别档位相同。
    :param dml_device_id: 可选，DML 设备索引（见 :func:`list_dml_devices`），
        ``None`` 表示由 onnxruntime 自动选择。
    :param rec_batch_size: 识别阶段每批处理的文本框数量（来自配置项）。
    :param det_only: 仅加载检测模型（供精准模式做逐帧文本框检测用）。
    """

    def __init__(self, providers=None, model_dir=None, model_tier='small',
                 det_model_tier=None, dml_device_id=None, rec_batch_size=100,
                 det_only=False):
        import onnxruntime as ort

        self._ort = ort
        self.det_only = det_only
        self.rec_batch_size = max(1, int(rec_batch_size or 100))
        if model_tier not in MODEL_TIERS:
            model_tier = 'small'
        self.model_tier = model_tier
        det_tier = det_model_tier if det_model_tier in MODEL_TIERS else model_tier
        self.det_model_tier = det_tier
        available = ort.get_available_providers()
        if providers is None:
            providers = [p for p in ('DmlExecutionProvider', 'CPUExecutionProvider')
                         if p in available]
            if not providers:
                providers = ['CPUExecutionProvider']
        if ('DmlExecutionProvider' not in providers
                and 'CPUExecutionProvider' not in providers):
            raise RuntimeError('[ONNX] 无可用执行提供程序（DML / CPU）')

        # 将 provider 列表转为可带选项的格式，用于指定 DML 设备
        formatted = []
        for p in providers:
            if p == 'DmlExecutionProvider' and dml_device_id is not None:
                formatted.append((p, {'device_id': str(dml_device_id)}))
            else:
                formatted.append(p)
        self.providers = formatted

        self.cache_dir = Path(model_dir).expanduser() if model_dir else _default_cache_dir()
        self.det_path = _download_model(_DET_MODEL_DIRS[det_tier], self.cache_dir)

        session_options = ort.SessionOptions()
        self.det_session = ort.InferenceSession(
            str(self.det_path), sess_options=session_options, providers=formatted)
        self.det_post = _DBPostProcess()
        self.active_provider = self.det_session.get_providers()[0]

        if not det_only:
            rec_model_dir = _REC_MODEL_DIRS[model_tier]
            self.rec_path = _download_model(rec_model_dir, self.cache_dir)
            self.char_list = _read_char_dict(rec_model_dir, self.cache_dir)
            self.rec_session = ort.InferenceSession(
                str(self.rec_path), sess_options=session_options, providers=formatted)
            self.rec_decode = _CTCDecode(self.char_list)

    def _det_predict(self, image):
        """检测文本框，返回 [(x1,y1,x2,y2), ...] 的 AABB 列表。"""
        src_h, src_w = image.shape[:2]
        # DetResizeForTest(resize_long=960)，长边缩放到 960 且取 128 的倍数
        ratio = _DET_LIMIT_SIDE_LEN / max(src_h, src_w)
        resize_h = int(src_h * ratio)
        resize_w = int(src_w * ratio)
        resize_h = (resize_h + 127) // 128 * 128
        resize_w = (resize_w + 127) // 128 * 128
        if resize_h == src_h and resize_w == src_w:
            resized = image
            ratio_h = ratio_w = 1.0
        else:
            resized = cv2.resize(image, (resize_w, resize_h))
            ratio_h = resize_h / float(src_h)
            ratio_w = resize_w / float(src_w)

        # NormalizeImage: scale 1/255, mean/std, HWC->CHW
        img = resized.astype(np.float32) / 255.0
        img = (img - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / \
              np.array([0.229, 0.224, 0.225], dtype=np.float32)
        img = img.transpose((2, 0, 1))[None, ...]

        outputs = self.det_session.run(None, {'x': np.ascontiguousarray(img)})
        pred = outputs[0][0, 0]  # (H, W)
        boxes, scores = self.det_post(pred, src_h, src_w, ratio_h, ratio_w)
        return boxes, scores

    def _prepare_rec_image(self, image, box):
        """把一个检测框预处理为识别模型的输入张量 (C, H, W)。"""
        crop = self._CropRotate(image, box)
        c, img_h, img_w = _REC_IMAGE_SHAPE
        max_ratio = img_w / img_h
        h, w = crop.shape[:2]
        max_ratio = max(max_ratio, w / float(h))
        target_w = min(int(img_h * max_ratio), 3200)
        resized_w = min(target_w, int(math.ceil(img_h * w / float(h))))
        resized = cv2.resize(crop, (resized_w, img_h)).astype(np.float32)
        resized = resized.transpose((2, 0, 1)) / 255.0
        resized = (resized - 0.5) / 0.5
        padding = np.zeros((c, img_h, target_w), dtype=np.float32)
        padding[:, :, :resized_w] = resized
        return padding

    def _rec_predict_batch(self, tensors):
        """批量识别，返回 [(text, confidence), ...]，顺序与输入一致。"""
        results = []
        batch = max(1, self.rec_batch_size)
        for start in range(0, len(tensors), batch):
            chunk = tensors[start:start + batch]
            # 同批内按宽度补到一致，模型输入最后一维动态
            max_w = max(t.shape[2] for t in chunk)
            padded = np.zeros((len(chunk), chunk[0].shape[0],
                               chunk[0].shape[1], max_w), dtype=np.float32)
            for i, t in enumerate(chunk):
                padded[i, :, :, :t.shape[2]] = t
            outputs = self.rec_session.run(None, {'x': np.ascontiguousarray(padded)})
            for probs in outputs[0]:
                results.append(self.rec_decode(probs))
        return results

    @staticmethod
    def _CropRotate(img, box):
        """按四点框透视变换抠出文字行（映射为水平矩形）。"""
        x1, y1 = box[0]
        x2, y2 = box[1]
        x3, y3 = box[2]
        x4, y4 = box[3]
        width_a = np.sqrt(((x2 - x1) ** 2) + ((y2 - y1) ** 2))
        width_b = np.sqrt(((x3 - x4) ** 2) + ((y3 - y4) ** 2))
        width = int(max(width_a, width_b))
        height_a = np.sqrt(((x4 - x1) ** 2) + ((y4 - y1) ** 2))
        height_b = np.sqrt(((x3 - x2) ** 2) + ((y3 - y2) ** 2))
        height = int(max(height_a, height_b))
        if width <= 0 or height <= 0:
            return img
        src = np.float32([box[0], box[1], box[2], box[3]])
        dst = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
        matrix = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(img, matrix, (width, height))

    def detect(self, image):
        """仅做文本检测，返回检测框列表（4 点坐标的数组）。"""
        boxes, _ = self._det_predict(image)
        return [np.array(b, dtype=np.int32) for b in boxes]

    def predict(self, image):
        """与 OcrRecogniser.predict 兼容：返回 (dt_box, rec_res)。

        dt_box: [[(x1,y1),(x2,y2),(x3,y3),(x4,y4)], ...]
        rec_res: [(text, confidence), ...]
        """
        boxes, _ = self._det_predict(image)
        if not boxes:
            return [], []
        # 预处理所有框后按配置批大小批量识别，避免逐框推理的开销
        tensors = [self._prepare_rec_image(image, box) for box in boxes]
        texts = self._rec_predict_batch(tensors)
        dt_box, rec_res = [], []
        for box, (text, score) in zip(boxes, texts):
            if not text:
                continue
            dt_box.append([(int(p[0]), int(p[1])) for p in box])
            rec_res.append((text, score))
        return dt_box, rec_res
