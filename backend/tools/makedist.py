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

# 关键依赖 -> 其在 opt/packages 中 wheel 文件名的前缀（QPT 默认 DISPLAY_LOCAL_INSTALL
# 模式在构建阶段只把 wheel 下载到 opt/packages，真正的安装发生在目标机首次启动时）。
# 因此构建阶段的完整性校验必须针对 wheel 缓存，而不是尚未填充的 site-packages。
# 前缀用小写，匹配时统一转小写；名称含 - 和 _ 的归一化差异由 normalize() 处理。
_REQUIRED_WHEEL_PREFIXES = (
    "shiboken6-",                    # shiboken6
    "pyside6_essentials-",           # PySide6 运行时二进制（不再用 6.12 元包空壳）
    "pyside6_fluent_widgets-",       # qfluentwidgets
    "pysidesix_frameless_window-",   # qframelesswindow
    "onnxruntime_directml-",         # onnxruntime
    "opencv_python_headless-",       # cv2
    "numpy-",
    "pyyaml-",                       # yaml
    "rapidfuzz-",
    "urllib3-",
)


def _normalize(name):
    """wheel 文件名归一化：小写、把 - 和 . 统一成 _（版本分隔部分除外）。"""
    return name.lower().replace("-", "_")


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

        # 4) 校验关键依赖的 wheel 确实下载到 opt/packages，缺失时立即失败。
        #    QPT 在目标机首次启动时才把 wheel 安装进 site-packages，因此构建阶段
        #    的 site-packages 是（部分）空的，不能作为校验依据。
        #    opt/packages 的 wheel 缓存绝不能删除——首次启动部署依赖它。
        packages_dir = os.path.join(module_dir, "opt", "packages")
        if not os.path.isdir(packages_dir):
            raise RuntimeError(f"[{variant}] 缺少 opt/packages 目录，QPT 未正确下载依赖")
        wheels = [_normalize(f) for f in os.listdir(packages_dir)]
        missing = [prefix for prefix in _REQUIRED_WHEEL_PREFIXES
                   if not any(w.startswith(_normalize(prefix)) for w in wheels)]
        if missing:
            raise RuntimeError(
                f"[{variant}] 依赖 wheel 不完整，opt/packages 缺少: {', '.join(missing)}；"
                "请检查 requirements.txt 是否列出了全部传递依赖"
            )


def main():
    WORK_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    LAUNCH_PATH = os.path.join(WORK_DIR, 'gui.py')
    SAVE_PATH = os.path.join(os.path.dirname(WORK_DIR), 'vse_out')
    ICON_PATH = os.path.join(WORK_DIR, "design", "vse.ico")

    if os.getenv("QPT_Action") == "True":
        set_default_pip_source(PYPI_PIP_SOURCE)

    # 注意：不要传 ignore_dirs 参数。QPT 1.0b8 存在缺陷——仅当 ignore_dirs is None
    # 时才初始化 self.ignore_dirs，一旦外部传入，executor.py 内部的 .append() 会抛
    # AttributeError，导致打包在第一阶段就失败。不传时 QPT 会自行初始化该列表，
    # 并自动忽略 .github / .git / .idea / __pycache__ / 虚拟环境等目录。
    # 另外 .workbuddy 早已从 Git 历史与远端仓库彻底清除，CI 干净 checkout 的代码树
    # 中根本不含该目录，无需额外排除。
    module = CEM(
        work_dir=WORK_DIR,
        launcher_py_path=LAUNCH_PATH,
        save_path=SAVE_PATH,
        icon=ICON_PATH,
        hidden_terminal=False,
        requirements_file="./requirements.txt",
        sub_modules=[],
    )
    module.make()
    _post_process(SAVE_PATH)


if __name__ == '__main__':
    main()
