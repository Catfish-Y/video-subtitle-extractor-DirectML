简体中文 | [English](README_en.md)

<p align="center"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/icon_1024.png" alt="VSE Logo" width="128"/></p>

## 项目简介

![License](https://img.shields.io/badge/License-Apache%202-red.svg)
![python version](https://img.shields.io/badge/Python-3.12+-blue.svg)
![support os](https://img.shields.io/badge/OS-Windows-green.svg)  

Video-subtitle-extractor (VSE) 是一款将视频中的硬字幕提取为外挂字幕文件(srt格式)的软件，本仓库fork自[YaoFANGUK](https://github.com/YaoFANGUK/video-subtitle-extractor)，并根据自己的实际需求进行了大量修改。

## 源码使用说明

#### 1. 安装 Python

请确保您已经安装了 Python 3.12+

- Windows 用户可以前往 [Python 官网](https://www.python.org/downloads/windows/) 下载并安装 Python
- MacOS 用户可以使用 Homebrew 安装：
  ```shell
  brew install python@3.12
  ```
- Linux 用户可以使用包管理器安装，例如 Ubuntu/Debian：
  ```shell
  sudo apt update && sudo apt install python3.12 python3.12-venv python3.12-dev
  ```

#### 2. 安装依赖文件

请使用虚拟环境来管理项目依赖，避免与系统环境冲突

（1）创建虚拟环境并激活
```shell
python -m venv videoEnv
```

- Windows：
```shell
videoEnv\\Scripts\\activate
```
- MacOS/Linux：
```shell
source videoEnv/bin/activate
```

#### 3. 创建并激活项目目录

切换到源码所在目录：
```shell
cd <源码所在目录>
```
> 例如：如果您的源代码放在 D 盘的 tools 文件夹下，并且源代码的文件夹名为 video-subtitle-extractor，则输入：
> ```shell
> cd D:/tools/video-subtitle-extractor-main
> ```

#### 4. 安装合适的运行环境

本项目仅支持 **Windows**，只有一个安装版本，同时支持 **CPU** 与 **DirectML（DML）**
两种推理模式（假设显卡支持 DX12；现役 GPU 均满足）。OCR 统一使用 ONNX Runtime +
官方 **PP-OCRv6** ONNX 模型，不依赖 PaddlePaddle，也无需 CUDA/cuDNN。

##### (1) 安装（单一版本，CPU / DML 通用）

```shell
pip install -r requirements.txt
```

- 依赖中的 `onnxruntime-directml` 同时提供 `CPUExecutionProvider` 与
  `DmlExecutionProvider`：装一次即可在 CPU 与各品牌 GPU（DirectML）之间切换，
  无需区分 CPU 版 / DML 版，也无需卸载重装。
- NVIDIA / AMD / Intel 显卡统一走 DirectML，不需要 CUDA/cuDNN。
  > `onnxruntime-directml` 需 1.24.4+（1.20.1 在 Python 3.13 上无法安装）。

##### (2) 模型下载与档位

- OCR 使用官方 PP-OCRv6 ONNX 模型，分为两个档位（与抽帧模式正交，可在 GUI 中自由组合）：
  - **轻量档**：`PP-OCRv6_small_det` + `PP-OCRv6_tiny_rec`，速度优先；
  - **精准档**：`PP-OCRv6_small_det` + `PP-OCRv6_small_rec`，准确度优先。
- 模型从百度官方源按需下载到 `~/.paddleocr/onnx/`（不随仓库分发），可用环境变量
  `VSE_ONNX_MODEL_DIR` 覆盖。GUI 启动时会检测模型是否齐全：缺失时会提示所需模型、
  下载地址与存放目录，并提供「一键下载缺失模型」按钮。
- 也可手动下载：
  ```
  https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/<模型名>.tar
  ```
  将 tar 解压到 `~/.paddleocr/onnx/` 下，形成 `<模型名>/inference.onnx` 与 `inference.yml`。

##### (3) GUI 中的后端与设备选择

「基础设置」页提供：
- **OCR 后端**：`auto`（有 DML 设备时自动用 DML）、`cpu`、`dml`；
- **模型档位**：轻量 / 精准；
- **DML 设备**：选择具体的 GPU（程序启动时枚举）或自动选择；
- **硬件加速总开关**：关闭时强制使用 CPU。

#### 5. 运行程序

- 运行图形化界面版本（GUI）

```shell
python gui.py
```

- 运行命令行版本（CLI）

```shell
python ./backend/main.py
```

## 常见问题与解决方案

#### 1. 运行不正常/没有结果

解决方案：在「基础设置」页检查 OCR 后端与模型状态。单一安装包已同时含 CPU / DML
 provider；若 GPU 未被识别，确认显卡支持 DX12 并在「基础设置」页手动指定 DML 设备。
 模型缺失时点击「一键下载缺失模型」。

#### 2. 7z文件解压错误

解决方案：升级7-zip解压程序到最新版本
