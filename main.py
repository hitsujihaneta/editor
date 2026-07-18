import os
import glob
import json
import re
import hashlib
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QFont, QGuiApplication

from models import (
    FrameNumber, BoxList, Interval, Span, Lane, Box,
    EditorStore, _HEX_PALETTE, _PALETTE, _color_for_id,
)
from phase3_widgets import P3ImageView, P3Timeline, P3ControlPanel, Phase3Widget
from dialogs import IntervalEditor, FPSSettingsDialog, LayoutAdjusterDialog
from detection_editor_ui import UIBuilderMixin
from detection_editor_io import FileIOMixin
from detection_editor_logic import CoreLogicMixin

_PHASE3_AVAILABLE = True


class DetectionEditor(UIBuilderMixin, FileIOMixin, CoreLogicMixin, QtWidgets.QWidget):

    @property
    def detections(self) -> Dict:
        return self.store.detections

    @detections.setter
    def detections(self, val: Dict):
        self.store.detections = val

    @property
    def id_list(self) -> List[str]:
        return self.store.id_list

    @id_list.setter
    def id_list(self, val: List[str]):
        self.store.id_list = val

    @property
    def image_paths(self) -> List:
        return self.store.image_paths

    @image_paths.setter
    def image_paths(self, val: List):
        self.store.image_paths = val

    @property
    def image_folder(self) -> str:
        return self.store.image_folder

    @image_folder.setter
    def image_folder(self, val: str):
        self.store.image_folder = val

    def __init__(self):
        super().__init__()
        # ストアを最初に生成（プロパティアクセスより前に必要）
        self.store = EditorStore()
        self.setWindowTitle("PyQt Detection Editor (手動保存 / JSON・TXT対応 + 可変ID + 出場区間)")
        
        # QWidgetをメインウィンドウとして使うため、メニューバーを縦レイアウトのトップに追加
        self.main_vbox = QtWidgets.QVBoxLayout(self)
        self.main_vbox.setContentsMargins(0, 0, 0, 0)
        self.main_vbox.setSpacing(0)
        
        # メニューバーのセットアップ
        self._setup_menu_bar()
        self.main_vbox.addWidget(self.menu_bar)

        # フェーズ切り替えバー（メニューバー直下）
        phase_bar = QtWidgets.QWidget()
        phase_bar.setFixedHeight(32)
        phase_bar.setStyleSheet("background: #3c3c3c;")
        phase_h = QtWidgets.QHBoxLayout(phase_bar)
        phase_h.setContentsMargins(6, 2, 6, 2)
        phase_h.setSpacing(4)

        _btn_style = """
            QPushButton {
                color: #ddd; background: transparent;
                border: 1px solid #666; border-radius: 4px;
                padding: 1px 10px; font-size: 13px;
            }
            QPushButton:hover { background: #555; }
            QPushButton:checked { background: #2e6da4; color: white; border-color: #4a90d9; }
        """
        self.act_detect_phase = QtWidgets.QPushButton("検出フェーズ")
        self.act_detect_phase.setCheckable(True)
        self.act_detect_phase.setChecked(True)
        self.act_detect_phase.setStyleSheet(_btn_style)
        self.act_detect_phase.clicked.connect(lambda: self.switch_phase(1))
        phase_h.addWidget(self.act_detect_phase)

        self.act_track_phase = QtWidgets.QPushButton("追跡フェーズ")
        self.act_track_phase.setCheckable(True)
        self.act_track_phase.setChecked(False)
        self.act_track_phase.setStyleSheet(_btn_style)
        self.act_track_phase.clicked.connect(lambda: self.switch_phase(3))
        phase_h.addWidget(self.act_track_phase)

        phase_h.addStretch()
        self.main_vbox.addWidget(phase_bar)

        # ウィンドウサイズを使用中ディスプレイに応じて設定
        # （ノートPCの小さい画面では従来通り700x700付近を維持し、
        #  4K等の大画面では縦横に余裕を持たせてID一覧欄が圧迫されないようにする）
        self._apply_screen_aware_window_size()

        # 画面中央に配置
        self._center_on_screen()

        self.image_paths: List[Tuple[str, FrameNumber]] = []  # list of (path, frame_number)
        self.detections: Dict[FrameNumber, List[Box]] = {}
        self.loaded_frames: set = set()  # 読み込み済みフレームを記録
        self.pixmap_cache: Dict[str, QtGui.QPixmap] = {}  # 画像キャッシュ（再生高速化用）
        self.max_cache_size: int = 100  # 最大キャッシュサイズ
        self.current_frame_index: int = 0
        self.current_id: str = ""
        self.mode: str = "select"  # "select" | "edit"
        self.drag_start: Optional[Tuple[float, float]] = None
        self.drag_rect: Optional[QtWidgets.QGraphicsRectItem] = None # ★修正2: ドラッグ中の矩形アイテム
        self.min_new_box_w = 4.0
        self.min_new_box_h = 4.0
        self.require_w_for_add = False   # W無しで追加OKにするなら False
        self.add_with_w = False
        self._tab_held = False
        self._pending_drag = False
        self._press_pos_viewport = None
        self._pan_start: Optional[QtCore.QPoint] = None  # 右ドラッグによる視点移動の開始位置
        self._moving_box_index: Optional[int] = None          # 移動操作中のボックスインデックス
        self._moving_box_original: Optional[list] = None     # 移動前のボックスデータ [x,y,w,h,label]
        self._moving_press_scene: Optional[QtCore.QPointF] = None  # ドラッグ開始シーン座標
        self._moving_ghost: Optional[QtWidgets.QGraphicsRectItem] = None  # 移動プレビュー矩形
        self._is_dragging_box: bool = False                   # 実際にドラッグ中かどうか
        # --- hover highlight ---
        self.hover_item: Optional[QtWidgets.QGraphicsRectItem] = None
        self.hover_box_index: Optional[int] = None
        self.image_folder: str = ""
        self.last_txt_import_folder: str = ""  # 検出結果txtを読み込んだフォルダ（保存先のデフォルトに使用）
        self._initial_fit_done: bool = False
        self._min_zoom_scale: float = 0.0  # 初期フィット時のスケール（最小ズーム制限用）

        # 再生機能の追加
        self.is_playing: bool = False
        self.play_timer = QtCore.QTimer()
        self.play_timer.timeout.connect(self._play_next_frame)
        self.original_fps: float = 5.0  # 元動画のFPS（5fps）
        self.playback_speed: float = 2.0  # 再生速度倍率（デフォルト2倍速）
        self._bbox_items: list = []       # 再生中のBBoxアイテム管理リスト
        self._play_pixmap_item = None     # 再生中のPixmapアイテムキャッシュ
        
        # 進捗表示更新の遅延タイマー（操作中は更新を遅延）
        self.progress_update_timer = QtCore.QTimer()
        self.progress_update_timer.setSingleShot(True)
        self.progress_update_timer.timeout.connect(self.rebuild_id_list_ui)
        self.progress_update_delay = 500  # 500ms後に更新

        # Undo機能のための履歴管理
        self.undo_stack: List[Dict] = []  # 操作履歴スタック
        self.max_undo_history: int = 50  # 最大Undo回数

        # フェーズ管理
        self.current_phase: int = 1  # 1〜4
        self.label_check_mode: bool = False  # ラベルチェックモード ON/OFF
        self.phase4_active: bool = False      # 追跡ズームモード ON/OFF

        # ID
        self.id_list: List[str] = []
        self.id_intervals: Dict[str, List[Interval]] = {}
        self.hidden_ids: set = set()  # 非表示にするIDのセット

        self.id_color_map: Dict[str, int] = {}

        # カラーパレット（グローバル _HEX_PALETTE と統一）
        self.palette = [QtGui.QColor(c) for c in _HEX_PALETTE]

        # --- DPI & スケールユーティリティ ---
        scr = QtWidgets.QApplication.primaryScreen()
        self._dpi = scr.logicalDotsPerInch() if scr else 96.0
        self._dp_ratio = self._dpi / 96.0

        self._sidebar_ratio = 0.2  # サイドバーの幅比率（20%）
        self._splitter_user_moved = False  # ユーザーが手動でスプリッターを動かしたか

        self.build_ui()
        self.show()
        # ウィンドウが別モニターに移動したら、そのモニターの解像度で再判定する
        win_handle = self.windowHandle()
        if win_handle:
            win_handle.screenChanged.connect(self._on_screen_changed)
        QtCore.QTimer.singleShot(50, self._apply_splitter_ratio)
        # 起動時に自動で更新確認 → 更新を適用しなかった場合のみ画像読み込みを促す
        # （更新ダイアログと画像読み込みダイアログが同時に出ないよう、直列に実行する）
        QtCore.QTimer.singleShot(100, self._startup_update_check_then_prompt)

    def _apply_splitter_ratio(self):
        """スプリッターを現在のウィンドウ幅に基づいて比率で設定"""
        if not hasattr(self, 'content_splitter'):
            return
        total = self.content_splitter.width()
        if total <= 0:
            return
        sidebar_w = max(200, int(total * self._sidebar_ratio))
        self.content_splitter.setSizes([sidebar_w, total - sidebar_w])
        self.content_splitter.splitterMoved.connect(self._on_splitter_moved)

    def _on_splitter_moved(self, pos, index):
        """ユーザーがスプリッターを動かしたとき、現在の比率を記録"""
        total = self.content_splitter.width()
        if total > 0:
            sizes = self.content_splitter.sizes()
            self._sidebar_ratio = sizes[0] / total
            self._splitter_user_moved = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'content_splitter'):
            total = self.content_splitter.width()
            if total > 0:
                sidebar_w = max(200, int(total * self._sidebar_ratio))
                self.content_splitter.setSizes([sidebar_w, total - sidebar_w])

    def closeEvent(self, event):
        # 終了時の自動保存を削除
        # 再生タイマーを停止
        if self.is_playing:
            self.play_timer.stop()
        event.accept()



