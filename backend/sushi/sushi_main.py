# -*- coding: UTF-8 -*-
"""sushi 时间轴同步的便捷包装层。

接受「两个视频 + 一个字幕文件」的简单参数形式，自动把两个视频转为 wav，
再调用 sushi 的 CLI 入口完成时间轴同步。GUI 实际并没有走本模块（它直接以
``python -u sushi`` 调用 ``sushi/__main__.py``），这里保留作为独立 API。
"""
import sys
import os
import re
from pathlib import Path

from .__main__ import parse_args_and_run


def _find_ffmpeg():
    """优先使用 imageio-ffmpeg 自带的 ffmpeg，回退到 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


def subtitle_sync(argv, opts=None):
    videos = []
    subtitle = None
    isAss = False

    if len(argv) < 3:
        print("参数不足, 程序退出")
        return

    for arg in argv:
        if re.search(r'\.ass$', arg, flags=re.I) is not None:
            subtitle = arg
            isAss = True
        elif re.search(r'\.srt$', arg, flags=re.I) is not None:
            subtitle = arg
        else:
            videos.append(arg)

    if subtitle is None:
        print("未找到字幕文件, 程序退出")
        return
    if len(videos) != 2:
        print(f"需要恰好两个视频文件, 实际找到 {len(videos)} 个, 程序退出")
        return

    if os.path.getsize(videos[0]) > os.path.getsize(videos[1]):
        bdVideo, hdVideo = videos[0], videos[1]
    else:
        bdVideo, hdVideo = videos[1], videos[0]
    print("HD:", hdVideo)
    print("BD:", bdVideo)

    temp_dir = None
    if opts is not None and "temp" in opts:
        temp_dir = opts["temp"]

    if temp_dir is not None:
        hdWav = os.path.join(temp_dir, os.path.basename(hdVideo) + '.wav')
        bdWav = os.path.join(temp_dir, os.path.basename(bdVideo) + '.wav')
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    else:
        hdWav = hdVideo + '.wav'
        bdWav = bdVideo + '.wav'

    ffmpeg = _find_ffmpeg()
    for video, wav in ((hdVideo, hdWav), (bdVideo, bdWav)):
        ret = os.system(f'"{ffmpeg}" -y -i "{video}" "{wav}"')
        if ret != 0:
            print(f"ffmpeg 转换失败: {video}")
            return

    suffix = Path(bdVideo).suffix.lstrip('.')
    output = re.sub(r'\.' + re.escape(suffix) + r'$',
                    '.ass' if isAss else '.srt', bdVideo, flags=re.I)

    try:
        parse_args_and_run([
            "--src", hdWav,
            "--dst", bdWav,
            "--script", subtitle,
            "--output", output,
        ])
    finally:
        for wav in (hdWav, bdWav):
            if os.path.exists(wav):
                os.remove(wav)
    print("finish", output)


if __name__ == '__main__':
    subtitle_sync(sys.argv[1:], None)
