# -*- coding: utf-8 -*-
"""GUI 启动时的环境与模型检测。

集中处理：DML 设备枚举、模型齐全性检查、缺失模型下载。供设置页展示状态，
避免把检测逻辑散落在 UI 代码中。
"""
import threading

from backend.config import tr
from backend.tools.onnx_ocr import (
    MODEL_INFO, check_models, list_dml_devices, model_download_url, _download_model,
)


def detect_environment():
    """返回环境检测结果字典。

    {
      'dml_devices': [{'index','name',...}],
      'model_tier': 'tiny'|'small'|'medium'（识别档）,
      'det_model_tier': 'tiny'|'small'|'medium'（检测档）,
      'missing': [模型目录名],
      'model_dir': str,
      'ready': bool,
    }
    """
    from backend.config import config
    devices = list_dml_devices()
    tier = config.modelTier.value
    det_tier = config.detModelTier.value
    missing, model_dir = check_models(tier, det_model_tier=det_tier)
    return {
        'dml_devices': devices,
        'model_tier': tier,
        'det_model_tier': det_tier,
        'missing': missing,
        'model_dir': model_dir,
        'ready': not missing,
    }


def _t(key, default):
    """读取翻译，缺失时回退默认文案。"""
    try:
        return tr['Setting'][key]
    except (KeyError, TypeError):
        return default


def describe_missing(info):
    """把缺失模型信息格式化为给用户看的提示文本。"""
    if info['ready']:
        return _t('ModelReady', 'All models ready')
    names = ', '.join(MODEL_INFO.get(m, m) for m in info['missing'])
    hint = _t('ModelMissingHint', 'Download from {0} and extract to {1}').format(
        model_download_url(info['missing'][0]), info['model_dir'])
    return _t('ModelMissing', 'Missing models: {0}').format(names) + '\n' + hint


def model_download_items(info):
    """返回缺失模型的下载指引列表，供 GUI 弹窗展示。

    每项为 ``(显示名, 模型目录名, 下载URL)``。
    """
    return [
        (MODEL_INFO.get(name, name), name, model_download_url(name))
        for name in info['missing']
    ]


def download_missing_models(info, progress_callback=None, done_callback=None):
    """后台下载所有缺失模型。

    :param progress_callback: fn(message: str)，用于汇报进度
    :param done_callback: fn(success: bool, message: str)
    """
    def worker():
        try:
            from pathlib import Path
            cache = Path(info['model_dir'])
            for name in info['missing']:
                if progress_callback:
                    progress_callback(_t('ModelDownloading', 'Downloading {0} ...').format(
                        MODEL_INFO.get(name, name)))
                _download_model(name, cache)
            if progress_callback:
                progress_callback(_t('ModelDownloadDone', 'Model download finished'))
            if done_callback:
                done_callback(True, _t('ModelDownloadDone', 'Model download finished'))
        except Exception as e:
            msg = _t('ModelDownloadFail', 'Model download failed: {0}').format(str(e))
            if done_callback:
                done_callback(False, msg)
    threading.Thread(target=worker, daemon=True).start()
