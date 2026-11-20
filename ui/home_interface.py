import os
import cv2
import threading
import multiprocessing
import time
import traceback
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout
from PySide6.QtCore import Slot, QRect, Signal, Qt, QTimer
from PySide6.QtGui import QTextCharFormat, QColor
from PySide6 import QtWidgets
from qfluentwidgets import (PushButton, CardWidget, PlainTextEdit, FluentIcon, SmoothScrollArea)
from ui.setting_interface import SettingInterface
from ui.component.video_display_component import VideoDisplayComponent
from ui.component.task_list_component import TaskListComponent, TaskStatus, TaskOptions
from ui.icon.my_fluent_icon import MyFluentIcon
from backend.config import config, tr
from backend.tools.subtitle_extractor_remote_call import SubtitleExtractorRemoteCall
from backend.tools.process_manager import ProcessManager
from backend.tools import video_decoder


class _PreviewWorker:
    """单个 capture 的专属解码线程。

    cv2.VideoCapture 不是线程安全的，且 D3D11 挂起后 set/read/release 都可能
    在 C++ 层无限阻塞，因此每个 capture 的全部操作（open/set/read/release）
    都在它的专属线程内执行；看门狗超时时直接弃用该线程与 capture
    （不 release，避免阻塞），由调度器重建软解 worker。
    seek 请求只保留最新一条（拖动中间帧全部丢弃）。
    """

    _WATCHDOG_SECONDS = 8      # 单次 seek+read 超过该时长视为硬解挂起

    def __init__(self, loader, kind, path, hw):
        self._loader = loader
        self.kind = kind               # 'user' / 'progress'
        self._path = path
        self._hw = hw                  # True=D3D11 优先（仅 user）；False=固定软解
        self._queue_lock = threading.Lock()
        self._queue_wake = threading.Event()
        self._pending = None           # (frame_no, gen)
        self._stopped = False
        self._abandoned = False        # 看门狗弃用后不再操作 cap
        self.cap = None
        self.did = 'sw'
        self.meta = None
        self.first_frame = None
        self.load_ok = False
        self._thread = threading.Thread(
            target=self._run, name=f'PreviewWorker-{kind}', daemon=True)
        self._thread.start()

    def _open(self):
        """在专属线程内打开 capture 并预读首帧（带看门狗，防硬解打开挂起）。"""
        result = {}

        def _work():
            try:
                if self._hw:
                    cap, did = video_decoder.open_preview_capture(self._path)
                else:
                    cap, did = video_decoder._open_cv2(self._path), 'sw'
                result['cap'] = cap
                result['did'] = did
                if not cap.isOpened():
                    return
                ret, frame = cap.read()
                if not ret:
                    return
                result['frame'] = frame
                result['meta'] = {
                    'frame_count': int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
                    'frame_width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                    'frame_height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    'fps': cap.get(cv2.CAP_PROP_FPS),
                }
            except Exception:
                pass
            finally:
                # 若打开耗时超过看门狗窗口、worker 已被弃用，迟到的 cap
                # 无人消费，这里主动释放，避免 D3D11 设备/ffmpeg 进程泄漏
                if self._abandoned and result.get('cap') is not None:
                    try:
                        result['cap'].release()
                    except Exception:
                        pass

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(self._WATCHDOG_SECONDS if self._hw else 30)
        if t.is_alive():
            # 打开/首帧挂起：弃用（cap 可能已创建但卡在 C++ 层，不 release）
            with self._queue_lock:
                self._abandoned = True
            if self._hw:
                video_decoder.mark_preview_hw_failed(self._path)
            self.load_ok = False
            return
        self.cap = result.get('cap')
        self.did = result.get('did', 'sw')
        if 'meta' in result:
            self.first_frame = result['frame']
            self.meta = result['meta']
            self.load_ok = True

    def post(self, frame_no, gen):
        with self._queue_lock:
            if self._stopped or self._abandoned:
                return
            self._pending = (int(frame_no), gen)
        self._queue_wake.set()

    def stop(self):
        with self._queue_lock:
            self._stopped = True
            self._pending = None
        self._queue_wake.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def _run(self):
        self._open()
        self._loader._on_worker_opened(self)
        if self._abandoned:
            return  # 打开即挂起被弃用：不进入循环，cap 不 release
        while True:
            self._queue_wake.wait()
            self._queue_wake.clear()
            with self._queue_lock:
                if self._stopped or self._abandoned:
                    break
                req = self._pending
                self._pending = None
            if req is None or not self.load_ok:
                continue
            frame_no, gen = req
            self._seek_and_emit(frame_no, gen)
        with self._queue_lock:
            abandoned = self._abandoned
        if not abandoned and self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

    def _seek_and_emit(self, frame_no, gen):
        result = {}

        def _work():
            try:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ret, frame = self.cap.read()
                result['ok'] = ret
                result['frame'] = frame
            except Exception:
                result['ok'] = False

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        if self.did != 'sw':
            worker.join(self._WATCHDOG_SECONDS)
            if worker.is_alive():
                # D3D11 挂起（DXGI 设备移除后 cv2 无限阻塞）：弃用本 worker
                # （cap 不 release，操作线程留在 C++ 层），标记该视频，
                # 由调度器重建软解 worker 并重试本次请求。
                with self._queue_lock:
                    self._abandoned = True
                video_decoder.mark_preview_hw_failed(self._path)
                self._loader._on_worker_hung(self, frame_no, gen)
                return
        else:
            worker.join()
        if not result.get('ok'):
            return
        if self._loader._is_stale(gen):
            return
        with self._queue_lock:
            superseded = self._pending is not None
        if superseded and self.kind == 'user':
            return  # 已有更新的拖动请求，丢弃过期帧
        self._loader._emit_frame(frame_no, result['frame'])


