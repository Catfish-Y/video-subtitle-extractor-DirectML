"""发布前冒烟测试：在**打包后的解释器**里验证所有运行时依赖可被真实部署并导入。

由 workflow 在生成 Release 目录后、上传前执行，用于拦截 "打包成功但启动即
ImportError / DLL 加载失败" 的回归。仅验证依赖与 DML，不启动 GUI，也不修改
Release 产物（wheel 安装到系统临时目录，结束后删除）。

原理
----
QPT 默认 DISPLAY_LOCAL_INSTALL 模式在构建阶段只把 wheel 下载到 ``opt/packages``，
真正的安装发生在目标机首次启动时（sitecustomize -> RunExecutableModule._solve_module）。
因此不能只看 site-packages（构建阶段它是空的）。本脚本模拟这一步：用打包目录内的
pip 把 ``opt/packages`` 的 wheel 装到临时 target，再在新进程里 import 校验。

必须设置环境变量 ``QPT_MODE=Debugging``（含 "ing"）运行本脚本，否则 QPT 的
``sitecustomize.py`` 会尝试自动部署并拉起 GUI / 抛 TypeError。

用法（在 vse_out/Release 目录下）::

    QPT_MODE=Debugging ./Python/python.exe resources/backend/tools/release_smoke_test.py
"""
import glob
import os
import subprocess
import sys
import tempfile

# Windows 下打包解释器的 stdout 默认用 charmap 编码，打印中文日志会抛
# UnicodeEncodeError。强制改为 UTF-8，保证本脚本所有 print 都能输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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

# 项目自身模块：必须能从打包产物中导入。历史上 ui/icon/ 被 .gitignore 的
# macOS "Icon" 规则误伤而未入库，导致 CI 全绿但用户启动即 ModuleNotFoundError；
# 另有 FluentIcon.CHART 在 qfluentwidgets 1.11 被移除的 API 不兼容。把这些模块
# 纳入冒烟测试，可在 CI 阶段拦截同类回归，而不是等用户下载后反馈。
PROJECT_MODULES = (
    "gui",
    "ui.home_interface",
    "ui.setting_interface",
    "ui.timeline_sync_interface",
    "ui.advanced_setting_interface",
    "ui.component.task_list_component",
    "ui.component.video_display_component",
    "ui.icon.my_fluent_icon",
    "backend.config",
    "backend.tools.ocr",
    "backend.tools.onnx_ocr",
    "backend.tools.env_check",
    "backend.tools.subtitle_detect",
)

# _pth 里声明的 site-packages 相对路径（QPT 约定）
SITE_PACKAGES_REL = os.path.join("Lib", "site-packages")


def _module_dir():
    """返回绿色版根目录（Release 或 Debug）。

    脚本位于 <root>/resources/backend/tools/release_smoke_test.py，故上溯三级。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def _install_wheels_to_target(python_exe, module_dir, target):
    """用打包解释器把 opt/packages 的 wheel 全量装到临时 target。"""
    packages_dir = os.path.join(module_dir, "opt", "packages")
    wheels = sorted(glob.glob(os.path.join(packages_dir, "*.whl")))
    if not wheels:
        raise RuntimeError(f"opt/packages 中没有 wheel: {packages_dir}")

    env = os.environ.copy()
    # 关键：避免 sitecustomize 自动部署 / 拉起 GUI
    env["QPT_MODE"] = "Debugging"
    cmd = [python_exe, "-m", "pip", "install",
           "--no-index", "--no-deps", "--disable-pip-version-check",
           "--target", target]
    cmd.extend(wheels)
    print(f"[INFO] 部署 {len(wheels)} 个 wheel 到临时目录 ...", flush=True)
    proc = subprocess.run(cmd, env=env, cwd=module_dir,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("wheel 部署失败:\n" + proc.stdout[-4000:])
    print("[INFO] wheel 部署完成", flush=True)


def _verify_imports_in_child(python_exe, module_dir, target, script_path):
    """在子进程里以 target + 打包 site-packages 为路径校验全部 import。"""
    env = os.environ.copy()
    env["QPT_MODE"] = "Debugging"
    env["PYTHONIOENCODING"] = "utf-8"
    # 无头环境下 import Qt 模块不创建窗口，但设 offscreen 更稳妥
    env["QT_QPA_PLATFORM"] = "offscreen"

    code = r"""
import importlib, sys, os
target = sys.argv[1]
site_packages = sys.argv[2]
resources_dir = sys.argv[3]
# resources_dir 是项目自身模块根目录（gui.py / ui / backend 所在处）
for p in (resources_dir, target, site_packages):
    if p not in sys.path:
        sys.path.insert(0, p)
failed = []
# 先校验第三方依赖
for name in __MODULES__:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append((name, type(exc).__name__ + ": " + str(exc)))
        print("[FAIL] " + name + ": " + str(exc), file=sys.stderr)
    else:
        print("[ OK ] " + name)
# 再校验项目自身模块（拦截源码未入库 / API 不兼容）
for name in __PROJECT_MODULES__:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append((name, type(exc).__name__ + ": " + str(exc)))
        print("[FAIL] " + name + ": " + str(exc), file=sys.stderr)
    else:
        print("[ OK ] " + name)
try:
    import onnxruntime as ort
    providers = ort.get_available_providers()
    print("[ OK ] onnxruntime " + ort.__version__ + " providers=" + str(providers))
    if "DmlExecutionProvider" not in providers:
        failed.append(("onnxruntime", "DmlExecutionProvider 不可用"))
except Exception as exc:
    failed.append(("onnxruntime", type(exc).__name__ + ": " + str(exc)))
    print("[FAIL] onnxruntime: " + str(exc), file=sys.stderr)
if failed:
    raise SystemExit("冒烟测试失败，共 " + str(len(failed)) + " 项：" + repr(failed))
print("SMOKE TEST PASSED")
""".replace("__MODULES__", repr(MODULES)).replace("__PROJECT_MODULES__", repr(PROJECT_MODULES))

    resources_dir = os.path.join(module_dir, "resources")
    proc = subprocess.run(
        [python_exe, "-c", code, target, os.path.join(module_dir, SITE_PACKAGES_REL), resources_dir],
        env=env, cwd=resources_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    output = proc.stdout
    # 子进程输出直接在当前进程回显，便于在 workflow 日志里定位
    print(output, flush=True)
    return proc.returncode == 0


def main():
    if os.getenv("QPT_MODE") != "Debugging":
        raise SystemExit("请以 QPT_MODE=Debugging 运行本脚本（避免 sitecustomize 自动部署/拉起 GUI）")

    python_exe = sys.executable
    module_dir = _module_dir()
    print(f"[INFO] python: {python_exe}")
    print(f"[INFO] module_dir: {module_dir}")

    target = tempfile.mkdtemp(prefix="vse_smoke_")
    try:
        _install_wheels_to_target(python_exe, module_dir, target)
        ok = _verify_imports_in_child(python_exe, module_dir, target, __file__)
        if not ok:
            raise SystemExit("SMOKE TEST FAILED")
    finally:
        # 清理临时安装目录，不影响 Release 产物
        import shutil
        shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    main()
