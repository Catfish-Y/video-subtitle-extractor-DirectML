# -*- coding: utf-8 -*-
"""选区裁剪前移：OCR 计算前按选区裁剪图像，大幅降低 det/rec 计算量与内存。

背景：原流程对完整画面做检测/识别，选区仅用于事后过滤——选区外的文字
先花算力识别出来再丢弃。本模块把裁剪前移到计算之前：
- 裁剪区域 = 全部选区的并集包围盒 + 边距（多选区时不裁掉其他选区）；
- 识别结果坐标加回偏移量还原到全帧坐标系，下游过滤/去重逻辑无感知。

边距构成：
- 越界容忍边距：SUB_AREA_DEVIATION_RATE 允许文本框略微超出选区仍保留，
  裁剪时按同比例外扩，避免把这类框切掉导致行为变化；
- 安全边距：det 的 unclip 会把文本框向外扩约 1.5 倍，贴边文字需要少量
  上下文像素，默认 8px。

CropRegion 为纯数据对象（可 pickle，随 options 传入 OCR 子进程）。
"""
import numpy as np

_SAFETY_MARGIN = 8  # 像素：贴边文字的 det unclip 上下文


class CropRegion:
    """全帧坐标系下的裁剪矩形 (x0, y0, x1, y1)，x1/y1 为开区间。"""

    __slots__ = ('x0', 'y0', 'x1', 'y1')

    def __init__(self, x0, y0, x1, y1):
        self.x0 = int(x0)
        self.y0 = int(y0)
        self.x1 = int(x1)
        self.y1 = int(y1)

    def crop(self, frame):
        """裁剪图像。返回视图（不复制）；调用方如需长期持有请自行 copy。"""
        return frame[self.y0:self.y1, self.x0:self.x1]

    def shift_boxes(self, boxes):
        """把裁剪坐标系下的检测框平移回全帧坐标（返回新列表，不改原数据）。

        boxes: [[(x,y)×4], ...]，与 OcrRecogniser.predict 返回格式一致。
        """
        dx, dy = self.x0, self.y0
        return [[(x + dx, y + dy) for x, y in box] for box in boxes]

    def covers(self, frame_width, frame_height):
        """是否已覆盖全帧（此时裁剪无收益，调用方可跳过）。"""
        return self.x0 <= 0 and self.y0 <= 0 and \
            self.x1 >= frame_width and self.y1 >= frame_height

    def __repr__(self):
        return f'CropRegion({self.x0},{self.y0},{self.x1},{self.y1})'


def build_crop_region(sub_area, frame_width, frame_height,
                      deviation_rate=0.0, extra_areas=None):
    """根据选区构造裁剪区域；无有效选区或裁剪无收益时返回 None。

    :param sub_area: 主选区（像素坐标，SubtitleArea 或含 xmin/xmax/ymin/ymax 的对象）
    :param extra_areas: 其余选区列表（多选区时取并集包围盒，避免裁掉其他选区）
    :param deviation_rate: 越界容忍比例（0~1），按比例外扩边距
    """
    areas = []
    if sub_area is not None:
        areas.append(sub_area)
    if extra_areas:
        areas.extend(a for a in extra_areas if a is not None)
    rects = []
    for a in areas:
        try:
            xmin, xmax = float(a.xmin), float(a.xmax)
            ymin, ymax = float(a.ymin), float(a.ymax)
        except (AttributeError, TypeError, ValueError):
            continue
        if xmin > xmax:
            xmin, xmax = xmax, xmin
        if ymin > ymax:
            ymin, ymax = ymax, ymin
        if xmax - xmin < 2 or ymax - ymin < 2:
            continue  # 空/退化选区
        rects.append((xmin, ymin, xmax, ymax))
    if not rects:
        return None
    xmin = min(r[0] for r in rects)
    ymin = min(r[1] for r in rects)
    xmax = max(r[2] for r in rects)
    ymax = max(r[3] for r in rects)
    # 边距 = 越界容忍（按选区尺寸比例）+ 安全边距
    rate = max(0.0, float(deviation_rate or 0.0))
    mx = int((xmax - xmin) * rate) + _SAFETY_MARGIN
    my = int((ymax - ymin) * rate) + _SAFETY_MARGIN
    x0 = max(0, int(xmin) - mx)
    y0 = max(0, int(ymin) - my)
    x1 = min(int(frame_width), int(xmax) + mx)
    y1 = min(int(frame_height), int(ymax) + my)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    region = CropRegion(x0, y0, x1, y1)
    if region.covers(frame_width, frame_height):
        return None  # 几乎全帧，裁剪无收益
    return region


def parse_extra_areas(frame_width, frame_height):
    """从 config.subtitleSelectionAreas（比例坐标字符串）解析全部选区。

    格式 "ymin,ymax,xmin,xmax;..."（0~1 比例）。解析失败返回 []。
    供后端构造并集包围盒使用；主选区仍由调用方传入（GUI 任务选区优先）。
    """
    try:
        from backend.config import config
        raw = str(config.subtitleSelectionAreas.value or '')
    except Exception:
        return []
    areas = []
    for part in raw.split(';'):
        try:
            ymin, ymax, xmin, xmax = (float(v) for v in part.split(','))
        except (ValueError, TypeError):
            continue
        areas.append(_RatioArea(ymin * frame_height, ymax * frame_height,
                                xmin * frame_width, xmax * frame_width))
    return areas


class _RatioArea:
    """比例选区换算成的像素矩形（仅用于包围盒计算）。"""

    __slots__ = ('ymin', 'ymax', 'xmin', 'xmax')

    def __init__(self, ymin, ymax, xmin, xmax):
        self.ymin = ymin
        self.ymax = ymax
        self.xmin = xmin
        self.xmax = xmax