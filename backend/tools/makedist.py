"""QPT 打包脚本：生成 Windows CPU / DirectML 二合一的绿色版运行目录。

打包后处理说明（``_post_process``）:
1. QPT 的 bundled Python 3.12.0 自带较旧的 VC++ 运行库，会导致 onnxruntime-directml
   1.24.4 在目标机上出现 "DLL 初始化例程失败"。用构建机 System32 中的新版 CRT DLL
   覆盖，保证 "目标电脑无需额外安装运行库" 这一前提成立。
2. 剔除仓库里仅供文档/演示使用的大文件，以及 VideoSubFinder 的 Intel oneAPI 变体，
   它们不会被代码调用，避免无谓增大 release 体积。
3. 校验关键依赖确实已安装到 site-packages，避免再次发布 "启动即崩溃" 的包。

注意：opt/packages 里的 wheel 缓存**不能删除**。QPT 的首次启动仍需用它执行
LocalInstallWhlOpt，删除会导致全新电脑首次启动失败。
"""
import os
import shutil

from qpt.executor import CreateExecutableModule as CEM
from qpt.smart_opt import set_default_pip_source
from qpt.kernel.qinterpreter import PYPI_PIP_SOURCE

# 仅用于仓库展示、运行时不会读取的设计/演示素材（vse.ico 需保留：GUI 窗口图标）
_UNUSED_ASSETS = (
    "demo.gif",
    "demo.png",
    "sponsor.png",
    "bg.png",
    "UI design.png",
    "paper (2020).pdf",
)

# 代码仅调用 VideoSubFinderWXW.exe（--open_video_opencv/ffmpeg）；
# _intel.exe 及其 oneAPI/CUDA 私有依赖不会被加载。
_UNUSED_VSF_FILES = (
    "VideoSubFinderWXW_intel.exe",
    "nppc64_12.dll",
    "nppicc64_12.dll",
    "nppig64_12.dll",
    "svml_dispmd.dll",
    "libmmd.dll",
    "sycl6.dll",
    "cudart64_12.dll",
)

# onnxruntime-directml 需要较新的 MSVC 运行库，QPT 内置版本过旧
_CRT_DLLS = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "concrt140.dll",
)

# 这些包必须在 site-packages 中，否则启动必然失败
_REQUIRED_PACKAGES = (
    "shiboken6",
    "PySide6",
    "qfluentwidgets",
    "qframelesswindow",
    "onnxruntime",
    "cv2",
    "numpy",
    "yaml",
    "rapidfuzz",
    "urllib3",
)


def _remove(path):
    if os.path.isfile(path) or os.path.islink(path):
        os.remove(path)


def _post_process(save_path):
    for variant in ("Release", "Debug"):
        module_dir = os.path.join(save_path, variant)
        if not os.path.isdir(module_dir):
            continue

        # 1) 刷新 VC++ 运行库（QPT 自带版本过旧，ORT 无法初始化）
        python_dir = os.path.join(module_dir, "Python")
        system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        for dll in _CRT_DLLS:
            src = os.path.join(system32, dll)
            if os.path.isfile(src) and os.path.isdir(python_dir):
                shutil.copy2(src, os.path.join(python_dir, dll))

        # 2) 裁剪无用素材
        resources = os.path.join(module_dir, "resources")
        design_dir = os.path.join(resources, "design")
        for name in _UNUSED_ASSETS:
            _remove(os.path.join(design_dir, name))

        # 3) 裁剪 VideoSubFinder 未使用的 Intel 变体
        vsf_dir = os.path.join(resources, "backend", "subfinder", "windows")
        for name in _UNUSED_VSF_FILES:
            _remove(os.path.join(vsf_dir, name))

        # 4) 校验关键依赖确实落盘，缺失时立即失败，避免再次发布启动即崩溃的包。
        #    注意：QPT 的首次启动部署仍依赖 opt/packages 中的 wheel，不可删除缓存。
        site_packages = os.path.join(python_dir, "Lib", "site-packages")
        installed = [p for p in _REQUIRED_PACKAGES
                     if os.path.exists(os.path.join(site_packages, p))]
        if len(installed) != len(_REQUIRED_PACKAGES):
            missing = set(_REQUIRED_PACKAGES) - set(installed)
            raise RuntimeError(
                f"[{variant}] 依赖安装不完整，缺少: {', '.join(sorted(missing))}；"
                "请检查 requirements.txt 是否列出了全部传递依赖"
            )


def main():
    WORK_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    LAUNCH_PATH = os.path.join(WORK_DIR, 'gui.py')
    SAVE_PATH = os.path.join(os.path.dirname(WORK_DIR), 'vse_out')
    ICON_PATH = os.path.join(WORK_DIR, "design", "vse.ico")

    if os.getenv("QPT_Action") == "True":
        set_default_pip_source(PYPI_PIP_SOURCE)

    module = CEM(
        work_dir=WORK_DIR,
        launcher_py_path=LAUNCH_PATH,
        save_path=SAVE_PATH,
        icon=ICON_PATH,
        hidden_terminal=False,
        requirements_file="./requirements.txt",
        sub_modules=[],
        ignore_dirs=[os.path.join(WORK_DIR, ".workbuddy")],
    )
    module.make()
    _post_process(SAVE_PATH)


if __name__ == '__main__':
    main()