class PreviewLoader:
    """预览解码调度器：UI 线程只投递请求，解码全部在后台线程完成。

    双 worker 设计（各自独占一个 capture，互不干扰 seek 位置）：
    - user worker：D3D11 硬解优先、失败回退软解（open_preview_capture），
      服务拖动进度条 / 快捷键 / 打开文件等交互预览；
    - progress worker：固定软解，服务提取中的进度回写预览（真随机 seek，
      顺序递增读帧极快，不会像 QSV/NVDEC 那样从头解码）。

    请求合并：每个队列只保留最新请求（拖动中间帧全部丢弃）；
    交互请求 40ms 防抖。D3D11 seek 挂起由看门狗兜底：超时弃用硬解、
    标记该视频、重建软解 worker 重试。
    回调（on_frame/on_loaded/on_load_failed）由 HomeInterface 通过 Qt
    信号排队到主线程执行，后台线程不直接触碰任何 widget。
    """

    _DEBOUNCE_MS = 40          # 交互请求防抖窗口

    def __init__(self, on_frame, on_loaded, on_load_failed):
        self._on_frame = on_frame              # callable(frame_no, frame)
        self._on_loaded = on_loaded            # callable(gen, meta, first_frame)
        self._on_load_failed = on_load_failed  # callable(gen, path)
        self._lock = threading.Lock()
        self._generation = 0
        self._path = None
        self._user_worker = None
        self._progress_worker = None
        self._stop = False
        self._load_waiters = {}                # {gen: (Event, holder)}
        self._debounce_timer = None            # threading.Timer 防抖

    # ---- 调用接口（UI 线程 / 提取工作线程） ----

    def load(self, video_path):
        """异步加载新视频，返回代数 gen。旧 worker 停止并自行释放。"""
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._path = str(video_path)
            self._stop_workers_locked()
            self._spawn_workers_locked()
        return gen

    def load_sync(self, video_path, timeout=30):
        """同步加载（提取工作线程用）：阻塞等待 user worker 打开完成。

        返回 (ok, meta, gen)。加载完成后照常触发 on_loaded 刷新预览。
        """
        done = threading.Event()
        holder = {}
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._path = str(video_path)
            self._stop_workers_locked()
            self._spawn_workers_locked()
            self._load_waiters[gen] = (done, holder)
        if not done.wait(timeout):
            with self._lock:
                self._load_waiters.pop(gen, None)
            return False, None, gen
        return holder.get('ok', False), holder.get('meta'), gen

    def request(self, frame_no, kind='user'):
        """投递 seek 请求。kind='user' 交互 / 'progress' 进度回写。"""
        with self._lock:
            if self._stop or self._user_worker is None:
                return
            worker = self._progress_worker if kind == 'progress' else self._user_worker
            gen = self._generation
            if kind == 'user':
                # 防抖：窗口内的连续拖动只保留最后一次（Timer 重置）
                if self._debounce_timer is not None:
                    self._debounce_timer.cancel()
                self._debounce_timer = threading.Timer(
                    self._DEBOUNCE_MS / 1000.0,
                    self._post_user, args=(frame_no, gen))
                self._debounce_timer.daemon = True
                self._debounce_timer.start()
                return
        if worker is not None:
            worker.post(frame_no, gen)

    def _post_user(self, frame_no, gen):
        with self._lock:
            self._debounce_timer = None
            worker = self._user_worker
            current_gen = self._generation
            stopped = self._stop
        if worker is not None and current_gen == gen and not stopped:
            worker.post(frame_no, gen)

    def shutdown(self):
        with self._lock:
            self._stop = True
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            workers = [w for w in (self._user_worker, self._progress_worker) if w is not None]
            self._stop_workers_locked()
        # 等待 worker 退出（正常情况 <100ms；被弃用的硬解 worker 不阻塞退出）
        for w in workers:
            w.join(timeout=2)

    # ---- 内部 ----

    def _spawn_workers_locked(self):
        self._user_worker = _PreviewWorker(self, 'user', self._path, hw=True)
        self._progress_worker = _PreviewWorker(self, 'progress', self._path, hw=False)

    def _stop_workers_locked(self):
        for w in (self._user_worker, self._progress_worker):
            if w is not None:
                w.stop()
        self._user_worker = None
        self._progress_worker = None

    def _is_stale(self, gen):
        with self._lock:
            return self._stop or gen != self._generation

    def _on_worker_opened(self, worker):
        """worker 打开完成回调（worker 线程）。user worker 决定加载结果。

        D3D11 打开失败/挂起时自动重建为软解 worker 重试一次；
        软解仍失败才向 UI 报告加载失败。
        """
        if worker.kind != 'user':
            return
        rebuild = False
        with self._lock:
            gen = self._generation
            stale = self._stop or worker is not self._user_worker
            path = self._path
            if not stale and not worker.load_ok and worker._hw:
                # 硬解失败 → 重建软解 worker（标记避免再次尝试硬解）
                video_decoder.mark_preview_hw_failed(path)
                self._user_worker = _PreviewWorker(self, 'user', path, hw=False)
                rebuild = True
            waiter = self._load_waiters.get(gen) if not stale and not rebuild else None
            if waiter is not None and not rebuild:
                self._load_waiters.pop(gen, None)
        if stale:
            return
        if rebuild:
            return  # 新软解 worker 打开后会再次回调本方法
        if waiter is not None:
            done, holder = waiter
            holder['ok'] = worker.load_ok
            holder['meta'] = worker.meta
            done.set()
        if worker.load_ok:
            self._on_loaded(gen, worker.meta, worker.first_frame)
        else:
            self._on_load_failed(gen, path)

    def _on_worker_hung(self, worker, frame_no, gen):
        """D3D11 挂起：重建 user worker 为软解并重试本次请求。"""
        with self._lock:
            if self._stop or gen != self._generation or worker is not self._user_worker:
                return
            self._user_worker = _PreviewWorker(self, 'user', self._path, hw=False)
            new_worker = self._user_worker
        if frame_no is not None:
            new_worker.post(frame_no, gen)

    def _emit_frame(self, frame_no, frame):
        if frame is not None:
            self._on_frame(frame_no, frame)


