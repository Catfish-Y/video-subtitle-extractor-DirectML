# -*- coding: utf-8 -*-
"""执行后端检测（仅 CPU / DirectML，Windows 平台）。

职责：探测 onnxruntime 可用的执行提供程序与 DML 设备，供 GUI 启动时展示、
供 OCR 后端选择使用。不再检测 CUDA / PaddlePaddle GPU。
"""
from backend.tools.onnx_ocr import list_dml_devices


class HardwareAccelerator:
    """执行后端信息单例：记录可用的 DML 设备与是否启用加速。"""

    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = HardwareAccelerator()
            cls._instance.initialize()
        return cls._instance

    def __init__(self):
        self._dml_devices = []
        self._dml_available = False
        self._enabled = True

    def initialize(self):
        self._dml_devices = list_dml_devices()
        self._dml_available = bool(self._dml_devices)

    @property
    def dml_devices(self):
        """返回 DML 设备列表（index/name/vendor_id/device_id）。"""
        return list(self._dml_devices)

    def has_dml(self):
        return self._enabled and self._dml_available

    def has_accelerator(self):
        """兼容旧调用：是否有 DML 加速可用。"""
        return self.has_dml()

    def has_cuda(self):
        """兼容旧调用：已移除 CUDA 支持，恒为 False。"""
        return False

    @property
    def accelerator_name(self):
        if not self.has_dml():
            return 'CPU'
        return ', '.join(d['name'] for d in self._dml_devices)

    @property
    def onnx_providers(self):
        """兼容旧调用：返回 DML provider 名列表（无 DML 时为空）。"""
        return ['DmlExecutionProvider'] if self.has_dml() else []

    def set_enabled(self, enable):
        self._enabled = enable
