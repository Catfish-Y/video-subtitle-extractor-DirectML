
import os
import json
from pathlib import Path
from qfluentwidgets import (qconfig, ConfigItem, QConfig, OptionsValidator, BoolValidator, OptionsConfigItem, 
                            EnumSerializer, RangeValidator, RangeConfigItem, ConfigValidator)
from backend.tools.constant import SubtitleArea, VideoSubFinderDecoder
import configparser

# 项目版本号
VERSION = "3.0.0"
PROJECT_HOME_URL = "https://github.com/Catfish-Y/video-subtitle-extractor-DirectML"
PROJECT_ISSUES_URL = PROJECT_HOME_URL + "/issues"
PROJECT_RELEASES_URL = PROJECT_HOME_URL + "/releases"
PROJECT_UPDATE_URLS = [
    # /releases/latest 会跳过 prerelease/draft；3.0.0 当前为预发布版，
    # 故优先用 /releases 列表端点（由 version_service 优先取正式版、否则取预发布版）。
    "https://api.github.com/repos/Catfish-Y/video-subtitle-extractor-DirectML/releases",
    "https://api.github.com/repos/Catfish-Y/video-subtitle-extractor-DirectML/releases/latest",
    "https://accelerate.xdow.net/api/repos/Catfish-Y/video-subtitle-extractor-DirectML/releases/latest",
] 
# 硬件加速选项开关
HARDWARD_ACCELERATION_OPTION = True

# 读取界面语言配置
tr = configparser.ConfigParser()

TRANSLATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'interface', f"en.ini")
tr.read(TRANSLATION_FILE, encoding='utf-8')

class Config(QConfig):
    # 界面语言设置
    intefaceTexts = {
        '简体中文': 'ch',
        '繁體中文': 'chinese_cht',
        'English': 'en',
        '한국어': 'ko',
        '日本語': 'japan',
        'Tiếng Việt': 'vi',
        'Español': 'es',
        'Turkish': 'tr'
    }
    interface = OptionsConfigItem("Window", "Interface", "ChineseSimplified", OptionsValidator(intefaceTexts.values()), restart = True)
    
    # 窗口位置和大小
    windowX = ConfigItem("Window", "X", None)
    windowY = ConfigItem("Window", "Y", None)
    windowW = ConfigItem("Window", "Width", 1200)
    windowH = ConfigItem("Window", "Height", 1200)

    # 使用一个配置项存储所有选区
    # 默认值为一个选区，格式为："ymin,ymax,xmin,xmax;ymin,ymax,xmin,xmax;..."，分号分隔不同选区
    subtitleSelectionAreas = ConfigItem("Main", "SubtitleSelectionAreas", "0.78,0.99,0.05,0.95")

    # 字幕语言设置
    language = OptionsConfigItem("Main", "Language", "ch", OptionsValidator([name for name in tr["Language"]]))
    # 识别模式（抽帧策略）：fast=VideoSubFinder 抽帧 / accurate=逐帧 det 检测
    mode = OptionsConfigItem("Main", "Mode", "fast",  OptionsValidator(["fast", "accurate"]))
    # 是否生成TXT文本字幕
    generateTxt = ConfigItem("Main", "GenerateTxt", False, BoolValidator())
    # 识别阶段每批处理的文本框数量，显存越大可设越大
    recBatchNumber = RangeConfigItem("Main", "RecBatchNumber", 100, RangeValidator(1, 2000))
    # OCR 帧预取队列容量
    maxBatchSize = RangeConfigItem("Main", "MaxBatchSize", 100, RangeValidator(1, 2000))
    # 字幕出现区域
    subtitleArea = OptionsConfigItem("Main", "SubtitleArea", SubtitleArea.UNKNOWN, OptionsValidator(SubtitleArea), EnumSerializer(SubtitleArea))
    # 每一秒抓取多少帧进行OCR识别
    extractFrequency = RangeConfigItem("Main", "ExtractFrequency", 3, RangeValidator(1, 60))
    # 容忍的像素点偏差
    tolerantPixelY = RangeConfigItem("Main", "TolerantPixelY", 50, RangeValidator(1, 1000))
    tolerantPixelX = RangeConfigItem("Main", "TolerantPixelX", 100, RangeValidator(1, 1000))
    # 字幕区域偏移量
    subtitleAreaDeviationPixel = RangeConfigItem("Main", "SubtitleAreaDeviationPixel", 50, RangeValidator(1, 1000))
    # 最有可能出现的水印区域
    waterarkAreaNum = RangeConfigItem("Main", "WaterarkAreaNum", 5, RangeValidator(1, 10))
    # 文本相似度阈值
    # 用于去重时判断两行字幕是不是同一行，这个值越高越严格。 e.g. 0.99表示100个字里面有99各个字一模一样才算相似
    # 采用动态算法实现相似度阈值判断: 对于短文本要求较低的阈值，对于长文本要求较高的阈值
    # 如：文本较短，人民、入民，0.5就算相似
    thresholdTextSimilarity = RangeConfigItem("Main", "ThresholdTextSimilarity", 80, RangeValidator(0, 100))
    # 字幕提取中置信度低于0.75的不要
    dropScore = RangeConfigItem("Main", "DropScore", 75, RangeValidator(0, 100))
    # 字幕区域允许偏差, 0为不允许越界, 0.03表示可以越界3%
    subtitleAreaDeviationRate = RangeConfigItem("Main", "SubtitleAreaDeviationRate", 0, RangeValidator(0, 100))
    # 输出丢失的字幕帧, 仅简体中文,繁体中文,日文,韩语有效, 默认将调试信息输出到: 视频路径/loss
    debugOcrLoss = ConfigItem("Main", "DebugOcrLoss", False, BoolValidator())
    # 是否不删除缓存数据，以方便调试
    debugNoDeleteCache = ConfigItem("Main", "DebugNoDeleteCache", False, BoolValidator())
    # 是否删除空时间轴
    deleteEmptyTimeStamp = ConfigItem("Main", "DeleteEmptyTimeStamp", True, BoolValidator())
    # 是否重新分词, 用于解决没有语句没有空格
    wordSegmentation = ConfigItem("Main", "WordSegmentation", True, BoolValidator())
    # OCR 执行后端：auto=自动(DML优先) / cpu / dml
    ocrBackend = OptionsConfigItem("Main", "OcrBackend", "auto", OptionsValidator(["auto", "cpu", "dml"]))
    # OCR 检测模型档位（det）：tiny=极速 / small=均衡 / medium=精准
    detModelTier = OptionsConfigItem("Main", "DetModelTier", "small",
                                     OptionsValidator(["tiny", "small", "medium"]))
    # OCR 识别模型档位（rec）：tiny=极速 / small=均衡 / medium=精准
    modelTier = OptionsConfigItem("Main", "ModelTier", "small",
                                  OptionsValidator(["tiny", "small", "medium"]))
    # 指定 DML 设备索引（留空自动选择；预置 0-7 覆盖常见多 GPU 场景）
    dmlDeviceId = OptionsConfigItem(
        "Main", "DmlDeviceId", "", OptionsValidator(["", "0", "1", "2", "3", "4", "5", "6", "7"]))
    # 是否使用硬件加速
    hardwareAcceleration = ConfigItem("Main", "HardwareAcceleration", HARDWARD_ACCELERATION_OPTION, BoolValidator())
    # 启动时检查应用更新
    checkUpdateOnStartup = ConfigItem("Main", "CheckUpdateOnStartup", True, BoolValidator())
    # 视频保存目录
    saveDirectory = ConfigItem("Main", "SaveDirectory", "", ConfigValidator())
    # VideoSubFinder CPU核心数
    videoSubFinderCpuCores = RangeConfigItem("Main", "VideoSubFinderCpuCores", 0, RangeValidator(0, os.cpu_count()))
    # VideoSubFinder 视频解码组件
    videoSubFinderDecoder = OptionsConfigItem("Main", "VideoSubFinderDecoder", VideoSubFinderDecoder.OPENCV, OptionsValidator(VideoSubFinderDecoder), EnumSerializer(VideoSubFinderDecoder))

