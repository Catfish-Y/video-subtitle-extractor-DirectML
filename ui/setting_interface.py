from PySide6 import QtWidgets
from PySide6.QtCore import Qt, QUrl, QEvent
from qfluentwidgets import (FluentIcon, ComboBoxSettingCard, SwitchSettingCard,
                            PushSettingCard, InfoBar, MessageBoxBase, SubtitleLabel,
                            BodyLabel, HyperlinkLabel)
from backend.config import config, tr, HARDWARD_ACCELERATION_OPTION
from backend.tools import env_check


def _t(key, default):
    """读取翻译，缺失时回退默认文案。"""
    try:
        return tr['Setting'][key]
    except (KeyError, TypeError):
        return default


class DeviceComboBoxSettingCard(ComboBoxSettingCard):
    """DML 设备选择卡片：下拉框宽度固定为卡片宽度的一定比例（默认 50%）。

    显卡全名可能很长（如 "NVIDIA GeForce GTX 1070 with Max-Q Design"），
    qfluentwidgets 的 ComboBox 会按当前文本 adjustSize 撑宽，把卡片布局
    顶出界面。这里把下拉框宽度钉死在卡片宽度的比例上：
    - 关闭按钮的 Expanding 尺寸策略，改为 Fixed，宽度由卡片 resizeEvent 驱动；
    - 文本超出宽度时按钮自动省略号显示，完整名称放 tooltip；
    - 下拉菜单弹出时仍按完整文本自适应宽度（不受按钮宽度限制）。
    """

    WIDTH_RATIO = 0.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        combo = self.comboBox
        combo.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        combo.setMinimumWidth(0)
        combo.installEventFilter(self)
        combo.currentTextChanged.connect(lambda _t: self._sync_combo_tooltip())
        self._sync_combo_width()
        self._sync_combo_tooltip()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_combo_width()

    def eventFilter(self, obj, event):
        if obj is self.comboBox and event.type() == QEvent.Resize:
            # ComboBox.setText 会 adjustSize 试图撑宽，这里立即拉回比例宽度
            self._sync_combo_width()
        return super().eventFilter(obj, event)

    def _sync_combo_width(self):
        target = max(120, int(self.width() * self.WIDTH_RATIO))
        if self.comboBox.width() != target:
            self.comboBox.setFixedWidth(target)

    def _sync_combo_tooltip(self):
        self.comboBox.setToolTip(self.comboBox.currentText())