if __name__ == '__main__':
    # High DPI
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    # 5.14+ の場合はスケール丸めをPassThrough（可能なら）
    try:
        from PyQt5.QtGui import QGuiApplication
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QtWidgets.QApplication([])

    # OSのテーマに左右されず常にダーク基調で統一する（Windowsのデフォルトスタイルは
    # OSのダークモード設定を追随しないため、明示的にFusionスタイル+ダークパレットを適用する）
    app.setStyle("Fusion")
    dark_palette = QtGui.QPalette()
    dark_palette.setColor(QtGui.QPalette.Window, QtGui.QColor(45, 45, 45))
    dark_palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(220, 220, 220))
    dark_palette.setColor(QtGui.QPalette.Base, QtGui.QColor(30, 30, 30))
    dark_palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(45, 45, 45))
    dark_palette.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(45, 45, 45))
    dark_palette.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor(220, 220, 220))
    dark_palette.setColor(QtGui.QPalette.Text, QtGui.QColor(220, 220, 220))
    dark_palette.setColor(QtGui.QPalette.Button, QtGui.QColor(53, 53, 53))
    dark_palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(220, 220, 220))
    dark_palette.setColor(QtGui.QPalette.BrightText, QtGui.QColor(255, 80, 80))
    dark_palette.setColor(QtGui.QPalette.Link, QtGui.QColor(90, 160, 255))
    dark_palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(42, 130, 218))
    dark_palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
    dark_palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(127, 127, 127))
    dark_palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(127, 127, 127))
    dark_palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.WindowText, QtGui.QColor(127, 127, 127))
    app.setPalette(dark_palette)

    # ベースフォントはポイント指定（DPIに追随）
    base_font = QtGui.QFont("Noto Sans JP", 10)
    app.setFont(base_font)

    editor = DetectionEditor()
    app.exec_()