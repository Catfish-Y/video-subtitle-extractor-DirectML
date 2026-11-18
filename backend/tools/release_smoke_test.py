"""发布前冒烟测试：在**打包后的解释器**里验证所有运行时依赖可真正导入。

仅做 import 校验，不启动 GUI。由 workflow 在生成 Release 目录后、上传前执行，
用于拦截 "打包成功但启动即 ImportError / DLL 加载失败" 的回归。

用法（在 vse_out/Release 目录下）::

    ./Python/python.exe -S -c "import sys;sys.path.insert(0,'Lib/site-packages');\
        exec(open('resources/backend/tools/release_smoke_test.py',encoding='utf-8').read())"
"""
import importlib
import sys

MODULES = (
    "cv2",
    "numpy",
    "yaml",
    "pyclipper",
    "shapely",
    "PIL",
    "pysrt",
    "Levenshtein",
    "rapidfuzz",
    "wordsegment",
    "tqdm",
    "imageio_ffmpeg",
    "shiboken6",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtXml",
    "qfluentwidgets",
    "qframelesswindow",
    "requests",
    "urllib3",
    "certifi",
    "chardet",
    "colorama",
    "darkdetect",
    "showinfm",
)


def main():
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 需要捕获所有导入失败
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] {name}: {exc}", file=sys.stderr)
        else:
            print(f"[ OK ] {name}")

    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        print(f"[ OK ] onnxruntime {ort.__version__} providers={providers}")
        if "DmlExecutionProvider" not in providers:
            failed.append(("onnxruntime", "DmlExecutionProvider 不可用"))
    except Exception as exc:  # noqa: BLE001
        failed.append(("onnxruntime", f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] onnxruntime: {exc}", file=sys.stderr)

    if failed:
        raise SystemExit(f"冒烟测试失败，共 {len(failed)} 项：{failed}")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