class SettingInterface(QtWidgets.QVBoxLayout):
    """主页面的常用设置区。

    【界面语言】与【模型检测】已移至高级设置页，这里只保留日常提取常用的选项。
    """

    CARD_HEIGHT = 50
    CARD_SPACING = 3
    CARD_V_MARGIN = 16

    @classmethod
    def content_height(cls, count=9):
        """按卡片高度与间距计算设置面板的自然内容高度。"""
        return (count * cls.CARD_HEIGHT + (count - 1) * cls.CARD_SPACING
                + 2 * cls.CARD_V_MARGIN)

    def __init__(self, parent):
        super().__init__()
        self.setContentsMargins(16, 16, 16, 16)
        self.setSpacing(self.CARD_SPACING)
        self.parent = parent

        # 视频字幕的语言
        self.language_combo = ComboBoxSettingCard(
            configItem=config.language, icon=FluentIcon.FONT,
            title=tr['LanguageModeGUI']['SubtitleLanguage'], content="",
            parent=parent,
            texts=[tr['Language'][i] for i in config.language.validator.options])
        self._add_card(self.language_combo)

        # 识别模式（抽帧策略：快速=VSF 抽帧 / 精准=逐帧 det 检测，无自动）
        self.mode_combo = ComboBoxSettingCard(
            configItem=config.mode, icon=FluentIcon.SETTING,
            title=tr['LanguageModeGUI']['Mode'],
            content=_t('ModeDesc', 'Fast = VSF frame extraction; Accurate = per-frame detection'),
            parent=parent, texts=[tr['Mode'].get(i, i) for i in config.mode.validator.options])
        self._add_card(self.mode_combo)

        # OCR 后端选择
        self.backend_combo = ComboBoxSettingCard(
            configItem=config.ocrBackend, icon=FluentIcon.ROBOT,
            title=tr["Setting"]["OcrBackend"],
            content=tr["Setting"]["OcrBackendDesc"],
            parent=parent, texts=['auto', 'cpu', 'dml'])
        self._add_card(self.backend_combo)

        # 检测模型档位（det）：定位文字框所用的模型
        tier_texts = [tr['Setting'][key] for key in ('ModelTierTiny', 'ModelTierSmall', 'ModelTierMedium')]
        self.det_tier_combo = ComboBoxSettingCard(
            configItem=config.detModelTier, icon=FluentIcon.SEARCH,
            title=tr["Setting"]["DetModelTier"],
            content=tr["Setting"]["DetModelTierDesc"],
            parent=parent, texts=tier_texts)
        self._add_card(self.det_tier_combo)

        # 识别模型档位（rec）：把文字框识别成文字所用的模型
        self.tier_combo = ComboBoxSettingCard(
            configItem=config.modelTier, icon=FluentIcon.PIE_SINGLE,
            title=tr["Setting"]["RecModelTier"],
            content=tr["Setting"]["RecModelTierDesc"],
            parent=parent, texts=tier_texts)
        self._add_card(self.tier_combo)

        # DML 设备选择：只列出实际枚举到的设备，序号与执行时绑定的
        # 设备索引严格一致（不再显示不存在的 GPU #2-#7 占位项）。
        # 下拉框宽度按卡片宽度 50% 固定，长显卡名省略号显示，防止撑出界面。
        devices = env_check.list_dml_devices()
        device_names = [tr['Setting']['AutoDevice']]
        device_names.extend(f"#{d['index']}  {d['name']}" for d in devices)
        self.device_combo = DeviceComboBoxSettingCard(
            configItem=config.dmlDeviceId, icon=FluentIcon.DEVELOPER_TOOLS,
            title=tr["Setting"]["DmlDevice"],
            content=tr["Setting"]["DmlDeviceDesc"],
            parent=parent, texts=device_names)
        self._add_card(self.device_combo)

        # 是否启用硬件加速
        self.hardware_acceleration = SwitchSettingCard(
            configItem=config.hardwareAcceleration, icon=FluentIcon.SPEED_HIGH,
            title=tr["Setting"]["HardwareAcceleration"],
            content=tr["Setting"]["HardwareAccelerationDesc"], parent=parent)
        self._add_card(self.hardware_acceleration)
        if not HARDWARD_ACCELERATION_OPTION:
            self.hardware_acceleration.switchButton.setChecked(False)
            self.hardware_acceleration.switchButton.setEnabled(False)
            config.set(config.hardwareAcceleration, False)

        # 是否生成 TXT
        self.generate_txt_switch = SwitchSettingCard(
            configItem=config.generateTxt, icon=FluentIcon.DOCUMENT,
            title=tr["Setting"]["GenerateTxt"], content="", parent=parent)
        self._add_card(self.generate_txt_switch)

        # 是否重新分词
        self.word_segmentation = SwitchSettingCard(
            configItem=config.wordSegmentation, icon=FluentIcon.EDIT,
            title=tr["Setting"]["WordSegmentation"],
            content=tr["Setting"]["WordSegmentationDesc"], parent=parent)
        self._add_card(self.word_segmentation)

        # 设置面板高于卡片总高，剩余空间必须由尾部 stretch 吸收；
        # 否则 QVBoxLayout 会把多余高度均摊到每个间隙（间隙被撑到 ~14px）。
        self.addStretch(1)

    def _add_card(self, card):
        """统一卡片高度并加入布局。

        必须在 addWidget 之前调用 setFixedHeight：qfluentwidgets 的卡片在构造时
        自带 sizeHint（有说明=70px / 无说明=50px），若先 addWidget 布局会缓存这个
        旧高度、即使之后改高度仍按旧槽位分配，导致卡片间出现十几像素的空隙。
        """
        card.setFixedHeight(self.CARD_HEIGHT)
        self.addWidget(card)