CONFIG_FILE = 'config/config.json'
config = Config()
qconfig.load(CONFIG_FILE, config)


def _migrate_legacy_options():
    """升级旧版本 config.json 的档位/模式值。

    3.0.0 早期版本用 light/precise 表示识别档位、auto 表示识别模式；现在 det/rec
    各自独立三档且取消了自动模式。注意 qfluentwidgets 在 load 时会把 validator
    不接受的值**静默改成第一项**（precise→tiny），因此必须直接读原始 JSON 才能
    拿到用户真正保存过的旧值。
    """
    raw = {}
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            raw = json.load(f).get('Main', {})
    except (OSError, ValueError, AttributeError):
        pass

    legacy_tiers = {'light': 'tiny', 'precise': 'small'}
    raw_tier = str(raw.get('ModelTier', '')).strip()
    if raw_tier in legacy_tiers:
        config.set(config.modelTier, legacy_tiers[raw_tier])
    elif config.modelTier.value not in config.modelTier.validator.options:
        config.set(config.modelTier, 'small')

    raw_mode = str(raw.get('Mode', '')).strip()
    if raw_mode == 'auto' or config.mode.value not in config.mode.validator.options:
        config.set(config.mode, 'fast')

    if config.detModelTier.value not in config.detModelTier.validator.options:
        config.set(config.detModelTier, 'small')


_migrate_legacy_options()

# 读取界面语言配置
tr = configparser.ConfigParser()

TRANSLATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'interface', f"{config.interface.value}.ini")
tr.read(TRANSLATION_FILE, encoding='utf-8')

# 项目的base目录
BASE_DIR = str(Path(os.path.abspath(__file__)).parent)

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'