class HomeInterface(QWidget):
    progress_signal = Signal(int, int, int, bool, int)
    append_log_signal = Signal(list)
    task_error_signal = Signal(object)
    # 预览后台线程 → 主线程的排队信号（后台线程不直接触碰 widget）
    preview_frame_signal = Signal(int, object)
    video_loaded_signal = Signal(int, object, object)
    video_load_failed_signal = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("HomeInterface")
        # 初始化一些变量
        self.video_path = None
        self.fps = None
        self.frame_count = None
        self.frame_width = None
        self.frame_height = None
        self.se = None  # 后台字幕提取器
        self._preview_gen = -1          # 当前预览对应的加载代数
        self._slider_resetting = False  # 程序重置滑块时不触发预览请求

        # 添加自动滚动控制标志
        self.auto_scroll = True
        self.running_task = False
        self.running_process = None
        self.running_sub_process_pids = []

        # 当前正在处理的任务索引
        self.current_processing_task_index = -1

        # 预览解码后台线程（D3D11 优先/软解回退 + 进度专用软解）
        self.preview_loader = PreviewLoader(
            on_frame=lambda frame_no, frame: self.preview_frame_signal.emit(frame_no, frame),
            on_loaded=lambda gen, meta, first: self.video_loaded_signal.emit(gen, meta, first),
            on_load_failed=lambda gen, path: self.video_load_failed_signal.emit(gen, path),
        )
        self.preview_frame_signal.connect(self._on_preview_frame)
        self.video_loaded_signal.connect(self._on_video_loaded)
        self.video_load_failed_signal.connect(self._on_video_load_failed)
        # 进度回写节流：提取中最多 5fps 触发预览解码，避免无谓的密集 seek
        self._last_progress_preview = 0.0

        self.__init_widgets()
        self.progress_signal.connect(self.update_progress)
        self.append_log_signal.connect(self.append_log)
        self.task_error_signal.connect(self.on_task_error)
        print(tr['Main']['AcceleratorWarning'])

    def __init_widgets(self):
        """创建主页面"""
        main_layout = QHBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # 左侧视频区域
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)
        
        # 创建视频显示组件
        self.video_display_component = VideoDisplayComponent(self)
        self.video_display_component.ab_sections_changed.connect(self.ab_sections_changed)
        self.video_display_component.selections_changed.connect(self.selections_changed)
        left_layout.addWidget(self.video_display_component)
        
        # 获取视频显示和滑块的引用
        self.video_display = self.video_display_component.video_display
        self.video_slider = self.video_display_component.video_slider
        self.video_slider.valueChanged.connect(self.slider_changed)
        
        # 输出文本区域
        self.output_text = PlainTextEdit()
        self.output_text.setMinimumHeight(150)
        self.output_text.setReadOnly(True)
        self.output_text.document().setDocumentMargin(10)
        # 连接滚动条值变化信号
        self.output_text.verticalScrollBar().valueChanged.connect(self.on_scroll_change)
        
        output_container = CardWidget(self)
        output_layout = QVBoxLayout()
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_text)
        output_container.setLayout(output_layout)
        left_layout.addWidget(output_container)

        main_layout.addLayout(left_layout, 3)

        # 右侧设置区域
        right_layout = QVBoxLayout()
        right_layout.setSpacing(8)

        # 设置容器：放进滚动区域，卡片保持自己的固定高度与均匀间距，
        # 空间不足时滚动而不是把卡片压缩到互相重叠。
        settings_container = CardWidget(self)
        settings_container.setLayout(SettingInterface(settings_container))
        settings_container.setMinimumWidth(0)
        settings_scroll = SmoothScrollArea(self)
        settings_scroll.setWidget(settings_container)
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        settings_scroll.setMinimumHeight(200)
        settings_scroll.setMinimumWidth(320)
        # 面板高度按卡片高度/间距精确适配：空间充足时最多显示完整 9 张卡片，
        # 窗口变小时由布局压缩并出现滚动条；剩余空间让给下方任务列表。
        # +8px 缓冲抵消滚动区边框，避免刚好差几像素时出现滚动条。
        settings_scroll.setMaximumHeight(SettingInterface.content_height() + 8)
        right_layout.addWidget(settings_scroll, 8)

        # 添加任务列表容器
        task_list_container = CardWidget(self)
        task_list_layout = QHBoxLayout()
        task_list_layout.setContentsMargins(0, 0, 0, 0)
        task_list_layout.setSpacing(0)
        self.task_list_component = TaskListComponent(self)
        self.task_list_component.task_selected.connect(self.on_task_selected)
        self.task_list_component.task_deleted.connect(self.on_task_deleted)
        task_list_layout.addWidget(self.task_list_component)
        task_list_container.setLayout(task_list_layout)
        task_list_container.setMinimumHeight(70)
        right_layout.addWidget(task_list_container, 1)  # 占满剩余空间
        
        # 操作按钮容器
        button_container = CardWidget(self)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(16, 10, 16, 10)
        button_layout.setSpacing(8)
        
        self.file_button = PushButton(tr['SubtitleExtractorGUI']['Open'], self)
        self.file_button.setIcon(FluentIcon.FOLDER)
        self.file_button.clicked.connect(self.open_file)
        button_layout.addWidget(self.file_button)
        
        self.run_button = PushButton(tr['SubtitleExtractorGUI']['Run'], self)
        self.run_button.setIcon(FluentIcon.PLAY)
        self.run_button.clicked.connect(self.run_button_clicked)
        button_layout.addWidget(self.run_button)
        
        self.stop_button = PushButton(tr['SubtitleExtractorGUI']['Stop'], self)
        self.stop_button.setIcon(MyFluentIcon.Stop)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self.stop_button_clicked)
        
        button_layout.addWidget(self.stop_button)
        
        button_container.setLayout(button_layout)
        right_layout.addWidget(button_container)

        main_layout.addLayout(right_layout, 2)
    
    def on_scroll_change(self, value):
        """监控滚动条位置变化"""
        scrollbar = self.output_text.verticalScrollBar()
        # 如果滚动到底部，启用自动滚动
        if value == scrollbar.maximum():
            self.auto_scroll = True
        # 如果用户向上滚动，禁用自动滚动
        elif self.auto_scroll and value < scrollbar.maximum():
            self.auto_scroll = False

    
    def slider_changed(self, value):
        """滑块变化 → 只投递请求，解码在后台线程完成（不阻塞 UI）。

        提取进行中的进度回写也走这里，但改用进度专用软解 capture，
        避免与用户拖动互相干扰 seek 位置，也避免硬解管道从头解码。
        """
        if self._slider_resetting:
            return
        kind = 'progress' if self.running_task else 'user'
        if kind == 'progress':
            now = time.monotonic()
            if now - self._last_progress_preview < 0.2:
                return
            self._last_progress_preview = now
        self.preview_loader.request(value, kind)

    @Slot(int, object)
    def _on_preview_frame(self, frame_no, frame):
        """后台解码完成回调（主线程）：帧号与滑块当前值一致才显示。"""
        if frame is None:
            return
        if frame_no != self.video_slider.value():
            return
        self.update_preview(frame)

    @Slot(int, object, object)
    def _on_video_loaded(self, gen, meta, first_frame):
        """视频加载完成回调（主线程）：设置元数据并显示首帧。"""
        if gen != self._preview_gen or meta is None:
            return
        self.frame_count = meta['frame_count']
        self.frame_width = meta['frame_width']
        self.frame_height = meta['frame_height']
        self.fps = meta['fps']
        self._slider_resetting = True
        try:
            self.video_slider.setMaximum(max(1, self.frame_count))
            self.video_slider.setValue(1)
        finally:
            self._slider_resetting = False
        self.video_display_component.set_dragger_enabled(True)
        if first_frame is not None:
            self.update_preview(first_frame)

    @Slot(int, str)
    def _on_video_load_failed(self, gen, path):
        """视频加载失败回调（主线程）。"""
        if gen != self._preview_gen:
            return
        self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoFailed']}: {path}")

    def ab_sections_changed(self, ab_sections):
        get_current_task_index = self.task_list_component.get_current_task_index()
        if get_current_task_index == -1:
            return
        self.task_list_component.update_task_option(get_current_task_index, TaskOptions.AB_SECTIONS, ab_sections)

    def selections_changed(self, selections):
        get_current_task_index = self.task_list_component.get_current_task_index()
        if get_current_task_index == -1:
            return
        self.task_list_component.update_task_option(get_current_task_index, TaskOptions.SUB_AREAS, selections)

    def on_task_selected(self, index, file_path):
        """处理任务被选中事件
        
        Args:
            index: 任务索引
            file_path: 文件路径
        """
        # 加载选中的视频进行预览
        self.load_video(file_path)
        ab_sections = self.task_list_component.get_task_option(index, TaskOptions.AB_SECTIONS, [])
        self.video_display_component.set_ab_sections(ab_sections)
        self.apply_task_selection(index)

    def apply_task_selection(self, index):
        """
        从任务列表中获取选区信息
        如果选区信息不存在, 则从已设置的选区中加载
        如果仍然不存在已设置选区则从配置文件中加载
        """
        selections = self.task_list_component.get_task_option(index, TaskOptions.SUB_AREAS, [])
        # 优先从已设置选区中加载
        if len(selections) <= 0:
            tasks = self.task_list_component.get_all_tasks()
            for i, _ in enumerate(tasks):
                selections = self.task_list_component.get_task_option(i, TaskOptions.SUB_AREAS, []).copy()
                if len(selections) > 0:
                    self.append_output('update subarea', selections)
                    self.task_list_component.update_task_option(index, TaskOptions.SUB_AREAS, selections)
                    break
        
        if len(selections) <= 0:
            selections = self.video_display_component.load_selections_from_config()

        self.video_display_component.set_selection_rects(selections)
        self.task_list_component.update_task_option(index, TaskOptions.SUB_AREAS, selections)
        return selections

    
    def on_task_deleted(self, index):
        """处理任务被删除事件
        
        Args:
            index: 任务索引
        """
        # 如果删除的是正在处理的任务，则需要更新状态
        if index == self.current_processing_task_index:
            self.current_processing_task_index = -1
        
        task = self.task_list_component.get_task(0)
        if task:
            # 如果还有任务，选中第一个
            self.task_list_component.select_task(0)

    def update_preview(self, frame):
        # 先缩放图像
        resized_frame = self._img_resize(frame)

        # 设置视频参数
        self.video_display_component.set_video_parameters(
            self.frame_width, self.frame_height, 
            self.scaled_width if hasattr(self, 'scaled_width') else None,
            self.scaled_height if hasattr(self, 'scaled_height') else None,
            self.border_left if hasattr(self, 'border_left') else 0,
            self.border_top if hasattr(self, 'border_top') else 0,
            self.fps if self.fps is not None else 30,
        )
        
        # 更新视频显示（这会同时保存current_pixmap）
        self.video_display_component.update_video_display(resized_frame)

    def _img_resize(self, image):
        height, width = image.shape[:2]
        
        video_preview_width = self.video_display_component.video_preview_width
        video_preview_height = self.video_display_component.video_preview_height
        # 计算等比缩放后的尺寸
        target_ratio = video_preview_width / video_preview_height
        image_ratio = width / height
        
        if image_ratio > target_ratio:
            # 宽度适配，高度按比例缩放
            new_width = video_preview_width
            new_height = int(new_width / image_ratio)
            top_border = (video_preview_height - new_height) // 2
            bottom_border = video_preview_height - new_height - top_border
            left_border = 0
            right_border = 0
        else:
            # 高度适配，宽度按比例缩放
            new_height = video_preview_height
            new_width = int(new_height * image_ratio)
            left_border = (video_preview_width - new_width) // 2
            right_border = video_preview_width - new_width - left_border
            top_border = 0
            bottom_border = 0
        
        # 先缩放图像
        resized = cv2.resize(image, (new_width, new_height))
        
        # 添加黑边以填充到目标尺寸
        padded = cv2.copyMakeBorder(
            resized, 
            top_border, bottom_border, 
            left_border, right_border, 
            cv2.BORDER_CONSTANT, 
            value=[0, 0, 0]
        )
        
        # 保存边框信息，用于坐标转换
        self.border_left = left_border / video_preview_width
        self.border_right = right_border / video_preview_width
        self.border_top = top_border / video_preview_height
        self.border_bottom = bottom_border / video_preview_height
        self.original_width = width
        self.original_height = height
        self.is_vertical = width < height
        self.scaled_width = new_width / video_preview_width
        self.scaled_height = new_height / video_preview_height
        
        return padded

    def stop_button_clicked(self):
        try:
            self.running_task = False
            for pid in self.running_sub_process_pids:
                ProcessManager.instance().terminate_by_pid(pid)
            running_process = self.running_process
            if running_process:
                ProcessManager.instance().terminate_by_process(running_process)
            # 更新任务状态为待处理
            if self.current_processing_task_index >= 0:
                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.PENDING)
        finally:    
            self.running_sub_process_pids = []
            self.running_process = None
            self.run_button.setVisible(True)
            self.stop_button.setVisible(False)

    def run_button_clicked(self):
        if not self.task_list_component.get_pending_tasks():
            self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFirst'])
            return
            
        try:
            # 获取所有待执行的任务
            pending_tasks = self.task_list_component.get_pending_tasks()
            if not pending_tasks:
                return
            
            self.run_button.setVisible(False)
            self.stop_button.setVisible(True)
            # 开启后台线程处理视频
            def task():
                self.running_task = True
                try:
                    while self.running_task:
                        try:
                            pending_tasks = self.task_list_component.get_pending_tasks()
                            if not pending_tasks:
                                print("No pending tasks. Exiting.")
                                break
                            pending_task = pending_tasks[0]
                            # 更新当前处理的任务索引
                            self.current_processing_task_index, task = pending_task
                            # 同步加载预览（后台解码线程执行，工作线程等待结果）
                            ok, meta, gen = self.preview_loader.load_sync(task.path)
                            self._preview_gen = gen
                            if not ok:
                                self.append_output(tr['SubtitleExtractorGUI']['OpenVideoFailed'].format(task.path))
                                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)
                                continue
                            self.video_path = task.path
                            self.frame_count = meta['frame_count']
                            self.frame_width = meta['frame_width']
                            self.frame_height = meta['frame_height']
                            self.fps = meta['fps']
                            
                            # 获取字幕区域坐标
                            subtitle_areas = self.apply_task_selection(self.current_processing_task_index)
                            if not subtitle_areas or len(subtitle_areas) <= 0:
                                self.append_output(tr['SubtitleExtractorGUI']['SelectSubtitleArea'].format(task.path))
                                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)
                                continue

                            self.video_display_component.save_selections_to_config()

                            # 更新任务状态为运行中
                            self.task_list_component.update_task_progress(self.current_processing_task_index, 0)
                            
                            # 选中当前任务
                            self.task_list_component.select_task(self.current_processing_task_index)
                            
                            self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.PROCESSING)
                            options = {}
                            for key in task.options:
                                value = task.options[key]
                                if key == TaskOptions.SUB_AREAS.value:
                                    value = self.video_display_component.preview_coordinates_to_video_coordinates(value)
                                    value = value[0] if len(value) > 0 else None
                                options[key] = value
                            # 清理缓存, 使用动态路径
                            task.output_path = None
                            output_path = task.output_path
                            process = self.run_subtitle_extractor_process(task.path, output_path, options)
                            
                            # 更新任务状态为已完成
                            task = self.task_list_component.get_task(self.current_processing_task_index)
                            if process.exitcode == 0 and task and task.status == TaskStatus.PROCESSING:
                                self.progress_signal.emit(100, 100, 100, True, 100)
                                # 任务完成, 更新输出路径为只读
                                task.output_path = output_path
                                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.COMPLETED)
                            else:
                                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)
                            
                        except Exception as e:
                            print(e)
                            self.append_output(f"Error: {e}")
                            # 更新任务状态为失败
                            if self.current_processing_task_index >= 0:
                                self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)
                            # 单个任务失败不应终止整个批量队列，标记失败后继续处理下一个
                            continue
                        finally:
                            # 预览 capture 由 PreviewLoader 统一管理，
                            # 下一个任务 load_sync 时自动释放旧的
                            time.sleep(1)
                finally:
                    self.running_task = False
                    self.run_button.setVisible(True)
                    self.stop_button.setVisible(False)

            threading.Thread(target=task, daemon=True).start()
        except Exception as e:
            print(traceback.format_exc())
            self.append_output(f"Error: {e}")
            # 没有待执行的任务，恢复按钮状态
            self.run_button.setVisible(True)
            self.stop_button.setVisible(False)

    @staticmethod
    def extractor_process(queue, video_path, output_path, options):
        """
        在子进程中执行字幕提取的函数
        
        Args:
            video_path: 视频文件路径
            output_path: 输出文件路径
            options: 选项
        """
        sr = None
        try:
            from backend.main import SubtitleExtractor
            sr = SubtitleExtractor(video_path)
            sr.subtitle_output_path = output_path
            for key in options:
                setattr(sr, key, options[key])
            sr.add_progress_listener(lambda progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post=0: SubtitleExtractorRemoteCall.remote_call_update_progress(queue, progress_ocr, progress_frame_extract, progress_total, isFinished, progress_post))
            sr.append_output = lambda *args: SubtitleExtractorRemoteCall.remote_call_append_log(queue, args)
            sr.manage_process = lambda pid: SubtitleExtractorRemoteCall.remote_call_manage_process(queue, pid)
            sr.run()
        except Exception as e:
            traceback.print_exc()
            SubtitleExtractorRemoteCall.remote_call_catch_error(queue, e)
        finally:
            if sr:
                sr.isFinished = True
                sr.vsf_running = False
            SubtitleExtractorRemoteCall.remote_call_finish(queue)
            ProcessManager.instance().terminate_all()
            

    # 修改run_subtitle_extractor_process方法
    def run_subtitle_extractor_process(self, video_path, output_path, options):
        """
        使用多进程执行字幕提取，并等待进程完成
        
        Args:
            video_path: 视频文件路径
            output_path: 输出文件路径
            options: 任务选项
        """

        subtitle_extractor_remote_caller = SubtitleExtractorRemoteCall()
        subtitle_extractor_remote_caller.register_update_progress_callback(self.progress_signal.emit)
        subtitle_extractor_remote_caller.register_log_callback(self.append_log_signal.emit)
        subtitle_extractor_remote_caller.register_manage_process_callback(lambda pid: (
                ProcessManager.instance().add_pid(pid),
                self.running_sub_process_pids.append(pid),
            )
        )
        subtitle_extractor_remote_caller.register_error_callback(self.task_error_signal.emit)
        process = multiprocessing.Process(
            target=HomeInterface.extractor_process,
            args=(subtitle_extractor_remote_caller.queue, video_path, output_path, options)
        )
        try:
            if not self.running_task:
                return process
            process.start()
            ProcessManager.instance().add_process(process)
            self.running_process = process
            process.join()
            print(f"Process exited with code {process.exitcode}")
        finally:
            subtitle_extractor_remote_caller.stop()
        return process

    @Slot()
    def processing_finished(self):
        pending_tasks = self.task_list_component.get_pending_tasks()
        if pending_tasks:
            # 还有待执行任务, 忽略
            return
        # 处理完成后恢复界面可用性
        self.run_button.setVisible(True)
        self.stop_button.setVisible(False)
        self.se = None
        # 重置视频滑块（程序重置不触发预览请求）
        self._slider_resetting = True
        try:
            self.video_slider.setValue(1)
        finally:
            self._slider_resetting = False
        # 重置当前处理任务索引
        self.current_processing_task_index = -1

    @Slot(int, bool)
    def update_progress(self, progress_remover, progress_finder, progress_total, isFinished, progress_post=0):
        try:
            if self.frame_count:
                pos = min(self.frame_count - 1, int(progress_remover / 100 * self.frame_count))
                if pos != self.video_slider.value():
                    self.video_slider.setValue(pos)

            # 更新任务进度（加权：帧提取5% + OCR 90% + 后处理5%）
            if self.current_processing_task_index >= 0:
                displayed = min(100, int(progress_remover * 0.9 + progress_finder * 0.05 + progress_post * 0.05))
                # 确保进度不会回退
                if not hasattr(self, '_last_task_progress'):
                    self._last_task_progress = {}
                task_idx = self.current_processing_task_index
                last_progress = self._last_task_progress.get(task_idx, -1)
                if displayed >= last_progress or isFinished:
                    self._last_task_progress[task_idx] = displayed
                    self.task_list_component.update_task_progress(
                        task_idx,
                        displayed,
                    )
            
            # 检查是否完成
            if isFinished:
                self.processing_finished()
        except Exception as e:
            # 捕获任何异常，防止崩溃
            print(f"更新进度时出错: {str(e)}")

    @Slot(list)
    def append_log(self, log):
        self.append_output(*log)

    def append_output(self, *args):
        """添加文本到输出区域并控制滚动
        Args:
            *args: 要输出的内容，多个参数将用空格连接
        """
        text = ' '.join(str(arg) for arg in args).rstrip()

        # 根据消息类型选择颜色
        fmt = QTextCharFormat()
        if '【完成】' in text or '【结束】' in text:
            fmt.setForeground(QColor('#10b981'))
        elif '【处理中】' in text:
            fmt.setForeground(QColor('#3b82f6'))
        elif '【警告】' in text or '【重要提示】' in text:
            fmt.setForeground(QColor('#f59e0b'))
        elif text.startswith('---'):
            fmt.setForeground(QColor('#d1d5db'))
        elif '成功打开视频' in text:
            fmt.setForeground(QColor('#10b981'))
        elif '无法打开视频' in text or '请先打开视频' in text or '错误' in text or 'Error' in text:
            fmt.setForeground(QColor('#ef4444'))
        elif text.startswith('  '):
            fmt.setForeground(QColor('#6b7280'))

        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text + '\n', fmt)
        print(*args)  # 保持原始的 print 行为
        # 如果启用了自动滚动，则滚动到底部
        if self.auto_scroll:
            scrollbar = self.output_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    @Slot(object)
    def on_task_error(self, e):
        self.append_output(tr['SubtitleExtractorGUI']['ErrorDuringProcessing'].format(str(e)))
        if self.current_processing_task_index >= 0:
            self.task_list_component.update_task_status(self.current_processing_task_index, TaskStatus.FAILED)

    def load_video(self, video_path):
        """异步加载视频预览（D3D11 优先/软解回退，后台线程解码）。

        元数据与首帧通过 video_loaded_signal 回调设置，不阻塞 UI。
        """
        self.video_path = video_path
        self._preview_gen = self.preview_loader.load(video_path)

    def open_file(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            tr['SubtitleExtractorGUI']['Open'],
            "",
            "All Files (*.*);;MP4 Files (*.mp4);;FLV Files (*.flv);;WMV Files (*.wmv);;AVI Files (*.avi)"
        )
        if files:
            # 只校验文件可读性（打开即释放），不做完整解码；
            # 真正的预览加载只对第一个文件做一次（select_task 触发）。
            files_loaded = []
            for path in files:
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    cap.release()
                    self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoSuccess']}: {path}")
                    files_loaded.append(path)
                else:
                    self.append_output(f"{tr['SubtitleExtractorGUI']['OpenVideoFailed']}: {path}")
            # 正序添加, 确保任务列表顺序一致
            for path in files_loaded:
                self.task_list_component.add_task(path)
            if not files_loaded:
                return
            index = max(0, self.task_list_component.find_task_index_by_path(files_loaded[0]))
            self.task_list_component.select_task(index)

    def closeEvent(self, event):
        """窗口关闭时断开信号连接"""
        try:
            # 断开信号连接
            self.progress_signal.disconnect(self.update_progress)
            self.append_log_signal.disconnect(self.append_log)
            self.task_error_signal.disconnect(self.on_task_error)
            self.video_display_component.video_slider.valueChanged.disconnect(self.slider_changed)
            self.video_display_component.ab_sections_changed.disconnect(self.ab_sections_changed)
            self.video_display_component.selections_changed.disconnect(self.selections_changed)
            # 停止预览解码线程并释放 capture
            self.preview_loader.shutdown()

            # 确保所有子进程都已终止
            ProcessManager.instance().terminate_all()
        except Exception as e:
            print(f"Error during close window:", e)
        super().closeEvent(event)
    