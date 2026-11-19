[简体中文](README.md) | English

<p align="center"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/icon_1024.png" alt="VSE Logo" width="128"/></p>

## Introduction

![License](https://img.shields.io/badge/License-Apache%202-red.svg)
![python version](https://img.shields.io/badge/Python-3.12+-blue.svg)
![support os](https://img.shields.io/badge/OS-Windows-green.svg)  

Video-subtitle-extractor (VSE) is a software that extracts hard-coded subtitles from videos into external subtitle files (SRT format). This repository is forked from [YaoFANGUK](https://github.com/YaoFANGUK/video-subtitle-extractor) and has been extensively modified to meet our actual needs.

## Source Code Usage Guide

#### 1. Install Python

Make sure you have Python 3.12+ installed.

- Windows users can download and install Python from the [Python official website](https://www.python.org/downloads/windows/)
- MacOS users can install via Homebrew:
  ```shell
  brew install python@3.12
  ```
- Linux users can install via package manager, e.g. Ubuntu/Debian:
  ```shell
  sudo apt update && sudo apt install python3.12 python3.12-venv python3.12-dev
  ```

#### 2. Install Dependencies

Use a virtual environment to manage project dependencies and avoid conflicts with the system environment.

(1) Create and activate a virtual environment
```shell
python -m venv videoEnv
```

- Windows:
```shell
videoEnv\\Scripts\\activate
```
- MacOS/Linux:
```shell
source videoEnv/bin/activate
```

#### 3. Navigate to the Project Directory

Switch to the source code directory:
```shell
cd <source-code-directory>
```
> For example: if your source code is in the tools folder on drive D, and the source code folder is named video-subtitle-extractor, enter:
> ```shell
> cd D:/tools/video-subtitle-extractor-main
> ```

#### 4. Install the Appropriate Runtime Environment

This project supports **Windows** only and ships as a **single installation** that
supports both **CPU** and **DirectML (DML)** inference modes (assuming the GPU
supports DX12, which all modern GPUs do). OCR uniformly uses ONNX Runtime with
official **PP-OCRv6** ONNX models — no PaddlePaddle, no CUDA/cuDNN needed.

##### (1) Install (single version, CPU / DML universal)

```shell
pip install -r requirements.txt
```

- The `onnxruntime-directml` dependency provides both `CPUExecutionProvider` and
  `DmlExecutionProvider`: install once and switch between CPU and any brand of GPU
  (DirectML) — no separate CPU/DML builds, no uninstall-and-reinstall.
- NVIDIA / AMD / Intel GPUs all go through DirectML; CUDA/cuDNN is unnecessary.
  > `onnxruntime-directml` 1.24.4+ is required (1.20.1 cannot be installed on Python 3.13).

##### (2) Model download and tiers

- Two model tiers (orthogonal to the frame-extraction mode, freely combinable in the GUI):
  - **Light**: `PP-OCRv6_small_det` + `PP-OCRv6_tiny_rec` — speed first;
  - **Precise**: `PP-OCRv6_small_det` + `PP-OCRv6_small_rec` — accuracy first.
- Models are downloaded on demand from the official Baidu mirror into `models/`
  under the program directory (portable green build - nothing is written to the C drive
  user profile; not bundled), overridable via `VSE_ONNX_MODEL_DIR`. On startup the GUI
  checks model availability: a status light turns red and clicking the "Model Check"
  card opens a dialog with download links and the target directory.
- Manual download:
  ```
  https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/<model>.tar
  ```
  Extract into `models/` under the program directory so each model has
  `models/<name>/inference.onnx` and `inference.yml`.

##### (3) Backend and device selection in the GUI

The "Basic Settings" page offers:
- **OCR Backend**: `auto` (DML when available), `cpu`, `dml`;
- **Model Tier**: detection / recognition models each pick Tiny / Small / Medium independently;
- **DML Device**: pick the GPU used for OCR (only real adapters are listed) or auto-select.
  The selected device is the one actually used; the run log prints
  "DirectML @ <adapter name>" so you can verify. The dropdown width is fixed to
  half the card width; long adapter names are elided (hover for the full name)
  so they never overflow the interface;
- **Hardware Acceleration** master switch: off forces CPU (both OCR and video decoding).

The "Advanced Settings" page also offers **Video hardware decoding**, whose options
are probed from the actual hardware at startup:
- **Auto**: benchmarks software vs every available hardware decoder when a video is
  opened and picks the faster one (default);
- **Software decoding**: always software;
- **D3D11 · <adapter>**: D3D11VA/DXVA2 decoding, generic to any GPU (listed per DXGI adapter);
- **Intel QSV · <adapter>**: Intel Quick Sync (only shown on Intel GPUs);
- **NVIDIA NVDEC · <adapter>**: NVIDIA NVDEC/CUDA (only shown on NVIDIA GPUs).

Unsupported formats, failed init or abnormal devices fall back to software automatically.
Note: OCR needs CPU-side frames, so the GPU→CPU readback after hardware decoding adds
overhead — for common H.264 video software decoding is often faster. Keep "Auto" and let
the program decide by measurement rather than forcing hardware decoding.

#### 5. Run the Program

- Run the GUI version:

```shell
python gui.py
```

- Run the CLI version:

```shell
python ./backend/main.py
```

## FAQ

#### 1. Program not working / no results

Solutions: On the Basic Settings page check the OCR backend and model status. The single
installation already includes both CPU and DML providers; if the GPU is not detected,
confirm it supports DX12 and manually select the DML device on the Basic Settings page.
Use the "Download Missing Models" button if models are missing.

#### 2. 7z file extraction error

Solution: Upgrade 7-Zip to the latest version.
