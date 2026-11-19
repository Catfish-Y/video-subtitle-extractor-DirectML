#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author  : Fang Yao（原作者） / 改写：Jason Eric
@Time    : 2021/4/1 6:07 下午（原始时间）
@FileName: gui.py
@desc: 字幕提取器图形化界面（由 PySimpleGUI 改写为 PySide6）
"""

import sys
import os
import configparser
import cv2
import multiprocessing
from PySide6.QtCore import Qt, QTimer
from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import (FluentWindow, PushButton, Slider, ProgressBar, PlainTextEdit,
                          setTheme, Theme, FluentIcon, CardWidget, SettingCardGroup,
                          ComboBoxSettingCard, SwitchSettingCard, setThemeColor, OptionsConfigItem,
                          OptionsValidator, SubtitleLabel, HollowHandleStyle, qconfig, ConfigItem, QConfig,
                          NavigationWidget, NavigationItemPosition, isDarkTheme, InfoBar)

from qframelesswindow.utils import getSystemAccentColor
from backend.config import config, tr, VERSION
from backend.tools.theme_listener import SystemThemeListener
from backend.tools.process_manager import ProcessManager
from ui.advanced_setting_interface import AdvancedSettingInterface
from ui.home_interface import HomeInterface
from ui.timeline_sync_interface import TimelineSyncInterface


class SubtitleExtractorGUI(FluentWindow):
    # 窗口尺寸策略：首次启动用DEFAULT尺寸，随后把尺寸/位置都夹到屏幕可用区域内。
    # 注意：右侧设置卡片的内容标签会撑出约1871x920的布局minimumSizeHint，
    # 小屏上窗口会被撑得比屏幕还大，必须显式 setMinimumSize 覆盖该提示。
    DEFAULT_WINDOW_W = 1200
    DEFAULT_WINDOW_H = 800
    MIN_WINDOW_W = 900
    MIN_WINDOW_H = 600
    SCREEN_MARGIN = 40

    def __init__(self):
        super().__init__()
        # 禁用云母效果
        self.setMicaEffectEnabled(False)
        # 设置深色主题并跟随系统主题色
        # setTheme(Theme.LIGHT)
        # setThemeColor(getSystemAccentColor(), save=True)

        # 初始化系统主题监听器并连接信号
        # self.themeListener = SystemThemeListener(self)
        # self.themeListener.start()
        # 任何尺寸下都悬浮展开, 防止窗口撑大
        self.navigationInterface.panel.minimumExpandWidth = 999999
        # 设置窗口图标
        self.setWindowIcon(QtGui.QIcon("design/vse.ico"))
        self.setWindowTitle(tr['SubtitleExtractorGUI']['Title'] + " v" + VERSION)
        # 创建界面布局
        self._create_layout()
        self._connectSignalToSlot()
        self._lazy_check_update()

    def _lazy_check_update(self):
        """ 延迟检查更新 """
        if not config.checkUpdateOnStartup.value:
            return
        self.check_update_timer = QtCore.QTimer(self)
        self.check_update_timer.setSingleShot(True)
        self.check_update_timer.timeout.connect(lambda: self.advancedSettingInterface.check_update(ignore=True))
        self.check_update_timer.start(2000)

    def _connectSignalToSlot(self):
        config.appRestartSig.connect(self._showRestartTooltip)

    def _showRestartTooltip(self):
        """ show restart tooltip """
        InfoBar.success(
            'Updated successfully',
            'Configuration takes effect after restart',
            duration=5000,
            parent=self
        )

    def _create_layout(self):
        # 创建主页面和高级设置页面
        self.homeInterface = HomeInterface(self)
        self.homeInterface.setObjectName("HomeInterface")
        self.timelineSyncInterface = TimelineSyncInterface(self)
        self.timelineSyncInterface.setObjectName("TimelineSyncInterface")
        self.advancedSettingInterface = AdvancedSettingInterface(self)
        self.advancedSettingInterface.setObjectName("AdvancedSettingInterface")
        
        # 添加到主窗口作为子界面
        self.addSubInterface(self.homeInterface,FluentIcon.HOME, tr['SubtitleExtractorGUI']['Title'])
        self.addSubInterface(self.timelineSyncInterface,FluentIcon.FONT, tr['TimelineSync']['Title'])
        self.addSubInterface(self.advancedSettingInterface, FluentIcon.SETTING, tr['Setting']['AdvancedSetting'], NavigationItemPosition.BOTTOM)

    def switchTo(self, interface: QWidget):
        super().switchTo(interface)
        if interface == self.advancedSettingInterface:
            QTimer.singleShot(222, lambda: self.advancedSettingInterface.macos_scrollarea_issue_workaround())
            # try again
            QTimer.singleShot(666, lambda: self.advancedSettingInterface.macos_scrollarea_issue_workaround())

    def closeEvent(self, event):
        """程序关闭时保存窗口位置并恢复标准输出和标准错误"""
        self.save_window_position()
        # 断开信号连接
        # self.themeListener.terminate()
        # self.themeListener.deleteLater()
        ProcessManager().instance().terminate_all()
        super().closeEvent(event)

    def save_window_position(self):
        """保存窗口位置到配置文件"""
        # 保存窗口位置和大小
        config.set(config.windowX, self.x())
        config.set(config.windowY, self.y())
        config.set(config.windowW, self.width())
        config.set(config.windowH, self.height())

    def _screen_rect(self):
        """当前主屏的可用区域（已扣除任务栏等）。"""
        return QtWidgets.QApplication.primaryScreen().availableGeometry()

    def _clamp_size(self, width, height):
        """把窗口尺寸限制在屏幕可用区域内，并留出边距。"""
        screen_rect = self._screen_rect()
        max_w = max(400, screen_rect.width() - self.SCREEN_MARGIN)
        max_h = max(300, screen_rect.height() - self.SCREEN_MARGIN)
        width = max(300, min(int(width), max_w))
        height = max(240, min(int(height), max_h))
        return width, height

    def load_window_position(self):
        """读取窗口位置/大小，并保证完整落在当前屏幕内。

        关键点：
        1. 必须显式 setMinimumSize，否则布局的 minimumSizeHint(约1871x920)
           会把窗口撑出小屏幕；这里给出一个能在小屏上放下的下限。
        2. 首次启动（X/Y 为空）或无有效历史几何时，按默认尺寸居中显示。
        3. 历史几何超出屏幕（如换到小屏显示器）时，夹取尺寸与左上角坐标。
        """
        # 覆盖布局撑起的最小尺寸，使其不超过当前屏幕可用区域
        min_w, min_h = self._clamp_size(self.MIN_WINDOW_W, self.MIN_WINDOW_H)
        self.setMinimumSize(min_w, min_h)

        # 首次启动：按默认尺寸居中（不能只 center，否则停留在 minimumSizeHint）
        try:
            x = config.windowX.value
            y = config.windowY.value
            if not x or not y:
                width, height = self._clamp_size(self.DEFAULT_WINDOW_W, self.DEFAULT_WINDOW_H)
                self.resize(width, height)
                self.center_window()
                return
        except Exception as e:
            print(e)
            self.center_window()
            return

        # 有历史几何：把尺寸与位置都夹到屏幕内
        try:
            screen_rect = self._screen_rect()
            width, height = self._clamp_size(config.windowW.value, config.windowH.value)
            x = max(screen_rect.x(), min(int(x), screen_rect.x() + screen_rect.width() - width))
            y = max(screen_rect.y(), min(int(y), screen_rect.y() + screen_rect.height() - height))
            self.setGeometry(x, y, width, height)
        except Exception as e:
            print(e)
            self.center_window()

    def center_window(self):
        """将窗口居中显示（尺寸已在 load_window_position 中限制）。"""
        screen_rect = self._screen_rect()
        window_rect = self.frameGeometry()
        center_point = screen_rect.center()
        window_rect.moveCenter(center_point)
        self.move(window_rect.topLeft())

    def keyPressEvent(self, event):
        """处理键盘事件"""
        # 检测Ctrl+C组合键
        if event.key() == QtCore.Qt.Key_C and event.modifiers() == QtCore.Qt.ControlModifier:
            print("\n程序被用户中断(Ctrl+C)，正在退出...")
            self.close()
        else:
            super().keyPressEvent(event)


if __name__ == '__main__':
    multiprocessing.set_start_method("spawn")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings)
    window = SubtitleExtractorGUI()
    # 先设置透明, 再显示, 否则会有闪烁的效果
    window.setWindowOpacity(0.0)
    window.show()
    window.load_window_position()
    # 使用动画效果逐渐显示窗口
    animation = QtCore.QPropertyAnimation(window, b"windowOpacity")
    animation.setDuration(300)  # 300毫秒的动画
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.start()
    app.exec()