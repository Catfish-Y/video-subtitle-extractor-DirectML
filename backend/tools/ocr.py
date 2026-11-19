# -*- coding: utf-8 -*-
"""OCR 门面：统一使用 ONNX Runtime 后端（CPU / DirectML）。

不再依赖 PaddleOCR / PaddlePaddle。后端、模型档位、DML 设备均由全局 config
决定（GUI 可设置）；OCR 结果统一为 ``(dt_box, rec_res)`` 格式。
"""
from backend.config import config
from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.onnx_ocr import OnnxOcrRecogniser


class OcrRecogniser:
    """文本检测 + 识别门面，接口保持 ``predict(bgr_image) -> (dt_box, rec_res)``。"""

    def __init__(self):
        self.recogniser = None
        self.hardware_accelerator = HardwareAccelerator.instance()
        self.backend_used = None

    @staticmethod
    def y_round(y):
        y_min = y + 10 - y % 10
        y_max = y - y % 10
        if abs(y - y_min) < abs(y - y_max):
            return y_min
        return y_max

    def _resolve_config(self):
        """根据全局配置决定 provider、档位、设备。"""
        backend = config.ocrBackend.value
        # 总加速开关关闭时，无论选择何后端都强制 CPU
        if not config.hardwareAcceleration.value:
            backend = 'cpu'
        if backend == 'dml':
            providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
        elif backend == 'cpu':
            providers = ['CPUExecutionProvider']
        else:  # auto：有 DML 设备就用，否则 CPU
            providers = (['DmlExecutionProvider', 'CPUExecutionProvider']
                         if self.hardware_accelerator.has_dml()
                         else ['CPUExecutionProvider'])
        tier = config.modelTier.value
        det_tier = config.detModelTier.value
        device_id = None
        raw = config.dmlDeviceId.value
        if raw is not None and str(raw).strip():
            try:
                device_id = int(str(raw).strip())
            except (ValueError, TypeError):
                device_id = None
        return providers, tier, det_tier, device_id

    def init_model(self):
        providers, tier, det_tier, device_id = self._resolve_config()
        recogniser = OnnxOcrRecogniser(
            providers=providers,
            model_tier=tier,
            det_model_tier=det_tier,
            dml_device_id=device_id,
            rec_batch_size=config.recBatchNumber.value,
        )
        self.backend_used = recogniser.active_provider
        # 打印实际执行设备，便于用户核实 GUI 选择是否生效
        if recogniser.active_provider == 'DmlExecutionProvider':
            dev = recogniser.dml_device_name or '自动选择'
            print(f'[OCR] 实际执行: DirectML @ {dev}')
        else:
            print('[OCR] 实际执行: CPU')
        return recogniser

    def predict(self, image):
        if not self.recogniser:
            self.recogniser = self.init_model()
        dt_box, rec_res = self.recogniser.predict(image)
        if not dt_box:
            return [], []
        return self._sort_results(dt_box, rec_res)

    def _sort_results(self, dt_box, rec_res):
        """按「先 y 后 x」对检测结果排序，并将同一行的 y 对齐。

        这是从旧 PaddleOCR 路径保留的行为，字幕后处理依赖稳定的阅读顺序。
        """
        coordinate_list = []
        for box in dt_box:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            coordinate_list.append([min(xs), max(xs), min(ys), max(ys)])

        lines = []
        for coord in coordinate_list:
            rounded = self.y_round(coord[2])
            if not any(abs(rounded - line_y) <= 10 for line_y in lines):
                lines.append(rounded)
        lines = sorted(lines)
        for coord in coordinate_list:
            for line_y in lines:
                if abs(line_y - self.y_round(coord[2])) <= 10:
                    coord[2] = line_y

        ranked = sorted(zip(coordinate_list, rec_res), key=lambda x: (x[0][2], x[0][0]))
        sorted_boxes, sorted_rec = [], []
        for coord, rec in ranked:
            xmin, xmax, ymin, ymax = coord
            sorted_boxes.append([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])
            sorted_rec.append(rec)
        return sorted_boxes, sorted_rec


def get_coordinates(dt_box):
    """从返回的检测框中获取 (xmin, xmax, ymin, ymax) 坐标列表。"""
    coordinate_list = []
    if isinstance(dt_box, list):
        for i in dt_box:
            i = list(i)
            (x1, y1) = int(i[0][0]), int(i[0][1])
            (x2, y2) = int(i[1][0]), int(i[1][1])
            (x3, y3) = int(i[2][0]), int(i[2][1])
            (x4, y4) = int(i[3][0]), int(i[3][1])
            xmin = max(x1, x4)
            xmax = min(x2, x3)
            ymin = max(y1, y2)
            ymax = min(y3, y4)
            coordinate_list.append((xmin, xmax, ymin, ymax))
    return coordinate_list
