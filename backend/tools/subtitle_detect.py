# -*- coding: utf-8 -*-
"""精准模式的文本检测器，基于 ONNX det 模型（与 OCR 共用模型/后端）。"""
import numpy as np

from backend.config import config
from backend.tools.onnx_ocr import OnnxOcrRecogniser


class SubtitleDetect:
    """检测视频帧中是否存在文本框（精准模式逐帧判定字幕头尾帧）。"""

    def __init__(self):
        backend = config.ocrBackend.value
        if not config.hardwareAcceleration.value:
            backend = 'cpu'
        if backend == 'dml':
            providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
        else:
            providers = ['CPUExecutionProvider']
        device_id = None
        raw = config.dmlDeviceId.value
        if raw is not None and str(raw).strip():
            try:
                device_id = int(str(raw).strip())
            except (ValueError, TypeError):
                pass
        self.detector = OnnxOcrRecogniser(
            providers=providers, dml_device_id=device_id, det_only=True,
            det_model_tier=config.detModelTier.value)

    def detect_subtitle(self, img):
        """检测文本框。

        :return: ``(dt_boxes, elapse)``，dt_boxes 为 numpy 数组列表。
        """
        return np.array(self.detector.detect(img), dtype=object), 0
