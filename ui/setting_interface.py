from PySide6 import QtWidgets
from qfluentwidgets import (FluentIcon, ComboBoxSettingCard, SwitchSettingCard,
                            PushSettingCard, InfoBar)
from backend.config import config, tr, HARDWARD_ACCELERATION_OPTION
from backend.tools import env_check


class SettingInterface(QtWidgets.QVBoxLayout):
    """主页面的常用设置区：语言、模型、后端、设备与模型状态。"""

    def __init__(self, parent):
        super().__init__()
        self.setContentsMargins(16, 16, 16, 16)
        self.parent = parent

        # 界面语言设置
        self.interface_combo = ComboBoxSettingCard(
            configItem=config.interface, icon=FluentIcon.LANGUAGE,
            title=tr["LanguageModeGUI"]["InterfaceLanguage"], content="",
            parent=parent, texts=config.intefaceTexts.keys())
        self.addWidget(self.interface_combo)

        # 字幕语言设置
        self.language_combo = ComboBoxSettingCard(
            configItem=config.language, icon=FluentIcon.FONT,
            title=tr["LanguageModeGUI"]["SubtitleLanguage"], content="",
            parent=parent,
            texts=[tr['Language'][i] for i in config.language.validator.options])
        self.addWidget(self.language_combo)

        # 识别模式设置（抽帧策略）
        self.mode_combo = ComboBoxSettingCard(
            configItem=config.mode, icon=FluentIcon.SETTING,
            title=tr["LanguageModeGUI"]["Mode"], content="",
            parent=parent, texts=[tr['Mode'][i] for i in config.mode.validator.options])
        self.addWidget(self.mode_combo)

        # OCR 后端选择
        self.backend_combo = ComboBoxSettingCard(
            configItem=config.ocrBackend, icon=FluentIcon.ROBOT,
            title=tr["Setting"]["OcrBackend"],
            content=tr["Setting"]["OcrBackendDesc"],
            parent=parent, texts=['auto', 'cpu', 'dml'])
        self.addWidget(self.backend_combo)

        # 模型档位选择
        self.tier_combo = ComboBoxSettingCard(
            configItem=config.modelTier, icon=FluentIcon.PIE_SINGLE,
            title=tr["Setting"]["ModelTier"],
            content=tr["Setting"]["ModelTierDesc"],
            parent=parent,
            texts=[tr['Setting']['ModelTierLight'] if 'ModelTierLight' in tr['Setting'] else 'light',
                   tr['Setting']['ModelTierPrecise'] if 'ModelTierPrecise' in tr['Setting'] else 'precise'])
        self.addWidget(self.tier_combo)

        # DML 设备选择（选项与 config.dmlDeviceId 的 validator 对齐：'' + '0'..'7'）
        devices = env_check.list_dml_devices()
        device_by_index = {str(d['index']): d['name'] for d in devices}
        device_names = [tr['Setting']['AutoDevice']]
        for i in range(8):
            device_names.append(device_by_index.get(str(i), f'GPU #{i}'))
        self.device_combo = ComboBoxSettingCard(
            configItem=config.dmlDeviceId, icon=FluentIcon.DEVELOPER_TOOLS,
            title=tr["Setting"]["DmlDevice"],
            content=tr["Setting"]["DmlDeviceDesc"],
            parent=parent, texts=device_names)
        self.addWidget(self.device_combo)

        # 是否启用硬件加速
        self.hardware_acceleration = SwitchSettingCard(
            configItem=config.hardwareAcceleration, icon=FluentIcon.SPEED_HIGH,
            title=tr["Setting"]["HardwareAcceleration"],
            content=tr["Setting"]["HardwareAccelerationDesc"], parent=parent)
        self.addWidget(self.hardware_acceleration)
        if not HARDWARD_ACCELERATION_OPTION:
            self.hardware_acceleration.switchButton.setChecked(False)
            self.hardware_acceleration.switchButton.setEnabled(False)
            config.set(config.hardwareAcceleration, False)

        # 是否生成 TXT
        self.generate_txt_switch = SwitchSettingCard(
            configItem=config.generateTxt, icon=FluentIcon.DOCUMENT,
            title=tr["Setting"]["GenerateTxt"], content="", parent=parent)
        self.addWidget(self.generate_txt_switch)

        # 是否重新分词
        self.word_segmentation = SwitchSettingCard(
            configItem=config.wordSegmentation, icon=FluentIcon.EDIT,
            title=tr["Setting"]["WordSegmentation"],
            content=tr["Setting"]["WordSegmentationDesc"], parent=parent)
        self.addWidget(self.word_segmentation)

        # 模型检测卡片（显示状态 + 一键下载）
        self.model_check_card = PushSettingCard(
            tr['Setting']['ModelCheck'], FluentIcon.DOWNLOAD,
            tr['Setting']['ModelCheck'], '', parent)
        self.model_check_card.clicked.connect(self._on_model_check_clicked)
        self.addWidget(self.model_check_card)

        self.env_info = None
        self.refresh_environment()

    def refresh_environment(self):
        """启动时调用：检测 DML 设备与模型状态，更新提示。"""
        self.env_info = env_check.detect_environment()
        if self.env_info['ready']:
            self.model_check_card.setContent(tr['Setting']['ModelReady'])
        else:
            self.model_check_card.setContent(env_check.describe_missing(self.env_info))
        if not self.env_info['dml_devices']:
            InfoBar.warning(
                title=tr['Setting']['NoDmlDevice'], content='', duration=4000,
                parent=self.parent)

    def _on_model_check_clicked(self):
        """点击模型卡片：缺失则下载，齐全则重新检测。"""
        if self.env_info is None or self.env_info['ready']:
            self.refresh_environment()
            return
        info = self.env_info

        def progress(msg):
            self.model_check_card.setContent(msg)

        def done(success, msg):
            self.model_check_card.setContent(msg)
            if success:
                self.refresh_environment()

        env_check.download_missing_models(info, progress, done)
