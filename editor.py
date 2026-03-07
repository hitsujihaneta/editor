import os
import glob
import json
import re
import hashlib
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtGui, QtCore

# フェーズ3用追加import
import copy
import time
from dataclasses import dataclass
from PyQt5.QtCore import QRectF, QLineF, QPointF, QTimer, pyqtSignal, Qt
from PyQt5.QtGui import QColor, QPainter, QPen, QFont, QPixmap, QGuiApplication
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
    QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton,
    QSizePolicy, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)
_PHASE3_AVAILABLE = True


FrameNumber = int
Box = List[object]  # [x, y, w, h, label(str)]
Interval = List[Optional[int]]  # [start_frame_number, end_frame_number_or_None]


# =====================================================================
# Phase3 クラス群（phase3_widget.py より統合）
# =====================================================================
@dataclass
class Span:
    start: int
    end: int
    kind: str = "track"


@dataclass
class Lane:
    label: str
    color: QColor
    spans: List[Span]
    id_value: str  # コードBのラベル文字列（"1","GK"等）


@dataclass
class Box:
    frame: int
    lane_index: int
    rect: Tuple[float, float, float, float]  # x1,y1,x2,y2


# =====================================================================
# カラーパレット（コードBのpalette順と合わせる）
# =====================================================================
_HEX_PALETTE = [
    "#006400", "#FF0000", "#800080", "#FFFF00", "#0000FF",
    "#D2691E", "#00FF00", "#654321", "#FF00FF", "#00FFFF",
    "#FFC0CB", "#A52A2A", "#DAA520", "#C0C0C0", "#000080",
    "#800000", "#008080",
]
_PALETTE = [QColor(c) for c in _HEX_PALETTE]


def _color_for_id(id_value: str, id_list: List[str]) -> QColor:
    """id_listの順番でカラーパレットを割り当て"""
    try:
        idx = id_list.index(id_value)
    except ValueError:
        idx = abs(hash(id_value)) 
    return _PALETTE[idx % len(_PALETTE)]


# =====================================================================
# GraphicsImageView（コードAから移植、最小変更）
# =====================================================================
class P3ImageView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setFrameShape(QFrame.NoFrame)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.MinimalViewportUpdate)

        self.images: List[str] = []
        self.boxes_by_frame: Dict[int, List[Box]] = {}
        self.lane_colors: Dict[int, QColor] = {}
        self.lane_labels: Dict[int, str] = {}
        self.current_frame = 0
        self._pixmap_item = None
        self._box_items: List[QGraphicsRectItem] = []
        self._label_items: List[QGraphicsSimpleTextItem] = []
        # 検出フェーズと統一した直接参照レンダリング用
        self._det_store: Optional[Dict] = None      # store.detections への参照
        self._orig_frames: List[int] = []           # internal_index → original_frame_number
        self._label_colors: Dict[str, QColor] = {}  # label文字列 → QColor
        self._right_dragging = False
        self._last_pan_pos = None
        self._initial_fit_done = False
        self._min_zoom_scale = 0.0
        self._playing = False  # 再生中フラグ（True時はラベル非表示）
        self.setMinimumSize(400, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.grabGesture(Qt.PinchGesture)

    def set_sources(self, images, boxes_by_frame, lane_colors, lane_labels=None,
                    det_store=None, orig_frames=None, label_colors=None):
        self.images = images
        self.boxes_by_frame = boxes_by_frame
        self.lane_colors = lane_colors
        self.lane_labels = lane_labels or {}
        # 直接参照レンダリング用（渡された場合はこちらを優先）
        self._det_store    = det_store
        self._orig_frames  = orig_frames or []
        self._label_colors = label_colors or {}
        self._initial_fit_done = False

    def set_frame(self, f: int):
        self.current_frame = f
        self._show_frame(f)

    def _show_frame(self, index: int):
        if not self.images or not (0 <= index < len(self.images)):
            return
        path = self.images[index]
        px = QPixmap(path)
        if px.isNull():
            return
        sc = self.scene()
        if self._pixmap_item is None:
            sc.clear()
            self._pixmap_item = sc.addPixmap(px)
            sc.setSceneRect(QRectF(px.rect()))
        else:
            self._pixmap_item.setPixmap(px)
        self._render_boxes()
        if not self._initial_fit_done:
            self.fitInView(sc.sceneRect(), Qt.KeepAspectRatio)
            self._min_zoom_scale = self.transform().m11()
            self._initial_fit_done = True

    def _render_boxes(self):
        sc = self.scene()
        for it in self._box_items + self._label_items:
            sc.removeItem(it)
        self._box_items.clear()
        self._label_items.clear()

        # ── 直接参照モード（store.detections + 元フレーム番号）────────────────
        # 検出フェーズと同じ座標系・フレーム対応を保証する
        if self._det_store is not None and self._orig_frames:
            if 0 <= self.current_frame < len(self._orig_frames):
                orig_frame = self._orig_frames[self.current_frame]
                raw_boxes = self._det_store.get(orig_frame, [])
                for item in raw_boxes:
                    if len(item) < 5:
                        continue
                    x, y, w, h, label = item[0], item[1], item[2], item[3], str(item[4])
                    color = self._label_colors.get(label, QColor("white"))
                    pen = QPen(color, 2)
                    ri = sc.addRect(float(x), float(y), float(w), float(h), pen)
                    self._box_items.append(ri)
                    self._draw_id_tag(sc, float(x), float(y), label, color)
            return

        # ── フォールバック（従来の boxes_by_frame 方式）──────────────────────
        boxes = self.boxes_by_frame.get(self.current_frame, [])
        for bx in boxes:
            color = self.lane_colors.get(bx.lane_index, QColor("white"))
            x1, y1, x2, y2 = bx.rect
            pen = QPen(color, 2)
            ri = sc.addRect(x1, y1, x2 - x1, y2 - y1, pen)
            self._box_items.append(ri)
            label = self.lane_labels.get(bx.lane_index, str(bx.lane_index))
            self._draw_id_tag(sc, x1, y1, str(label), color)

    def _draw_id_tag(self, sc, x: float, y: float, label: str, color: QColor):
        """ボックスの左上角に ID タグ（色付き背景＋白文字）を描画してリストに追加する。"""
        tag_font = QFont()
        tag_font.setPixelSize(12)
        fm = QtGui.QFontMetrics(tag_font)
        tw = fm.horizontalAdvance(label) + 6
        th = fm.height() + 4
        # 背景矩形（ボックス内側左上、IDカラー塗り）
        bg = sc.addRect(x, y, tw, th,
                        QPen(Qt.NoPen), QtGui.QBrush(color))
        # 文字色：背景が暗ければ白、明るければ黒
        fg = QColor(0, 0, 0) if color.lightness() > 160 else QColor(255, 255, 255)
        ti = sc.addSimpleText(label)
        ti.setFont(tag_font)
        ti.setBrush(QtGui.QBrush(fg))
        ti.setPos(x + 3, y + 2)
        self._label_items.append(bg)
        self._label_items.append(ti)

    def _zoom_at(self, view_pos, factor: float):
        old_pos = self.mapToScene(view_pos)
        self.scale(factor, factor)
        new_pos = self.mapToScene(view_pos)
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def wheelEvent(self, e):
        pixel = e.pixelDelta()
        if e.modifiers() & Qt.ShiftModifier:
            # Shift+スクロール：横スクロール
            hbar = self.horizontalScrollBar()
            step = pixel.y() if pixel.manhattanLength() > 0 else e.angleDelta().y()
            hbar.setValue(hbar.value() - step)
        elif pixel.manhattanLength() > 0 and not (e.modifiers() & Qt.ControlModifier):
            # トラックパッドの2本指スクロール → 視点移動（パン）
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            hbar.setValue(hbar.value() - pixel.x())
            vbar.setValue(vbar.value() - pixel.y())
        else:
            # マウスホイール or Ctrl+スクロール → ズーム
            angle_y = e.angleDelta().y()
            if angle_y != 0:
                factor = 1.15 if angle_y > 0 else 1.0 / 1.15
                self._zoom_at(e.pos(), factor)

    def event(self, e):
        if e.type() == e.Gesture:
            pinch = e.gesture(Qt.PinchGesture)
            if pinch:
                sf = pinch.scaleFactor()
                if sf and sf != 1.0:
                    center_global = pinch.centerPoint().toPoint()
                    view_pos = self.viewport().mapFromGlobal(center_global)
                    self._zoom_at(view_pos, sf)
                e.accept()
                return True
        # macOSトラックパッドのピンチズーム（NativeGesture）
        if e.type() == QtCore.QEvent.NativeGesture:
            if hasattr(e, 'gestureType') and e.gestureType() == Qt.ZoomNativeGesture:
                factor = 1.0 + e.value()
                factor = max(0.85, min(1.15, factor))
                if self._min_zoom_scale > 0:
                    current_scale = self.transform().m11()
                    if current_scale * factor < self._min_zoom_scale:
                        factor = self._min_zoom_scale / current_scale
                center_global = e.globalPos()
                view_pos = self.viewport().mapFromGlobal(center_global)
                self._zoom_at(view_pos, factor)
                e.accept()
                return True
        return super().event(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._right_dragging = True
            self._last_pan_pos = e.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            e.accept(); return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._right_dragging:
            if not (e.buttons() & Qt.RightButton):
                self._right_dragging = False
                self.viewport().setCursor(Qt.ArrowCursor)
                e.accept(); return
            delta = e.pos() - self._last_pan_pos
            self._last_pan_pos = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            e.accept(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.RightButton and self._right_dragging:
            self._right_dragging = False
            self.viewport().setCursor(Qt.ArrowCursor)
            e.accept(); return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self._pixmap_item:
            self.fitInView(self._pixmap_item.boundingRect(), Qt.KeepAspectRatio)
            self._min_zoom_scale = self.transform().m11()
        super().mouseDoubleClickEvent(e)


# =====================================================================
# ImprovedTimeline（コードAから移植）
# =====================================================================
class P3Timeline(QGraphicsView):
    visibilityChanged = pyqtSignal(object)
    scrubToFrame      = pyqtSignal(int)
    swapRequested     = pyqtSignal(int, int, int, int)

    def __init__(self, total_frames: int, fps: float, lanes: List[Lane], parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.total_frames = max(1, total_frames)
        self.fps = fps
        self.lanes: List[Lane] = lanes
        self.current_frame = 0

        self.row_height = 22
        self.margin_top = 30
        self.margin_left = 80
        self.visible_rows = 12
        self.max_visible_frames = 200
        self.pixels_per_frame = 8.0

        self._occlusions: set = set()
        self.cut_mode = False
        self.cut_to_end_mode = False
        self.sel1: Optional[tuple] = None
        self._sel_anchor_lane: Optional[int] = None
        self._sel_anchor_frame: Optional[int] = None
        self.hover_lane: Optional[int] = None
        self.dragging_sel = False

        self._check_proxies: List = []
        self._checks: List[QCheckBox] = []
        self._checkbox_states: List[bool] = []

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self._pin_left_column())
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QColor(40, 44, 48))

    def set_cut_mode(self, on: bool):
        self.cut_mode = on
        if not on:
            self.sel1 = None
            self._sel_anchor_lane = None
            self._sel_anchor_frame = None
        self.scene().update()

    def set_cut_to_end_mode(self, on: bool):
        self.cut_to_end_mode = on
        if not on:
            self.sel1 = None
            self._sel_anchor_lane = None
            self._sel_anchor_frame = None
        self.scene().update()

    def set_cut_selection(self, sel):
        self.sel1 = sel
        self.scene().update()

    def clear_cut_selection(self):
        self.sel1 = None
        self.dragging_sel = False
        self._sel_anchor_lane = None
        self._sel_anchor_frame = None
        self.scene().update()

    def _point_in_sel1(self, scene_pos) -> bool:
        """クリック位置が選択領域内かどうか判定"""
        if not self.sel1:
            return False
        lane, s, e_ = self.sel1
        y = self.margin_top + lane * self.row_height
        x1 = self.frame_to_x(s)
        x2 = self.frame_to_x(e_)
        r = QRectF(x1, y + 1, max(2.0, x2 - x1), self.row_height - 2)
        return r.contains(scene_pos)

    def set_occlusions(self, occ: set):
        self._occlusions = occ
        self.scene().update()

    def visible_frame_range(self) -> Tuple[int, int]:
        x_left  = self.horizontalScrollBar().value()
        x_right = x_left + self.viewport().width()
        start = max(0, int((x_left  - self.margin_left) / self.pixels_per_frame))
        end   = min(self.total_frames, int((x_right - self.margin_left) / self.pixels_per_frame) + 1)
        if end <= start:
            end = min(self.total_frames, start + self.max_visible_frames)
        return start, end

    def current_page_index(self) -> int:
        start, _ = self.visible_frame_range()
        return start // self.max_visible_frames

    def total_pages_virtual(self) -> int:
        return max(1, (self.total_frames + self.max_visible_frames - 1) // self.max_visible_frames)

    def update_model(self, total_frames: int, lanes: List[Lane]):
        self.total_frames = max(1, total_frames)
        self.lanes = lanes
        self._checkbox_states = []  # 読み込み直後は全オン
        self.rebuild()

    def update_playhead(self, frame: int):
        self.current_frame = max(0, min(self.total_frames - 1, frame))
        self._ensure_center_on_playhead()
        self.scene().update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.rebuild()

    def rebuild(self):
        view_width = max(800, self.viewport().width())
        timeline_width = max(200.0, view_width - self.margin_left - 20.0)
        frames_in_view = max(1, min(self.max_visible_frames, self.total_frames))
        self.pixels_per_frame = timeline_width / frames_in_view

        scene_height = self.margin_top + len(self.lanes) * self.row_height + 4
        total_width  = self.margin_left + self.total_frames * self.pixels_per_frame + 4
        self.setSceneRect(QRectF(0, 0, total_width, scene_height))

        visible = max(1, min(self.visible_rows, len(self.lanes)))
        view_height = self.margin_top + visible * self.row_height + 4
        self.setMaximumHeight(int(view_height))
        self.setMinimumHeight(120)

        self._rebuild_checkboxes()
        self._pin_left_column()
        self.scene().update()

    def _rebuild_checkboxes(self):
        self._checkbox_states = [cb.isChecked() for cb in self._checks]
        for p in self._check_proxies:
            self.scene().removeItem(p)
        self._check_proxies.clear()
        self._checks.clear()

        for i, _lane in enumerate(self.lanes):
            cb = QCheckBox()
            cb.setChecked(self._checkbox_states[i] if i < len(self._checkbox_states) else True)
            cb.setStyleSheet("""
                QCheckBox { color: #ddd; }
                QCheckBox::indicator { width:16px; height:16px; border-radius:3px; border:1px solid #666; background:#2b2f33; }
                QCheckBox::indicator:checked { background-color:#2ECC71; border:1px solid #1E8E3E; }
            """)
            proxy = self.scene().addWidget(cb)
            proxy.setZValue(1_000_000)

            def on_toggled(_checked, idx=i):
                checked_set = {j for j, w in enumerate(self._checks) if w.isChecked()}
                self.visibilityChanged.emit(checked_set)
                self.scene().update()

            cb.toggled.connect(on_toggled)
            self._checks.append(cb)
            self._check_proxies.append(proxy)

    def _pin_left_column(self):
        top_left = self.mapToScene(0, 0)
        for i, proxy in enumerate(self._check_proxies):
            y = self.margin_top + i * self.row_height + (self.row_height - 18) // 2
            proxy.setPos(top_left.x() + 2, y)

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._pin_left_column()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ShiftModifier:
            # Shift+スクロール → 横スクロール（フレーム移動）
            hbar = self.horizontalScrollBar()
            hbar.setValue(hbar.value() - e.angleDelta().y())
        else:
            # 通常スクロール → 縦スクロール（行移動）
            vbar = self.verticalScrollBar()
            delta = e.angleDelta().y()
            vbar.setValue(vbar.value() - delta)

    def shift_window_by_frames(self, delta_frames: int):
        start, _ = self.visible_frame_range()
        self._scroll_to_frame_left(start + delta_frames)

    def _scroll_to_frame_left(self, start_frame: int):
        x_left = self.margin_left + start_frame * self.pixels_per_frame
        self.horizontalScrollBar().setValue(int(x_left))

    def _ensure_center_on_playhead(self):
        if self.total_frames <= self.max_visible_frames:
            self.horizontalScrollBar().setValue(0)
            self._pin_left_column()
            return
        hbar = self.horizontalScrollBar()
        viewport_w = self.viewport().width()
        content_w  = self.sceneRect().width()
        f = self.current_frame
        if f < 100:
            target_left = 0
        elif f > self.total_frames - 100:
            target_left = max(0, content_w - viewport_w)
        else:
            center_x = self.margin_left + f * self.pixels_per_frame
            target_left = int(center_x - viewport_w / 2)
        target_left = max(0, min(int(target_left), hbar.maximum()))
        if abs(hbar.value() - target_left) > 1:
            hbar.setValue(target_left)
            self._pin_left_column()

    def frame_to_x(self, f: int) -> float:
        return self.margin_left + f * self.pixels_per_frame

    def x_to_frame(self, x: float) -> int:
        f = int((x - self.margin_left) / self.pixels_per_frame)
        return max(0, min(self.total_frames - 1, f))

    def y_to_lane(self, y: float) -> Optional[int]:
        row = int((y - self.margin_top) / self.row_height)
        return row if 0 <= row < len(self.lanes) else None

    def drawBackground(self, painter: QPainter, rect: QRectF):
        painter.fillRect(self.sceneRect(), QColor(40, 44, 48))
        start_frame, end_frame = self.visible_frame_range()
        for i, lane in enumerate(self.lanes):
            y  = self.margin_top + i * self.row_height
            bg = QColor(50, 54, 60) if i % 2 == 0 else QColor(44, 48, 54)
            painter.fillRect(QRectF(self.margin_left, y, self.sceneRect().width() - self.margin_left, self.row_height), bg)
            painter.setPen(QPen(QColor(60, 65, 72), 0.5))
            painter.drawLine(QLineF(self.margin_left, y + self.row_height, self.sceneRect().width(), y + self.row_height))
            # ID ラベル（左固定は_pin_left_columnのcheckboxが担当、ここはテキストのみ）
            painter.setPen(QColor(200, 200, 200))
            painter.setFont(QFont("Arial", 9))
            painter.drawText(QRectF(20, y, self.margin_left - 22, self.row_height),
                             Qt.AlignVCenter | Qt.AlignRight, f"ID {lane.id_value}")
            # スパン描画（常にlane.colorで描画）
            for sp in lane.spans:
                sf = max(sp.start, start_frame)
                ef = min(sp.end,   end_frame)
                if sf >= ef: continue
                x1 = self.frame_to_x(sf)
                x2 = self.frame_to_x(ef)
                w  = max(3.0, x2 - x1)
                r  = QRectF(x1, y + 2, w, self.row_height - 4)
                painter.fillRect(r, lane.color)
                painter.setPen(QPen(lane.color.darker(130), 1))
                painter.drawRect(r)

            # オクルージョン: 黒ボーダー矩形をスパン上に重ねて表示
            if self._occlusions:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(0, 0, 0), 1))
                for f in range(start_frame, end_frame):
                    if (i, f) in self._occlusions:
                        x = self.frame_to_x(f)
                        w_cell = max(3.0, self.pixels_per_frame)
                        painter.drawRect(QRectF(x + 0.5, y + 2.5, w_cell - 1.0, self.row_height - 5.0))

        # 目盛り
        painter.setPen(QColor(160, 160, 160))
        painter.setFont(QFont("Arial", 8))
        tick_interval = max(10, int(50 / max(self.pixels_per_frame, 0.1) / 10) * 10)
        for f in range(start_frame, end_frame, tick_interval):
            x = self.frame_to_x(f)
            painter.drawLine(QLineF(x, 0, x, self.margin_top - 4))
            painter.drawText(QRectF(x + 2, 0, 60, self.margin_top), Qt.AlignVCenter, str(f))

        # プレイヘッド
        px = self.frame_to_x(self.current_frame)
        painter.setPen(QPen(QColor(255, 60, 60), 2))
        painter.drawLine(QLineF(px, 0, px, self.sceneRect().height()))

        # 選択領域
        if self.sel1:
            src_lane, s, e_ = self.sel1
            x1 = self.frame_to_x(s)
            x2 = self.frame_to_x(e_)
            y  = self.margin_top + src_lane * self.row_height
            sel_color = QColor(255, 200, 0, 80)
            painter.fillRect(QRectF(x1, y, x2 - x1, self.row_height), sel_color)
            painter.setPen(QPen(QColor(255, 200, 0), 1))
            painter.drawRect(QRectF(x1, y, x2 - x1, self.row_height))

            # ドラッグ中：ドロップ先レーンをハイライト
            if self.dragging_sel and self.hover_lane is not None and self.hover_lane != src_lane:
                dst_y = self.margin_top + self.hover_lane * self.row_height
                # ドロップ先の同フレーム範囲を水色で塗る
                drop_color = QColor(0, 200, 255, 60)
                painter.fillRect(QRectF(x1, dst_y, x2 - x1, self.row_height), drop_color)
                painter.setPen(QPen(QColor(0, 200, 255), 2, Qt.DashLine))
                painter.drawRect(QRectF(x1, dst_y, x2 - x1, self.row_height))
                # 矢印ライン（元レーン → ドロップ先レーン の中央を結ぶ）
                src_cx = (x1 + x2) / 2
                src_cy = y + self.row_height / 2
                dst_cy = dst_y + self.row_height / 2
                painter.setPen(QPen(QColor(0, 220, 255), 1, Qt.DotLine))
                painter.drawLine(QLineF(src_cx, src_cy, src_cx, dst_cy))

        # アンカー表示（1回目クリック済み、2回目待ち）
        if self.cut_mode and self._sel_anchor_lane is not None and self._sel_anchor_frame is not None and not self.sel1:
            ay = self.margin_top + self._sel_anchor_lane * self.row_height
            ax = self.frame_to_x(self._sel_anchor_frame)
            painter.setPen(QPen(QColor(255, 200, 0), 2, Qt.DashLine))
            painter.drawLine(QLineF(ax, ay, ax, ay + self.row_height))

    def mousePressEvent(self, e):
        if e.pos().x() < self.margin_left:
            return super().mousePressEvent(e)

        scene_pos = self.mapToScene(e.pos())
        x, y = scene_pos.x(), scene_pos.y()
        frame = self.x_to_frame(x)
        lane  = self.y_to_lane(y)

        if e.button() == Qt.LeftButton:
            if self.cut_to_end_mode and lane is not None:
                # 既存の選択領域内をクリック → ドラッグ開始（スワップ準備）
                if self.sel1 and self._point_in_sel1(scene_pos):
                    self.dragging_sel = True
                    self.hover_lane = lane
                    self.scene().update()
                    e.accept(); return
                # それ以外 → 現フレームから末尾を選択範囲に設定
                self.set_cut_selection((lane, frame, self.total_frames))
                self._sel_anchor_lane = lane
                self._sel_anchor_frame = frame
                self.scene().update()
                e.accept(); return

            if self.cut_mode and lane is not None:
                # 既存の選択領域内をクリック → ドラッグ開始（スワップ準備）
                if self.sel1 and self._point_in_sel1(scene_pos):
                    self.dragging_sel = True
                    self.hover_lane = lane
                    self.scene().update()
                    e.accept(); return
                # アンカー未設定 → アンカーを設定（1回目のクリック）
                if self._sel_anchor_lane is None:
                    self._sel_anchor_lane = lane
                    self._sel_anchor_frame = frame
                    self.scene().update()
                    e.accept(); return
                # アンカーあり・同じレーン → 選択範囲を確定（2回目のクリック）
                if self._sel_anchor_lane == lane:
                    s  = min(self._sel_anchor_frame, frame)
                    e_ = max(self._sel_anchor_frame, frame) + 1
                    self.set_cut_selection((lane, s, e_))
                    self._sel_anchor_lane = None
                    self._sel_anchor_frame = None
                    e.accept(); return
                # アンカーあり・別のレーン → アンカーをリセット
                self._sel_anchor_lane = lane
                self._sel_anchor_frame = frame
                self.scene().update()
                e.accept(); return

            self.scrubToFrame.emit(frame)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        scene_pos = self.mapToScene(e.pos())
        self.hover_lane = self.y_to_lane(scene_pos.y())

        if self.dragging_sel:
            # ドラッグ中はホバーレーンの更新のみ（選択範囲は変更しない）
            self.scene().update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.dragging_sel and self.sel1:
            src_lane, s, e_ = self.sel1
            dst_lane = self.hover_lane
            if dst_lane is not None and dst_lane != src_lane:
                # Yes(左・灰) / Cancel(右・青)
                _dlg = QMessageBox(self)
                _dlg.setWindowTitle("入れ替え確認")
                src_id = self.lanes[src_lane].id_value if src_lane < len(self.lanes) else str(src_lane + 1)
                dst_id = self.lanes[dst_lane].id_value if dst_lane < len(self.lanes) else str(dst_lane + 1)
                _dlg.setText(f"区間 {s}〜{e_-1} を ID{src_id} ⇔ ID{dst_id} で入れ替えますか？")
                _btn_yes    = _dlg.addButton("Yes",    QMessageBox.AcceptRole)
                _btn_cancel = _dlg.addButton("Cancel", QMessageBox.RejectRole)
                _btn_yes.setStyleSheet(
                    "QPushButton{background-color:#6c6c6c;color:white;border-radius:5px;padding:4px 16px;}"
                    "QPushButton:hover{background-color:#555;}"
                )
                _btn_cancel.setStyleSheet(
                    "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 16px;}"
                    "QPushButton:hover{background-color:#0062d4;}"
                )
                _dlg.setDefaultButton(_btn_cancel)
                _dlg.exec_()
                if _dlg.clickedButton() == _btn_yes:
                    self.swapRequested.emit(src_lane, dst_lane, s, e_)
                else:
                    self.dragging_sel = False
                    self.hover_lane = None
                    self.scene().update()
                    return
            self.dragging_sel = False
            self.hover_lane = None
            self.scene().update()
            return
        super().mouseReleaseEvent(e)


# =====================================================================
# P3ControlPanel（タブ形式コントロールパネル）
# =====================================================================
_P3_SPEED_FACTORS = {"0.25x": 0.25, "0.5x": 0.5, "1x": 1.0, "2x": 2.0, "3x": 3.0, "4x": 4.0}

class P3ControlPanel(QWidget):
    """Phase3用コントロールパネル（再生系左・編集系右の一列レイアウト）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setStyleSheet("""
            QPushButton {
                background-color: white;
                color: black;
                border: 1px solid #5a5f66;
                border-radius: 6px;
                padding: 2px 6px;
            }
            QPushButton:hover { background-color: #f2f2f2; }
            QPushButton:pressed { background-color: #e6e6e6; }
        """)

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)

        def btn(text, w=None):
            b = QPushButton(text)
            if w:
                b.setFixedWidth(w)
            b.setFixedHeight(26)
            return b

        # ── 再生系（左） ────────────────────────────────────────
        self.status = QLabel("0 / 0f")
        self.status.setFixedWidth(100)
        row.addWidget(self.status)

        self.jumpEdit = QLineEdit()
        self.jumpEdit.setFixedWidth(55)
        self.jumpEdit.setFixedHeight(26)
        self.jumpEdit.setPlaceholderText("frame")
        self.jumpBtn = QPushButton("Go")
        self.jumpBtn.setFixedWidth(32)
        self.jumpBtn.setFixedHeight(26)
        row.addWidget(self.jumpEdit)
        row.addWidget(self.jumpBtn)

        self.btnBigPrev = btn("«« 10", 58)
        self.btnPrev    = btn("« 1",   46)
        self.btnPlay    = btn("▶",     46)
        self.btnNext    = btn("1 »",   46)
        self.btnBigNext = btn("10 »",  58)

        for b in [self.btnBigPrev, self.btnPrev, self.btnPlay,
                  self.btnNext,    self.btnBigNext]:
            row.addWidget(b)

        row.addSpacing(8)
        row.addWidget(QLabel("速度:"))
        self.speedCombo = QComboBox()
        self.speedCombo.addItems(list(_P3_SPEED_FACTORS.keys()))
        self.speedCombo.setCurrentText("3x")
        self.speedCombo.setFixedWidth(66)
        row.addWidget(self.speedCombo)

        row.addSpacing(8)
        self.chkIdTrack = QCheckBox("ID追跡")
        row.addWidget(self.chkIdTrack)
        self.idTrackCombo = QComboBox()
        self.idTrackCombo.setFixedWidth(72)
        self.idTrackCombo.setVisible(False)
        row.addWidget(self.idTrackCombo)

        self._zoom_label = QLabel("ズーム:")
        self._zoom_label.setVisible(False)
        row.addWidget(self._zoom_label)
        self.zoomSpin = QDoubleSpinBox()
        self.zoomSpin.setRange(1.0, 20.0)
        self.zoomSpin.setSingleStep(0.5)
        self.zoomSpin.setValue(6.0)
        self.zoomSpin.setSuffix("x")
        self.zoomSpin.setFixedWidth(68)
        self.zoomSpin.setFixedHeight(26)
        self.zoomSpin.setVisible(False)
        row.addWidget(self.zoomSpin)

        row.addStretch()

        # ── 編集系（右） ────────────────────────────────────────
        self.btnOcc      = btn("Occlusion OFF", 118)
        self.btnCut      = btn("✂ Cut",          76)
        self.btnCutToEnd = btn("✂ Cut(after all)", 118)
        self.progress    = QProgressBar()
        self.progress.setFixedWidth(90)
        self.progress.setVisible(False)

        for b in [self.btnOcc, self.btnCut, self.btnCutToEnd]:
            row.addWidget(b)
        row.addWidget(self.progress)


# =====================================================================
# 共有データストア（検出フェーズ・追跡フェーズ共通）
# =====================================================================
class EditorStore:
    """
    両フェーズが直接参照するシングルトン的なデータストア。
    DetectionEditor がオーナーとして生成し、Phase3Widget は参照を受け取る。
    """
    def __init__(self):
        self.detections: Dict[int, list] = {}
        self.id_list: List[str] = [str(i) for i in range(1, 12)]
        self.image_paths: List[Tuple] = []
        self.image_folder: str = ""
        self.shared_frame: int = 0  # フェーズ間で共有するフレーム番号（原フレーム番号）


# =====================================================================
# Phase3Widget（メインクラス）
# =====================================================================
class Phase3Widget(QWidget):
    """
    追跡フェーズウィジェット。
    EditorStore を直接参照し、DetectionEditor との間でデータを一元管理する。
    """

    def __init__(self, store: 'EditorStore', parent=None):
        super().__init__(parent)

        # 共有ストア（DetectionEditor と同一オブジェクトを参照）
        self.store = store

        # ---- 内部データ（Phase3 独自の表現）----
        self.lanes: List[Lane] = []
        self.boxes_by_frame: Dict[int, List[Box]] = {}
        self._boxes_by_frame_all: Dict[int, List[Box]] = {}
        self._lanes_all: List[Lane] = []
        self._allowed_lane_indices: Optional[set] = None

        self.total_frames = 1
        self.current_frame = 0
        self.fps = 5.0
        self.images: List[str] = []
        self.frame_offset = 0
        self.original_frame_numbers: List[int] = []

        self.show_occlusion = False
        self.cut_mode = False
        self.cut_to_end_mode = False
        self.auto_occluded: set = set()
        self._undo_stack: list = []
        self._id_tracking: bool = False

        # ---- UI構築 ----
        self._build_ui()
        self._connect_events()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # UI構築
    # ------------------------------------------------------------------
    def _build_ui(self):
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        self.mainView = P3ImageView(self)
        self.ctrl     = P3ControlPanel(self)
        self.timeline = P3Timeline(self.total_frames, self.fps, self.lanes, parent=self)

        self.timeline.visibilityChanged.connect(self._on_visibility_changed)
        self.timeline.scrubToFrame.connect(self._on_scrub)
        self.timeline.swapRequested.connect(self._on_swap)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        bl.addWidget(self.ctrl)
        bl.addWidget(self.timeline)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.mainView)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setStyleSheet("""
            QSplitter::handle { background-color:#0062d4; height:6px; }
            QSplitter::handle:hover { background-color:#888; }
        """)

        vbox.addWidget(splitter)

    def _connect_events(self):
        self.ctrl.btnPrev.clicked.connect(lambda: self._step(-1))
        self.ctrl.btnNext.clicked.connect(lambda: self._step(+1))
        self.ctrl.btnBigPrev.clicked.connect(lambda: self._step(-10))
        self.ctrl.btnBigNext.clicked.connect(lambda: self._step(+10))
        self.ctrl.btnPlay.clicked.connect(self._toggle_play)
        self.ctrl.btnOcc.clicked.connect(self._toggle_occlusion)
        self.ctrl.btnCut.clicked.connect(self._toggle_cut)
        self.ctrl.btnCutToEnd.clicked.connect(self._toggle_cut_to_end)
        self.ctrl.speedCombo.currentTextChanged.connect(self._on_speed_changed)
        self.ctrl.chkIdTrack.toggled.connect(self._on_id_track_toggled)
        self.ctrl.jumpBtn.clicked.connect(self._on_jump)
        self.ctrl.jumpEdit.returnPressed.connect(self._on_jump)
        self.ctrl.idTrackCombo.currentIndexChanged.connect(
            lambda _: self._center_on_tracked_id() if self._id_tracking else None
        )
        self.ctrl.zoomSpin.valueChanged.connect(
            lambda _: self._center_on_tracked_id() if self._id_tracking else None
        )

    # ------------------------------------------------------------------
    # 外部インタフェース（コードBとの橋渡し）
    # ------------------------------------------------------------------
    def load_from_editor(self):
        """
        EditorStore から最新データを読み込み、Phase3 内部形式に変換して表示する。
        フェーズ切り替え時に呼ばれる。データのコピーは行わず store を直接参照する。
        """
        detections  = self.store.detections
        id_list     = self.store.id_list
        image_paths = self.store.image_paths

        # 画像リスト（frame_numberでソート済みを前提）
        sorted_paths = sorted(image_paths, key=lambda t: t[1])
        self.images  = [p for p, _ in sorted_paths]
        self.original_frame_numbers = [n for _, n in sorted_paths]
        self.frame_offset = min(self.original_frame_numbers) if self.original_frame_numbers else 0
        self.total_frames = len(self.images)

        # id_list からレーンを構築（id_valueが文字列のまま）
        self._build_lanes_from_id_list(id_list)

        # detections を Box に変換
        self._convert_detections_to_boxes(detections, id_list)

        # 表示更新
        self._refresh_views()
        self._clear_undo()

    def _sync_to_store(self):
        """
        Phase3 の編集結果（_boxes_by_frame_all）を EditorStore.detections に書き戻す。
        編集操作・フェーズ離脱時に呼ばれ、常にストアを最新状態に保つ。
        """
        lane_to_id = {i: ln.id_value for i, ln in enumerate(self.lanes)}
        detections = self.store.detections
        detections.clear()
        for fno_internal, boxes in self._boxes_by_frame_all.items():
            frame_number = fno_internal + self.frame_offset
            boxes_out = []
            for bx in boxes:
                x1, y1, x2, y2 = bx.rect
                x = x1; y = y1
                w = x2 - x1; h = y2 - y1
                label = lane_to_id.get(bx.lane_index, str(bx.lane_index))
                boxes_out.append([x, y, w, h, label])
            if boxes_out:
                detections[frame_number] = boxes_out

    # ------------------------------------------------------------------
    # 内部変換
    # ------------------------------------------------------------------
    def _build_lanes_from_id_list(self, id_list: List[str]):
        """id_listからLaneリストを構築"""
        self.lanes = []
        for i, id_value in enumerate(id_list):
            color = _PALETTE[i % len(_PALETTE)]
            self.lanes.append(Lane(
                label=f"ID {id_value}",
                color=color,
                spans=[],
                id_value=id_value,
            ))
        self._lanes_all = list(self.lanes)

    def _convert_detections_to_boxes(self,
                                      detections: Dict[int, List],
                                      id_list: List[str]):
        """コードBのdetections → Box辞書に変換"""
        id_to_lane = {id_value: i for i, id_value in enumerate(id_list)}
        by_frame: Dict[int, List[Box]] = {}

        for frame_number, boxes in detections.items():
            fno_internal = frame_number - self.frame_offset
            internal_boxes = []
            for item in boxes:
                if len(item) < 5:
                    continue
                x, y, w, h, label = item[0], item[1], item[2], item[3], str(item[4])
                lane_idx = id_to_lane.get(label)
                if lane_idx is None:
                    # 未知IDはid_listに追加してレーンも追加
                    lane_idx = len(self.lanes)
                    id_to_lane[label] = lane_idx
                    color = _PALETTE[lane_idx % len(_PALETTE)]
                    self.lanes.append(Lane(f"ID {label}", color, [], label))
                    self._lanes_all = list(self.lanes)
                x1 = float(x); y1 = float(y)
                x2 = x1 + float(w); y2 = y1 + float(h)
                internal_boxes.append(Box(fno_internal, lane_idx, (x1, y1, x2, y2)))
            if internal_boxes:
                by_frame[fno_internal] = internal_boxes

        self._boxes_by_frame_all = by_frame
        self.boxes_by_frame = dict(by_frame)
        self._allowed_lane_indices = None

        # スパンを再構築
        self._rebuild_lanes_from_boxes()
        self.auto_occluded = self._compute_occlusions()

    def _rebuild_lanes_from_boxes(self):
        for ln in self.lanes:
            ln.spans = []
        presence: Dict[int, List[int]] = {i: [] for i in range(len(self.lanes))}
        for f, boxes in self._boxes_by_frame_all.items():
            for b in boxes:
                if 0 <= b.lane_index < len(self.lanes):
                    presence[b.lane_index].append(f)
        for lane_idx, frames in presence.items():
            if not frames: continue
            frames = sorted(set(frames))
            s = frames[0]; prev = frames[0]
            for f in frames[1:]:
                if f == prev + 1:
                    prev = f
                else:
                    self.lanes[lane_idx].spans.append(Span(s, prev + 1))
                    s = prev = f
            self.lanes[lane_idx].spans.append(Span(s, prev + 1))

    # ------------------------------------------------------------------
    # 表示更新
    # ------------------------------------------------------------------
    def _refresh_views(self):
        lane_colors  = {i: ln.color    for i, ln in enumerate(self._lanes_all)}
        lane_labels  = {i: ln.id_value for i, ln in enumerate(self._lanes_all)}
        label_colors = {ln.id_value: ln.color for ln in self._lanes_all}
        self.mainView.set_sources(
            self.images, self.boxes_by_frame, lane_colors, lane_labels,
            det_store=self.store.detections,
            orig_frames=self.original_frame_numbers,
            label_colors=label_colors,
        )
        self.timeline.update_model(self.total_frames, self.lanes)
        self.timeline.set_occlusions(self.auto_occluded if self.show_occlusion else set())
        self._populate_id_track_combo()
        self._seek(min(self.current_frame, self.total_frames - 1))
        self._update_status()
        self._apply_button_styles()

    def _seek(self, frame: int):
        self.current_frame = max(0, min(self.total_frames - 1, frame))
        self.timeline.update_playhead(self.current_frame)
        self.mainView.set_frame(self.current_frame)
        if self._id_tracking:
            self._center_on_tracked_id()
        self._update_status()

    def _center_on_tracked_id(self):
        """ID追跡: 選択されたIDのボックスにズーム＋センタリングを適用"""
        lane_idx = self.ctrl.idTrackCombo.currentData()
        if lane_idx is None:
            return
        if lane_idx >= len(self._lanes_all):
            return
        id_value = self._lanes_all[lane_idx].id_value

        # 現在フレームの原フレーム番号から store.detections を直接参照
        if not (0 <= self.current_frame < len(self.original_frame_numbers)):
            return
        orig_frame = self.original_frame_numbers[self.current_frame]
        raw_boxes = self.store.detections.get(orig_frame, [])

        target_box = None
        for item in raw_boxes:
            if len(item) >= 5 and str(item[4]) == id_value:
                target_box = item
                break

        if target_box is None:
            return

        x = float(target_box[0])
        y = float(target_box[1])
        w = float(target_box[2])
        h = float(target_box[3])
        cx = x + w / 2.0
        cy = y + h / 2.0

        # ズーム倍率を適用（初期フィットスケール × 指定倍率）
        zoom_factor = self.ctrl.zoomSpin.value()
        min_scale = self.mainView._min_zoom_scale
        target_scale = min_scale * zoom_factor if min_scale > 0 else zoom_factor

        current_scale = self.mainView.transform().m11()
        if abs(current_scale - target_scale) > 0.01:
            self.mainView.resetTransform()
            self.mainView.scale(target_scale, target_scale)

        self.mainView.centerOn(cx, cy)

    def _populate_id_track_combo(self):
        """ID追跡コンボボックスにIDリストを設定"""
        combo = self.ctrl.idTrackCombo
        prev_data = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for i, lane in enumerate(self._lanes_all):
            combo.addItem(f"ID {lane.id_value}", i)
        # 以前の選択を復元
        for j in range(combo.count()):
            if combo.itemData(j) == prev_data:
                combo.setCurrentIndex(j)
                break
        combo.blockSignals(False)

    def _step(self, delta: int):
        self._seek(self.current_frame + delta)

    def _on_jump(self):
        text = self.ctrl.jumpEdit.text().strip()
        if not text.isdigit():
            return
        frame_1based = int(text)
        frame_0based = max(0, min(frame_1based - 1, self.total_frames - 1))
        self._seek(frame_0based)
        self.ctrl.jumpEdit.clear()
        self.ctrl.jumpEdit.setFocus()

    def _update_status(self):
        self.ctrl.status.setText(
            f"{self.current_frame + 1} / {self.total_frames}f"
        )

    # ------------------------------------------------------------------
    # 再生
    # ------------------------------------------------------------------
    def _get_timer_interval(self) -> int:
        factor = _P3_SPEED_FACTORS.get(self.ctrl.speedCombo.currentText(), 1.0)
        return max(1, int(1000 / self.fps / factor))

    def _toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.ctrl.btnPlay.setText("▶")
            self.mainView._playing = False
            # 停止後にラベルを再描画
            self.mainView._render_boxes()
            self.timeline.update_playhead(self.current_frame)
        else:
            self.mainView._playing = True
            self.timer.start(self._get_timer_interval())
            self.ctrl.btnPlay.setText("❚❚")

    def _on_speed_changed(self, _text: str):
        if self.timer.isActive():
            self.timer.setInterval(self._get_timer_interval())

    def _on_id_track_toggled(self, checked: bool):
        self._id_tracking = checked
        self.ctrl.idTrackCombo.setVisible(checked)
        self.ctrl._zoom_label.setVisible(checked)
        self.ctrl.zoomSpin.setVisible(checked)
        if not checked:
            # 追跡終了時はビューをフィット状態に戻す
            if self.mainView._initial_fit_done and self.mainView._min_zoom_scale > 0:
                self.mainView.resetTransform()
                self.mainView.scale(self.mainView._min_zoom_scale, self.mainView._min_zoom_scale)
                self.mainView.horizontalScrollBar().setValue(0)
                self.mainView.verticalScrollBar().setValue(0)

    def _on_tick(self):
        if self.current_frame < self.total_frames - 1:
            self.current_frame += 1
            self.mainView.set_frame(self.current_frame)
            if self.current_frame % 10 == 0:
                self.timeline.update_playhead(self.current_frame)
            if self.current_frame % 5 == 0:
                self._update_status()
            if self._id_tracking:
                self._center_on_tracked_id()
        else:
            self._toggle_play()

    # ------------------------------------------------------------------
    # タイムラインイベント
    # ------------------------------------------------------------------
    def _on_scrub(self, f: int):
        self._seek(f)

    def _on_visibility_changed(self, checked_set):
        if len(checked_set) == len(self._lanes_all):
            self._allowed_lane_indices = None
            filtered = dict(self._boxes_by_frame_all)
        else:
            self._allowed_lane_indices = set(int(i) for i in checked_set)
            filtered = {}
            for f, boxes in self._boxes_by_frame_all.items():
                kept = [b for b in boxes if b.lane_index in self._allowed_lane_indices]
                if kept:
                    filtered[f] = kept
        self.boxes_by_frame = filtered
        self.mainView.boxes_by_frame = filtered
        self.mainView._render_boxes()

    def _on_swap(self, src_lane: int, dst_lane: int, s: int, e_: int):
        self._push_undo("swap")
        self._swap_ranges(src_lane, dst_lane, s, e_)
        self._rebuild_lanes_from_boxes()
        # _render_boxes が store.detections を参照するため、描画前にストアへ反映する
        self._sync_to_store()
        self._on_visibility_changed(
            {j for j, cb in enumerate(self.timeline._checks) if cb.isChecked()}
        )
        self.timeline.update_model(self.total_frames, self.lanes)
        self.timeline.clear_cut_selection()
        self._seek(self.current_frame)

    def _swap_ranges(self, laneA: int, laneB: int, s: int, e_: int):
        for fA in range(s, e_):
            boxesA = self._boxes_by_frame_all.get(fA, [])
            boxesB = self._boxes_by_frame_all.get(fA, [])  # 同一フレーム
            idxA = [i for i, bx in enumerate(boxesA) if bx.lane_index == laneA]
            idxB = [i for i, bx in enumerate(boxesB) if bx.lane_index == laneB]
            for i in idxA:
                bx = boxesA[i]; boxesA[i] = Box(bx.frame, laneB, bx.rect)
            for j in idxB:
                bx = boxesB[j]; boxesB[j] = Box(bx.frame, laneA, bx.rect)
            if idxA or idxB:
                self._boxes_by_frame_all[fA] = boxesA

    # ------------------------------------------------------------------
    # Occlusion / Cut
    # ------------------------------------------------------------------
    def _compute_occlusions(self, threshold=0.10) -> set:
        result = set()
        def area(b):
            x1,y1,x2,y2 = b.rect; return max(0,(x2-x1))*max(0,(y2-y1))
        def inter(a, b):
            ax1,ay1,ax2,ay2=a.rect; bx1,by1,bx2,by2=b.rect
            return max(0,min(ax2,bx2)-max(ax1,bx1))*max(0,min(ay2,by2)-max(ay1,by1))
        for f, boxes in self._boxes_by_frame_all.items():
            n=len(boxes)
            if n<2: continue
            areas=[area(b) for b in boxes]
            for i in range(n):
                if areas[i]<=0: continue
                for j in range(i+1,n):
                    if areas[j]<=0: continue
                    it=inter(boxes[i],boxes[j])
                    if it>0 and it/min(areas[i],areas[j])>=threshold:
                        result.add((boxes[i].lane_index,f))
                        result.add((boxes[j].lane_index,f))
        return result

    def _toggle_occlusion(self):
        self._push_undo("toggle_occlusion")
        self.show_occlusion = not self.show_occlusion
        self.ctrl.btnOcc.setText("Occlusion  ON" if self.show_occlusion else "Occlusion OFF")
        self.timeline.set_occlusions(self.auto_occluded if self.show_occlusion else set())
        self._apply_button_styles()

    def _toggle_cut(self):
        self._push_undo("toggle_cut")
        self.cut_mode = not self.cut_mode
        if self.cut_mode and self.cut_to_end_mode:
            self.cut_to_end_mode = False
            self.timeline.set_cut_to_end_mode(False)
        self.timeline.set_cut_mode(self.cut_mode)
        if not self.cut_mode:
            self.timeline.clear_cut_selection()
        self._apply_button_styles()

    def _toggle_cut_to_end(self):
        self._push_undo("toggle_cut_to_end")
        self.cut_to_end_mode = not self.cut_to_end_mode
        if self.cut_to_end_mode and self.cut_mode:
            self.cut_mode = False
            self.timeline.set_cut_mode(False)
        self.timeline.set_cut_to_end_mode(self.cut_to_end_mode)
        if not self.cut_to_end_mode:
            self.timeline.clear_cut_selection()
        self._apply_button_styles()

    def _apply_button_styles(self):
        on  = "QPushButton{background-color:#2E86C1;color:white;border:1px solid #1b5e85;border-radius:6px;padding:2px 6px;}"
        occ = "QPushButton{background-color:#808080;color:white;border:1px solid #555;border-radius:6px;padding:2px 6px;}"
        self.ctrl.btnCut.setStyleSheet(on if self.cut_mode else "")
        self.ctrl.btnCutToEnd.setStyleSheet(on if self.cut_to_end_mode else "")
        self.ctrl.btnOcc.setStyleSheet(occ if self.show_occlusion else "")

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------
    def _push_undo(self, label=""):
        self._undo_stack.append({
            "boxes_by_frame_all": copy.deepcopy(self._boxes_by_frame_all),
            "lanes": copy.deepcopy(self.lanes),
            "current_frame": self.current_frame,
            "show_occlusion": self.show_occlusion,
            "cut_mode": self.cut_mode,
            "cut_to_end_mode": self.cut_to_end_mode,
        })

    def undo_last(self):
        if not self._undo_stack:
            return
        snap = self._undo_stack.pop()
        self._boxes_by_frame_all = snap["boxes_by_frame_all"]
        self.lanes = snap["lanes"]
        self.show_occlusion  = snap["show_occlusion"]
        self.cut_mode        = snap["cut_mode"]
        self.cut_to_end_mode = snap["cut_to_end_mode"]
        self._rebuild_lanes_from_boxes()
        self._on_visibility_changed(
            {j for j, cb in enumerate(self.timeline._checks) if cb.isChecked()}
        )
        self._refresh_views()
        self._seek(snap["current_frame"])
        # ストアに反映
        self._sync_to_store()

    def _clear_undo(self):
        self._undo_stack.clear()

    # ------------------------------------------------------------------
    # フェーズ切り替え時の停止
    # ------------------------------------------------------------------
    def on_phase_deactivate(self):
        """フェーズ3から離れるとき呼ぶ（再生停止・ストアに同期）"""
        if self.timer.isActive():
            self._toggle_play()
        self._sync_to_store()

    def on_phase_activate(self):
        """フェーズ3に切り替わるとき呼ぶ（ストアから最新データを再ロード）"""
        self.load_from_editor()


class IntervalEditor(QtWidgets.QDialog):
    """簡易な出場区間(Intervals)編集ダイアログ。"""

    def __init__(self, parent: QtWidgets.QWidget, id_str: str, frames: List[int], intervals: List[Interval]):
        super().__init__(parent)
        self.setWindowTitle(f"区間編集 - {id_str}")
        self.resize(480, 320)
        self.frames = frames
        self.id_str = id_str
        self._data: List[Interval] = [[iv[0], iv[1]] for iv in intervals]

        layout = QtWidgets.QVBoxLayout(self)

        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["開始(フレーム番号)", "終了(フレーム番号/空で未定)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        btns = QtWidgets.QHBoxLayout()
        self.del_btn = QtWidgets.QPushButton("－ 行削除")
        self.clear_start_btn = QtWidgets.QPushButton("開始を空に")
        self.clear_end_btn = QtWidgets.QPushButton("終了を空に")
        btns.addWidget(self.del_btn)
        btns.addWidget(self.clear_start_btn)
        btns.addWidget(self.clear_end_btn)
        btns.addStretch(1)
        layout.addLayout(btns)

        self.del_btn.clicked.connect(self.del_row)
        self.clear_start_btn.clicked.connect(self.clear_start_cells)
        self.clear_end_btn.clicked.connect(self.clear_end_cells)
        self.add_with_w: bool = False  

        ab = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        layout.addWidget(ab)
        ab.accepted.connect(self.accept)
        ab.rejected.connect(self.reject)

        for s, e in self._data:
            self._append_row(s, e)

    def _append_row(self, start: Optional[int] = None, end: Optional[int] = None):
        r = self.table.rowCount()
        self.table.insertRow(r)
        s_item = QtWidgets.QTableWidgetItem("" if start is None else str(start))
        e_item = QtWidgets.QTableWidgetItem("" if end is None else str(end))
        self.table.setItem(r, 0, s_item)
        self.table.setItem(r, 1, e_item)

    def add_row(self):
        self._append_row()

    def del_row(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _selected_rows(self) -> List[int]:
        return sorted({idx.row() for idx in self.table.selectedIndexes()})

    def clear_start_cells(self):
        for r in self._selected_rows():
            item = self.table.item(r, 0)
            if item is None:
                item = QtWidgets.QTableWidgetItem("")
                self.table.setItem(r, 0, item)
            item.setText("")

    def clear_end_cells(self):
        for r in self._selected_rows():
            item = self.table.item(r, 1)
            if item is None:
                item = QtWidgets.QTableWidgetItem("")
                self.table.setItem(r, 1, item)
            item.setText("")

    def get_intervals(self) -> Optional[List[Interval]]:
        intervals: List[Interval] = []
        for r in range(self.table.rowCount()):
            s_item = self.table.item(r, 0)
            e_item = self.table.item(r, 1)
            s_txt = (s_item.text().strip() if s_item else "")
            e_txt = (e_item.text().strip() if e_item else "")
            if not s_txt:
                continue
            try:
                s_val = int(s_txt)
            except ValueError:
                QtWidgets.QMessageBox.warning(self, "入力エラー", f"開始フレームは整数で入力してください (row {r+1}).")
                return None
            if s_val not in self.frames:
                QtWidgets.QMessageBox.warning(self, "入力エラー", f"開始フレーム {s_val} は画像に存在しません。")
                return None
            if e_txt:
                try:
                    e_val = int(e_txt)
                except ValueError:
                    QtWidgets.QMessageBox.warning(self, "入力エラー", f"終了フレームは整数で入力してください (row {r+1}).")
                    return None
                if e_val not in self.frames:
                    QtWidgets.QMessageBox.warning(self, "入力エラー", f"終了フレーム {e_val} は画像に存在しません。")
                    return None
                if e_val < s_val:
                    QtWidgets.QMessageBox.warning(self, "入力エラー", f"終了フレームは開始以上である必要があります (row {r+1}).")
                    return None
                intervals.append([s_val, e_val])
            else:
                intervals.append([s_val, None])

        intervals.sort(key=lambda x: (x[0], x[1] if x[1] is not None else 10**12))
        merged: List[Interval] = []
        for s, e in intervals:
            if not merged:
                merged.append([s, e]); continue
            ps, pe = merged[-1]
            pe2 = pe if pe is not None else 10**12
            e2 = e if e is not None else 10**12
            if s <= pe2:
                new_end: Optional[int]
                if pe is None or e is None:
                    new_end = None
                else:
                    new_end = max(pe, e)
                merged[-1] = [ps, new_end]
            else:
                merged.append([s, e])
        return merged

    def accept(self) -> None:
        checked = self.get_intervals()
        if checked is None:
            return
        self._data = checked
        super().accept()

    @property
    def result_intervals(self) -> List[Interval]:
        return self._data


# =====================================================================
# FPSSettingsDialog
# =====================================================================
class FPSSettingsDialog(QtWidgets.QDialog):
    """FPS（再生速度）を調整するダイアログ"""
    def __init__(self, parent, current_fps: float):
        super().__init__(parent)
        self.setWindowTitle("FPS設定")
        self.setFixedSize(360, 160)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        desc = QtWidgets.QLabel("再生速度（FPS）を調整:")
        desc.setStyleSheet("font-size: 11pt;")
        layout.addWidget(desc)

        slider_row = QtWidgets.QHBoxLayout()
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(5)
        self.slider.setMaximum(60)
        self.slider.setValue(int(current_fps))
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.slider.setTickInterval(5)
        self.slider.valueChanged.connect(self._on_slider_changed)

        self.value_label = QtWidgets.QLabel(f"{int(current_fps)} FPS")
        self.value_label.setStyleSheet("font-size: 11pt; font-weight: bold; min-width: 70px;")
        self.value_label.setAlignment(QtCore.Qt.AlignCenter)

        slider_row.addWidget(self.slider)
        slider_row.addWidget(self.value_label)
        layout.addLayout(slider_row)

        hint = QtWidgets.QLabel("推奨: 低スペックPC=15, 標準=24, 高スペックPC=30")
        hint.setStyleSheet("font-size: 9pt; color: #666;")
        layout.addWidget(hint)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QtWidgets.QPushButton("OK")
        ok_btn.setFixedWidth(80)
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _on_slider_changed(self, value: int):
        self.value_label.setText(f"{value} FPS")

    def get_fps(self) -> float:
        return float(self.slider.value())


# =====================================================================
# LayoutAdjusterDialog
# =====================================================================
class LayoutAdjusterDialog(QtWidgets.QDialog):
    """サイドバーと画像ビューの幅比率を調整するダイアログ"""
    def __init__(self, parent, splitter: QtWidgets.QSplitter):
        super().__init__(parent)
        self.splitter = splitter
        self.setWindowTitle("レイアウト調整")
        self.setFixedSize(400, 160)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        desc = QtWidgets.QLabel("サイドバーと画像ビューの幅比率を調整:")
        desc.setStyleSheet("font-size: 11pt;")
        layout.addWidget(desc)

        label_row = QtWidgets.QHBoxLayout()
        lbl_left = QtWidgets.QLabel("サイドバー 大")
        lbl_left.setStyleSheet("font-weight: bold; color: #2196F3;")
        lbl_center = QtWidgets.QLabel("バランス")
        lbl_center.setAlignment(QtCore.Qt.AlignCenter)
        lbl_right = QtWidgets.QLabel("画像ビュー 大")
        lbl_right.setStyleSheet("font-weight: bold; color: #4CAF50;")
        label_row.addWidget(lbl_left)
        label_row.addStretch()
        label_row.addWidget(lbl_center)
        label_row.addStretch()
        label_row.addWidget(lbl_right)
        layout.addLayout(label_row)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(10)
        self.slider.setMaximum(60)
        self.slider.setValue(30)
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.slider.setTickInterval(10)
        self.slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.slider)

        self.value_label = QtWidgets.QLabel("サイドバー: 30%  |  画像ビュー: 70%")
        self.value_label.setAlignment(QtCore.Qt.AlignCenter)
        self.value_label.setStyleSheet("font-size: 10pt; color: #666;")
        layout.addWidget(self.value_label)

        QtCore.QTimer.singleShot(100, self._init_from_splitter)

    def _init_from_splitter(self):
        sizes = self.splitter.sizes()
        if len(sizes) >= 2 and sum(sizes) > 0:
            total = sum(sizes)
            pct = int((sizes[0] / total) * 100)
            pct = max(10, min(60, pct))
            self.slider.setValue(pct)

    def _on_slider_changed(self, value: int):
        self.value_label.setText(f"サイドバー: {value}%  |  画像ビュー: {100 - value}%")
        total = self.splitter.width()
        if total > 0:
            self.splitter.setSizes([int(total * value / 100), int(total * (100 - value) / 100)])


class DetectionEditor(QtWidgets.QWidget):
    # ------------------------------------------------------------------
    # EditorStore へのプロパティ委譲
    # （両フェーズが同一ストアを参照するため、インスタンス属性ではなくプロパティで管理）
    # ------------------------------------------------------------------
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

        # ウィンドウサイズを700x700に設定
        self.resize(700, 700)
        
        # 画面中央に配置
        self._center_on_screen()

        self.image_paths: List[Tuple[str, FrameNumber]] = []  # list of (path, frame_number)
        self.detections: Dict[FrameNumber, List[Box]] = {}
        self.loaded_frames: set = set()  # 読み込み済みフレームを記録
        self.pixmap_cache: Dict[str, QtGui.QPixmap] = {}  # 画像キャッシュ（再生高速化用）
        self.max_cache_size: int = 100  # 最大キャッシュサイズ
        self.current_frame_index: int = 0
        self.current_id: str = "1"
        self.mode: str = "select"  # "select" | "edit"
        self.drag_start: Optional[Tuple[float, float]] = None
        self.drag_rect: Optional[QtWidgets.QGraphicsRectItem] = None # ★修正2: ドラッグ中の矩形アイテム
        self.min_new_box_w = 4.0
        self.min_new_box_h = 4.0
        self.require_w_for_add = False   # W無しで追加OKにするなら False
        self.add_with_w = False
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
        self._initial_fit_done: bool = False
        self._min_zoom_scale: float = 0.0  # 初期フィット時のスケール（最小ズーム制限用）

        # 再生機能の追加
        self.is_playing: bool = False
        self.play_timer = QtCore.QTimer()
        self.play_timer.timeout.connect(self._play_next_frame)
        self.original_fps: float = 5.0  # 元動画のFPS（5fps）
        self.playback_speed: float = 3.0  # 再生速度倍率（デフォルト3倍速）
        
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
        self.id_list: List[str] = [str(i) for i in range(1, 12)]
        self.id_intervals: Dict[str, List[Interval]] = {}

        self.id_color_map: Dict[str, int] = {}

        # カラーパレット（グローバル _HEX_PALETTE と統一）
        self.palette = [QtGui.QColor(c) for c in _HEX_PALETTE]

        # --- DPI & スケールユーティリティ ---
        scr = QtWidgets.QApplication.primaryScreen()
        self._dpi = scr.logicalDotsPerInch() if scr else 96.0
        self._dp_ratio = self._dpi / 96.0

        self.build_ui()
        self.show()
        QtCore.QTimer.singleShot(200, self._prompt_initial_image_load)

    def _setup_menu_bar(self):
        """メニューバーを構築し、メインレイアウトに追加する"""
        self.menu_bar = QtWidgets.QMenuBar()

        # --- File メニュー ---
        file_menu = self.menu_bar.addMenu("File")

        # 開く
        act_load_img = file_menu.addAction("📂 画像フォルダを選択")
        act_load_det = file_menu.addMenu("📄 検出結果を読み込み")
        file_menu.addSeparator()

        # 保存
        save_menu = file_menu.addMenu("💾 保存")
        act_save_json        = save_menu.addAction("JSON形式で保存 [フレームごと]")
        save_menu.addSeparator()
        act_save_txt         = save_menu.addAction("TXT形式で保存 (Ctrl+Shift+S) [一括ファイル]")
        act_save_txt_per_frame = save_menu.addAction("TXT形式で保存 [フレームごと]")
        save_menu.addSeparator()
        act_save_csv         = save_menu.addAction("CSVで保存 (frame,id,x1,y1,x2,y2)...")
        act_save_labelme     = save_menu.addAction("LabelMe JSONフォルダへ保存...")
        save_menu.addSeparator()
        act_export_all       = save_menu.addAction("📦 一括エクスポート (CSV + TXT + JSON)...")

        # 読み込みサブメニュー
        act_load_det_file   = act_load_det.addAction("📄 TXTファイル（全フレーム一括）")
        act_load_det_folder = act_load_det.addAction("📁 TXTフォルダ（フレームごと）")
        act_load_det.addSeparator()
        act_load_csv        = act_load_det.addAction("📊 CSVファイル (frame,id,x1,y1,x2,y2)")
        act_load_json_folder = act_load_det.addAction("🗂 LabelMe JSONフォルダ")

        # 接続
        act_load_img.triggered.connect(self.load_images)
        act_load_det_file.triggered.connect(lambda: self.load_detections_from_txt(is_folder=False))
        act_load_det_folder.triggered.connect(lambda: self.load_detections_from_txt(is_folder=True))
        act_load_csv.triggered.connect(self.open_csv)
        act_load_json_folder.triggered.connect(self.open_labelme_json_folder)
        act_save_json.triggered.connect(self.save_all_json)
        act_save_txt.triggered.connect(self.save_all_txt)
        act_save_txt_per_frame.triggered.connect(self.save_all_txt_per_frame)
        act_save_csv.triggered.connect(self.save_as_csv)
        act_save_labelme.triggered.connect(self.save_as_labelme_json)
        act_export_all.triggered.connect(self.export_all_formats)

        # --- Undo メニュー ---
        undo_menu = self.menu_bar.addMenu("Undo")
        self.act_undo = undo_menu.addAction("⟲ Undo (Ctrl+Z)")
        self.act_undo.setShortcut("Ctrl+Z")
        self.act_undo.triggered.connect(self.undo_last_operation)

    def _key_press_for_mode_q(self, checked):
        """Modeメニューからの呼び出し用"""
        self.mode = "edit" if self.mode == "select" else "select"
        self.update_mode_label()


    def _center_on_screen(self):
        """ウィンドウを画面の中央に配置"""
        screen = QtWidgets.QApplication.primaryScreen()
        if screen:
            screen_geometry = screen.geometry()
            x = (screen_geometry.width() - self.width()) // 2
            y = (screen_geometry.height() - self.height()) // 2
            self.move(x, y)

    def _play_next_frame(self):
        """再生時に次のフレームに進む"""
        if self.current_frame_index < len(self.image_paths) - 1:
            self.current_frame_index += 1
            self._load_image_fast()  # 再生中は高速な描画を使用
        else:
            # 最後のフレームに到達したら停止
            self.toggle_play_pause()
    
    def _load_image_fast(self):
        """再生時の高速画像読み込み（進捗表示更新なし、キャッシュ使用、テキストなし）"""
        if not self.image_paths:
            return
        
        img_path, frame_number = self.image_paths[self.current_frame_index]
        self._load_frame_if_needed(frame_number)

        # キャッシュから画像を取得、なければ読み込んでキャッシュに保存
        if img_path in self.pixmap_cache:
            pixmap = self.pixmap_cache[img_path]
        else:
            pixmap = QtGui.QPixmap(img_path)
            if pixmap.isNull():
                return
            # キャッシュサイズ制限
            if len(self.pixmap_cache) >= self.max_cache_size:
                # 古いキャッシュを削除（最初の要素）
                first_key = next(iter(self.pixmap_cache))
                del self.pixmap_cache[first_key]
            self.pixmap_cache[img_path] = pixmap

        # scene.clear()は重いので、BBoxとテキストのみ削除
        for item in list(self.scene.items()):
            if isinstance(item, (QtWidgets.QGraphicsRectItem, QtWidgets.QGraphicsTextItem)):
                self.scene.removeItem(item)
        
        self.hover_item = None
        self.hover_box_index = None
        self.drag_rect = None
        
        # 画像アイテムが存在しない場合のみ追加
        pixmap_items = [item for item in self.scene.items() if isinstance(item, QtWidgets.QGraphicsPixmapItem)]
        if pixmap_items:
            # 既存の画像を更新
            pixmap_items[0].setPixmap(pixmap)
        else:
            # 新規追加
            self.scene.addPixmap(pixmap)
            self.scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())

        # 既存の検出ボックスを描画（テキストなし - 高速化のため）
        frame_boxes = self.detections.get(frame_number, [])
        
        # フィルター適用（全ID表示がOFFの場合）
        if hasattr(self, 'show_all_checkbox') and not self.show_all_checkbox.isChecked():
            if hasattr(self, 'filter_combo') and self.filter_combo.count() > 0:
                filter_id = self.filter_combo.currentText()
                frame_boxes = [box for box in frame_boxes if box[4] == filter_id]
        
        for i, (x, y, w, h, label) in enumerate(frame_boxes):
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            box_item.setData(0, i)
            # テキストラベルの描画をスキップ（再生高速化のため）

        # フレーム情報を更新（軽量）
        if hasattr(self, 'frame_index_label'):
            self.frame_index_label.setText(
                f"フレーム: {self.current_frame_index + 1} / {len(self.image_paths)} (元画像番号: {frame_number})"
            )
        
        # 進捗表示の更新はスキップ（rebuild_id_list_uiを呼ばない）

        # 追跡ズーム ON のときズームを適用
        if getattr(self, 'phase4_active', False):
            self._phase4_apply_zoom()

    def toggle_play_pause(self):
        """再生/停止を切り替え"""
        if not self.image_paths:
            return
            
        self.is_playing = not self.is_playing
        
        if self.is_playing:
            self.play_pause_btn.setText("⏸ 停止")
            # 再生速度 = 元のFPS × 倍速
            target_fps = self.original_fps * self.playback_speed
            interval = int(1000 / target_fps)  # ミリ秒単位のインターバル
            print(f"[再生開始] 倍速={self.playback_speed}x, 目標FPS={target_fps}, インターバル={interval}ms")
            self.play_timer.start(interval)
        else:
            self.play_pause_btn.setText("▶ 再生")
            self.play_timer.stop()
            print(f"[再生停止]")
    
    def _on_speed_changed(self, text: str):
        """再生速度選択が変更されたときの処理"""
        try:
            # "2x" → 2.0 に変換
            speed_str = text.replace('x', '')
            new_speed = float(speed_str)
            self.playback_speed = new_speed
            
            # デバッグ出力
            target_fps = self.original_fps * self.playback_speed
            interval = int(1000 / target_fps)
            print(f"[速度変更] {text} → 倍速={new_speed}, 目標FPS={target_fps}, インターバル={interval}ms")
            
            # 再生中の場合はタイマーを再起動
            if self.is_playing:
                self.play_timer.stop()
                self.play_timer.start(interval)
                print(f"[タイマー再起動] 再生中のため、タイマーを{interval}msで再起動しました")
            else:
                print(f"[待機中] 再生停止中のため、タイマーは再起動しません")
        except ValueError as e:
            print(f"[エラー] 速度変更エラー: {e}")
            pass
    
    def _on_show_all_changed(self, state):
        """全ID表示チェックボックスの状態変更"""
        if state == QtCore.Qt.Checked:
            # 全ID表示ON → フィルターコンボを無効化
            self.filter_combo.setEnabled(False)
        else:
            # 全ID表示OFF → フィルターコンボを有効化
            self.filter_combo.setEnabled(True)

    # -------- DPI helpers --------
    def dp(self, x: float) -> int:
        """96dpi基準のpxをDPIに合わせてスケール"""
        return int(round(x * self._dp_ratio))

    def em(self, w_chars: float = 1.0) -> int:
        """現在フォントの高さ基準の相対サイズ"""
        fm = self.fontMetrics()
        return int(round(fm.height() * w_chars))

    # -------- utils --------
    def color_for(self, id_str: str) -> QtGui.QColor:
        s = str(id_str)
        if s == "-1":
            return QtGui.QColor(255,255,255)
        # 必要に応じて割当表を整備
        self._ensure_color_map()
        idx = self.id_color_map.get(s, 0)
        return self.palette[idx]

    def frame_numbers(self) -> List[int]:
        return [fn for _, fn in self.image_paths]

    def meta_path(self) -> str:
        return os.path.join(self.image_folder, "_editor_meta.json") if self.image_folder else "_editor_meta.json"

    # -------- UI --------
    def build_ui(self):
        # メニューバーは __init__ で main_vbox に追加済み
        
        # ここからはメニューバーの下に配置するコンテンツ部分
        # 左側サイドバーコンテナ (スタックで初期/操作画面を切り替える)
        sidebar_container = QtWidgets.QWidget()
        sidebar_container.setMinimumWidth(self.dp(200))
        sidebar_container.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)

        self.sidebar_stack = QtWidgets.QStackedLayout(sidebar_container)
        self.sidebar_stack.setContentsMargins(0, 0, 0, 0)

        # --- 0: 起動時（初期）UI ---
        self.initial_ui = self._build_initial_ui()
        self.sidebar_stack.addWidget(self.initial_ui)

        # --- 1: 画像読み込み後（操作）UI ---
        self.loaded_ui = self._build_loaded_ui()
        self.sidebar_stack.addWidget(self.loaded_ui)

        # 起動時は初期UIを表示
        self.sidebar_stack.setCurrentIndex(0)

        # 右 (QGraphicsView)
        self.scene = QtWidgets.QGraphicsScene()
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setMouseTracking(True)
        self.view.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.view.setResizeAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        self.view.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.TextAntialiasing
            | QtGui.QPainter.SmoothPixmapTransform
        )

        # 左右をSplitterで繋ぐ（幅を自由に調整可能）
        self.content_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.content_splitter.addWidget(sidebar_container)
        self.content_splitter.addWidget(self.view)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setContentsMargins(self.dp(8), self.dp(8), self.dp(8), self.dp(8))
        self.content_splitter.setStyleSheet(
            "QSplitter::handle {"
            "  background-color: #0062d4;"
            "  width: 8px;"
            "  border-left: 2px solid #004aaa;"
            "  border-right: 2px solid #004aaa;"
            "}"
            "QSplitter::handle:hover { background-color: #3399ff; }"
            "QSplitter::handle:pressed { background-color: #ff9900; }"
        )

        # Phase3 全画面ウィジェット（サイドバーの外に置く）
        if _PHASE3_AVAILABLE:
            self.phase3_widget = Phase3Widget(store=self.store, parent=None)
        else:
            self.phase3_widget = QtWidgets.QLabel("Phase3 利用不可")
            self.phase3_widget.setAlignment(QtCore.Qt.AlignCenter)

        # Phase3 全画面コンテナ
        phase3_container = QtWidgets.QWidget()
        phase3_v = QtWidgets.QVBoxLayout(phase3_container)
        phase3_v.setContentsMargins(0, 0, 0, 0)
        phase3_v.setSpacing(0)
        phase3_v.addWidget(self.phase3_widget, 1)

        # メインコンテンツスタック：Phase1/2/4(splitter) vs Phase3(全画面)
        self.main_content_stack = QtWidgets.QStackedWidget()
        self.main_content_stack.addWidget(self.content_splitter)  # index 0
        self.main_content_stack.addWidget(phase3_container)        # index 1
        self.main_vbox.addWidget(self.main_content_stack)

        self.view.viewport().installEventFilter(self)
        self.view.installEventFilter(self)  # NativeGesture(ピンチ)はview本体に届く
        self.view.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocus()  # 起動時にメインウィンドウにフォーカス

        # Phase3 表示中のキーイベントをアプリレベルで横取りする
        QtWidgets.QApplication.instance().installEventFilter(self)

    def _build_initial_ui(self) -> QtWidgets.QWidget:
        """起動時（画像未読み込み時）のシンプルなファイル選択UIを作成"""
        initial_widget = QtWidgets.QWidget()
        v_layout = QtWidgets.QVBoxLayout(initial_widget)
        v_layout.setContentsMargins(self.dp(8), self.dp(8), self.dp(8), self.dp(8))
        v_layout.setSpacing(self.dp(8))
        
        filebox = QtWidgets.QGroupBox("ファイル選択")
        filelayout = QtWidgets.QVBoxLayout()
        filelayout.setSpacing(self.dp(6))
        filelayout.setContentsMargins(self.dp(8), self.dp(8), self.dp(8), self.dp(8))

        # 画像フォルダを選択ボタンのみ
        self.load_image_btn = QtWidgets.QPushButton("📂 画像フォルダを選択")
        self.load_image_btn.setMinimumHeight(self.em(2.0))
        self.load_image_btn.clicked.connect(self.load_images)
        filelayout.addWidget(self.load_image_btn)
        
        filebox.setLayout(filelayout)
        v_layout.addWidget(filebox)
        v_layout.addStretch(1) 
        
        return initial_widget
    
    def _build_loaded_ui(self) -> QtWidgets.QWidget:
        """画像読み込み後（操作用）のUIを作成"""
        loaded_widget = QtWidgets.QWidget()
        outer_v = QtWidgets.QVBoxLayout(loaded_widget)
        outer_v.setContentsMargins(0, 0, 0, 0)
        outer_v.setSpacing(0)

        # タブウィジェット（Phase1・Phase2を切り替え）
        self.phase_tab = QtWidgets.QTabWidget()
        outer_v.addWidget(self.phase_tab)

        # ===== タブ0: Phase1（従来サイドバー） =====
        phase1_widget = QtWidgets.QWidget()
        self.sidebar = QtWidgets.QVBoxLayout(phase1_widget)
        self.sidebar.setSpacing(self.dp(3))
        self.sidebar.setContentsMargins(self.dp(8), self.dp(6), self.dp(8), self.dp(6))
        self.phase_tab.addTab(phase1_widget, "Phase 1")

        # Phase2 UI は widgets 初期化のみ（タブには追加しない。ラベルチェックパネルで使用）
        # 戻り値を保持しないと子 widgets が GC で削除されるため self に保存
        self._phase2_hidden_widget = self._build_phase2_ui()

        # Phase4 はタブに追加しない（Phase1 サイドバーに内包）
        # Phase3 もタブに追加しない（メニューバーアクションで切り替え）

        # タブは Phase1 のみなのでタブバーを非表示
        self.phase_tab.tabBar().setVisible(False)
        self.phase_tab.currentChanged.connect(self._on_phase_tab_changed)

        # ★ 以下は従来通り self.sidebar に追加
        self.frame_index_label = QtWidgets.QLabel("フレーム: 0 / 0")

        # ★★★ 修正/追加箇所: ここでフォントサイズを変更します ★★★
        # 例: 現在のフォントサイズより2ポイント大きくする
        font = self.frame_index_label.font()
        font.setPointSize(font.pointSize() + 2) # この数値を変更することで大きさを調整
        font.setBold(True) # 必要に応じて太字設定も可能
        self.frame_index_label.setFont(font)
        self.frame_index_label.setWordWrap(True)
        # ★★★ ここまで ★★★
        self.sidebar.addWidget(self.frame_index_label)

        # --- 2. フレームジャンプ機能 ---
        jump_layout = QtWidgets.QHBoxLayout()
        
        self.jump_frame_input = QtWidgets.QLineEdit()
        self.jump_frame_input.setPlaceholderText("フレーム番号")
        self.jump_frame_input.setMinimumHeight(self.em(1.8))
        self.jump_frame_input.returnPressed.connect(self.jump_to_frame)
        jump_layout.addWidget(self.jump_frame_input, 1)
        
        self.jump_btn = QtWidgets.QPushButton("➤ ジャンプ")
        self.jump_btn.setMinimumHeight(self.em(1.6))
        self.jump_btn.clicked.connect(self.jump_to_frame)
        jump_layout.addWidget(self.jump_btn)
        
        self.sidebar.addLayout(jump_layout)

        # --- 3. 再生ボタンとFPS設定 ---
        playback_layout = QtWidgets.QHBoxLayout()
        
        self.play_pause_btn = QtWidgets.QPushButton("▶ 再生")
        self.play_pause_btn.setMinimumHeight(self.em(1.6))
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        playback_layout.addWidget(self.play_pause_btn)
        
        # 再生速度選択コンボボックス
        speed_label = QtWidgets.QLabel("速度:")
        playback_layout.addWidget(speed_label)
        
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItems(["0.5x", "1x", "2x", "3x", "5x"])
        self.speed_combo.setCurrentText("3x")  # デフォルト3倍速
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        self.speed_combo.setMinimumWidth(self.dp(70))
        playback_layout.addWidget(self.speed_combo)
        
        # --- 3.5 追跡ズームトグル（インライン） ---
        filter_layout = QtWidgets.QHBoxLayout()

        _sf = QtGui.QFont(self.font())
        _sf.setPointSize(int(self.font().pointSize() * 1.5))

        self.phase4_toggle = QtWidgets.QCheckBox("ID追跡")
        self.phase4_toggle.setFont(_sf)
        self.phase4_toggle.toggled.connect(self._on_phase4_toggled)
        filter_layout.addWidget(self.phase4_toggle)

        self._phase4_zoom_label = QtWidgets.QLabel(". 倍率:")
        self._phase4_zoom_label.setFont(_sf)
        self._phase4_zoom_label.setVisible(False)
        filter_layout.addWidget(self._phase4_zoom_label)

        self.phase4_zoom_spin = QtWidgets.QDoubleSpinBox()
        self.phase4_zoom_spin.setFont(_sf)
        self.phase4_zoom_spin.setRange(1.0, 20.0)
        self.phase4_zoom_spin.setValue(6.0)
        self.phase4_zoom_spin.setSuffix("x")
        self.phase4_zoom_spin.setSingleStep(0.5)
        self.phase4_zoom_spin.setFixedWidth(self.dp(90))
        self.phase4_zoom_spin.setVisible(False)
        self.phase4_zoom_spin.valueChanged.connect(self._phase4_apply_zoom)
        filter_layout.addWidget(self.phase4_zoom_spin)

        filter_layout.addStretch()

        # 再生ボタン＋追跡ズームをひとつのサブレイアウトにまとめる（隙間を独立制御）
        play_track_group = QtWidgets.QVBoxLayout()
        play_track_group.setContentsMargins(0, 0, 0, 0)
        play_track_group.setSpacing(self.dp(2))
        play_track_group.addLayout(playback_layout)
        play_track_group.addLayout(filter_layout)
        self.sidebar.addLayout(play_track_group)
        self.sidebar.addSpacing(self.dp(12))


        # --- 4. モード情報 ---
        mode_row = QtWidgets.QHBoxLayout()
        self.mode_label = QtWidgets.QLabel()
        mode_row.addWidget(self.mode_label)
        self.label_check_indicator = QtWidgets.QLabel()
        self.label_check_indicator.setFixedSize(self.dp(28), self.dp(28))
        self.label_check_indicator.setVisible(False)
        mode_row.addWidget(self.label_check_indicator)
        mode_row.addStretch()
        self.sidebar.addLayout(mode_row)
        self.guide_label = QtWidgets.QLabel()
        self.guide_label.setFont(_sf)
        self.sidebar.addWidget(self.guide_label)

        self.sidebar.addSpacing(self.dp(4))

        # Phase4 widgets（GC防止用。表示はしない）
        self._phase4_internal_widget = self._build_phase4_ui()

        self.sidebar.addSpacing(self.dp(4))

        # --- 5. 現在のID ---
        # 画像数、JSON数、取込数は参照用に非表示で保持
        self.image_count_label = QtWidgets.QLabel("画像数: 0")
        self.image_count_label.setVisible(False)
        self.sidebar.addWidget(self.image_count_label)
        
        self.ann_count_label = QtWidgets.QLabel("JSONファイル数: 0 件")
        self.ann_count_label.setVisible(False)
        self.sidebar.addWidget(self.ann_count_label)
        
        self.det_src_count_label = QtWidgets.QLabel("検出(テキスト)取込数: 0 枠")
        self.det_src_count_label.setVisible(False)
        self.sidebar.addWidget(self.det_src_count_label)
        
        # --- 6. ID管理 ---
        add_id_box = QtWidgets.QGroupBox("ID管理 / 出場区間")
        add_id_box.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        _gf = add_id_box.font()
        _gf.setPointSize(int(_gf.pointSize() * 1.5))
        add_id_box.setFont(_gf)
        add_v = QtWidgets.QVBoxLayout(add_id_box) # グループボックス内に直接レイアウトを設定
        add_v.setSpacing(self.dp(4))
        add_v.setContentsMargins(self.dp(6), self.dp(18), self.dp(6), self.dp(6))
        
        # 現在のIDラベルをID管理グループボックスの先頭に配置 (グループボックスのレイアウト add_v に追加)
        self.id_label = QtWidgets.QLabel("現在のID: 1")
        bold = self.id_label.font()
        bold.setPointSize(int(bold.pointSize() * 1.5))
        bold.setBold(True)
        self.id_label.setFont(bold)
        self.id_label.setStyleSheet("color: blue; background: white; border-radius: 3px; padding: 1px 4px;")
        add_v.addWidget(self.id_label)

        self.add_id_btn = QtWidgets.QPushButton("＋ ID追加")
        self.add_id_btn.setMinimumHeight(self.em(1.4))
        self.add_id_btn.clicked.connect(self.prompt_add_id)
        add_v.addWidget(self.add_id_btn)

        bulk_row = QtWidgets.QHBoxLayout()
        bulk_row.setSpacing(self.dp(4))
        self.bulk_combo = QtWidgets.QComboBox()
        self.bulk_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.bulk_combo.setMinimumHeight(self.em(1.2))
        bulk_row.addWidget(self.bulk_combo, 1)

        self.bulk_del_btn = QtWidgets.QPushButton("🗑 選択IDを全削除")
        self.bulk_del_btn.setMinimumHeight(self.em(1.2))
        self.bulk_del_btn.clicked.connect(self.delete_selected_id_globally)
        bulk_row.addWidget(self.bulk_del_btn)

        add_v.addLayout(bulk_row)

        # --- ラベルチェックトグル ---
        self.label_check_toggle = QtWidgets.QCheckBox("ラベル チェック")
        self.label_check_toggle.setFont(_sf)
        self.label_check_toggle.toggled.connect(self._on_label_check_toggled)
        add_v.addWidget(self.label_check_toggle)

        # --- ラベルチェック展開パネル（トグルON時に表示） ---
        self.label_check_panel = self._build_label_check_panel()
        self.label_check_panel.setVisible(False)
        add_v.addWidget(self.label_check_panel)

        # ID一覧のスクロールエリア
        self.id_scroll = QtWidgets.QScrollArea()
        self.id_scroll.setWidgetResizable(True)
        self.id_scroll.setMinimumHeight(self.em(12))
        self.id_scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.id_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.id_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        
        self.id_scroll_widget = QtWidgets.QWidget()
        self.id_scroll_layout = QtWidgets.QVBoxLayout(self.id_scroll_widget)
        self.id_scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.id_scroll_layout.setSpacing(self.dp(3))
        self.id_scroll.setWidget(self.id_scroll_widget)
        add_v.addWidget(self.id_scroll)

        self.sidebar.addWidget(add_id_box) # グループボックスをサイドバーに追加

        # --- 7. ヘルプボタン（左下固定） ---
        self.sidebar.addStretch(1)
        self._help_btn = QtWidgets.QPushButton("?")
        self._help_btn.setFixedSize(self.dp(30), self.dp(30))
        self._help_btn.setCheckable(True)
        self._help_btn.setToolTip("使い方 / ショートカット")
        self._help_btn.setStyleSheet("""
            QPushButton {
                font-size: 13px; font-weight: bold;
                border-radius: 15px;
                background: #555; color: #eee; border: none;
            }
            QPushButton:hover  { background: #777; }
            QPushButton:checked { background: #3a7bd5; }
        """)
        self._help_btn.clicked.connect(self._toggle_help_popup)
        self.sidebar.addWidget(self._help_btn)

        return loaded_widget

    # -------- Phase2 UI --------
    def _build_phase2_ui(self) -> QtWidgets.QWidget:
        """フェーズ2：ラベル数チェックUIを構築"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(self.dp(8), self.dp(8), self.dp(8), self.dp(8))
        v.setSpacing(self.dp(6))

        # 指定個数の設定
        count_row = QtWidgets.QHBoxLayout()
        count_row.addWidget(QtWidgets.QLabel("チェックするラベル数:"))
        self.phase2_count_spin = QtWidgets.QSpinBox()
        self.phase2_count_spin.setRange(1, 100)
        self.phase2_count_spin.setValue(11)
        self.phase2_count_spin.setFixedWidth(self.dp(60))
        count_row.addWidget(self.phase2_count_spin)
        count_row.addStretch()
        v.addLayout(count_row)

        # チェック実行ボタン
        self.phase2_check_btn = QtWidgets.QPushButton("🔍 チェック実行")
        self.phase2_check_btn.clicked.connect(self.run_phase2_check)
        v.addWidget(self.phase2_check_btn)

        # 結果サマリーラベル
        self.phase2_summary_label = QtWidgets.QLabel("（未チェック）")
        self.phase2_summary_label.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(self.phase2_summary_label)

        # 結果一覧テーブル（フレーム番号 / 実際の個数）
        self.phase2_table = QtWidgets.QTableWidget(0, 2)
        self.phase2_table.setHorizontalHeaderLabels(["フレーム番号", "ラベル数"])
        self.phase2_table.horizontalHeader().setStretchLastSection(True)
        self.phase2_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.phase2_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.phase2_table.verticalHeader().setVisible(False)
        self.phase2_table.setAlternatingRowColors(True)
        self.phase2_table.cellDoubleClicked.connect(self._phase2_jump_to_frame)
        v.addWidget(self.phase2_table)

        hint = QtWidgets.QLabel("※ 行をダブルクリックでそのフレームへジャンプ")
        hint.setStyleSheet("color: gray; font-size: 10px;")
        v.addWidget(hint)

        return w

    # -------- Phase1 ラベルチェックインラインパネル --------
    # -------- ヘルプポップアップ --------
    def _build_help_popup(self) -> QtWidgets.QWidget:
        """ショートカット一覧のフローティング非モーダルパネルを生成"""
        popup = QtWidgets.QWidget(None, QtCore.Qt.Tool)
        popup.setWindowTitle("使い方 / ショートカット")
        popup.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)

        v = QtWidgets.QVBoxLayout(popup)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)

        title = QtWidgets.QLabel("使い方 / ショートカット")
        f = title.font(); f.setBold(True); title.setFont(f)
        v.addWidget(title)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet("color: #666;")
        v.addWidget(sep)

        shortcuts = [
            ("A / D",           "前 / 次フレーム"),
            ("Shift + A / D",       "±10フレーム移動"),
            ("G",               "フレームジャンプ入力欄へ移動"),
            ("Space",           "再生 / 停止"),
            ("Q",               "モード切替（選択 / 編集）"),
            ("Ctrl+S",          "保存形式選択"),
            ("マウススクロール",        "ズーム"),
            ("右ドラック",        "視点移動"),
            ("開 / 終",          "出場区間の設定"),
            ("⋯",               "出場区間の手動編集"),
            ("➡",               "未付与の最小フレームにジャンプ"),
        ]
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(3)
        grid.setColumnMinimumWidth(0, 130)
        for row, (key, desc) in enumerate(shortcuts):
            key_lbl = QtWidgets.QLabel(key)
            key_lbl.setStyleSheet("font-family: monospace; font-size: 11px; color: #aaa;")
            desc_lbl = QtWidgets.QLabel(desc)
            desc_lbl.setStyleSheet("font-size: 11px;")
            grid.addWidget(key_lbl, row, 0)
            grid.addWidget(desc_lbl, row, 1)
        v.addLayout(grid)

        popup.adjustSize()
        return popup

    def _toggle_help_popup(self, checked: bool):
        """ヘルプポップアップの表示/非表示を切り替える"""
        if not hasattr(self, '_help_popup'):
            self._help_popup = self._build_help_popup()

        if not checked:
            self._help_popup.hide()
            return

        # メインウィンドウの左下に配置
        popup_size = self._help_popup.sizeHint()
        btn_pos = self._help_btn.mapToGlobal(QtCore.QPoint(0, 0))
        x = btn_pos.x()
        y = btn_pos.y() - popup_size.height() - self.dp(8)
        self._help_popup.move(x, y)
        self._help_popup.show()

    def _build_label_check_panel(self) -> QtWidgets.QWidget:
        """Phase1 サイドバー内に展開するラベルチェックミニパネル"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, self.dp(4), 0, self.dp(4))
        v.setSpacing(self.dp(4))

        # チェックするラベル数
        count_row = QtWidgets.QHBoxLayout()
        lc_count_label = QtWidgets.QLabel("チェックするラベル数:")
        _lf = lc_count_label.font()
        _lf.setPointSize(_lf.pointSize())
        lc_count_label.setFont(_lf)
        count_row.addWidget(lc_count_label)
        self.lc_count_spin = QtWidgets.QSpinBox()
        self.lc_count_spin.setRange(1, 100)
        self.lc_count_spin.setValue(11)
        self.lc_count_spin.setFont(_lf)
        self.lc_count_spin.setFixedWidth(self.dp(80))
        # Phase2 スピンと双方向同期
        self.lc_count_spin.valueChanged.connect(
            lambda val: self.phase2_count_spin.setValue(val)
            if hasattr(self, 'phase2_count_spin') and self.phase2_count_spin.value() != val else None
        )
        count_row.addWidget(self.lc_count_spin)
        count_row.addStretch()
        v.addLayout(count_row)

        # チェック実行ボタン
        lc_check_btn = QtWidgets.QPushButton("🔍 チェック実行")
        lc_check_btn.clicked.connect(self._run_label_check_from_phase1)
        v.addWidget(lc_check_btn)

        # サマリーラベル
        self.lc_summary_label = QtWidgets.QLabel("（未チェック）")
        self.lc_summary_label.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(self.lc_summary_label)

        # 結果テーブル
        self.lc_table = QtWidgets.QTableWidget(0, 2)
        self.lc_table.setHorizontalHeaderLabels(["フレーム番号", "ラベル数"])
        self.lc_table.horizontalHeader().setStretchLastSection(True)
        self.lc_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.lc_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.lc_table.verticalHeader().setVisible(False)
        self.lc_table.setAlternatingRowColors(True)
        self.lc_table.cellDoubleClicked.connect(self._lc_jump_to_frame)
        v.addWidget(self.lc_table)

        hint = QtWidgets.QLabel("※ 行をダブルクリックでそのフレームへジャンプ")
        hint.setStyleSheet("color: gray; font-size: 10px;")
        v.addWidget(hint)

        return w

    def _on_phase4_toggled(self, checked: bool):
        """ID追跡トグル ON/OFF"""
        if checked and not self.image_paths:
            QtWidgets.QMessageBox.warning(self, "警告", "先に画像を読み込んでください。")
            self.phase4_toggle.setChecked(False)
            return
        self.phase4_active = checked
        self._phase4_zoom_label.setVisible(checked)
        self.phase4_zoom_spin.setVisible(checked)
        if checked:
            self._phase4_on_activate()
        else:
            self._phase4_on_deactivate()

    def _on_label_check_toggled(self, checked: bool):
        """ラベルチェックトグル ON/OFF"""
        self.label_check_mode = checked
        self.label_check_indicator.setVisible(checked)
        self.label_check_panel.setVisible(checked)
        # Phase2 スピンと値を合わせる
        if checked and hasattr(self, 'phase2_count_spin') and hasattr(self, 'lc_count_spin'):
            self.lc_count_spin.setValue(self.phase2_count_spin.value())
        self.update_mode_label()
        if checked:
            self._update_label_check_indicator()

    def _update_label_check_indicator(self):
        """現在フレームのラベル数を確認し、インジケーター色を更新する"""
        if not hasattr(self, 'label_check_indicator') or not self.label_check_mode:
            return
        if not self.image_paths:
            self.label_check_indicator.setStyleSheet("background-color: gray; border: 1px solid #555;")
            return
        _, frame_number = self.image_paths[self.current_frame_index]
        target = self.lc_count_spin.value() if hasattr(self, 'lc_count_spin') else 11
        actual = len(self.detections.get(frame_number, []))
        color = "#00CC00" if actual == target else "#FF2222"
        self.label_check_indicator.setStyleSheet(
            f"background-color: {color}; border: 2px solid #333; border-radius: 3px;"
        )

    def _run_label_check_from_phase1(self):
        """Phase1 パネルからチェック実行"""
        if hasattr(self, 'phase2_count_spin') and hasattr(self, 'lc_count_spin'):
            self.phase2_count_spin.setValue(self.lc_count_spin.value())
        self.run_phase2_check()
        self._sync_lc_table()
        if hasattr(self, 'lc_summary_label') and hasattr(self, 'phase2_summary_label'):
            self.lc_summary_label.setText(self.phase2_summary_label.text())
            self.lc_summary_label.setStyleSheet(self.phase2_summary_label.styleSheet())
        self._update_label_check_indicator()

    def _sync_lc_table(self):
        """phase2_table の内容を lc_table にコピーする"""
        if not hasattr(self, 'lc_table') or not hasattr(self, 'phase2_table'):
            return
        self.lc_table.setRowCount(0)
        for row in range(self.phase2_table.rowCount()):
            self.lc_table.insertRow(row)
            for col in range(2):
                src = self.phase2_table.item(row, col)
                if src:
                    dst = QtWidgets.QTableWidgetItem(src.text())
                    dst.setTextAlignment(src.textAlignment())
                    dst.setForeground(src.foreground())
                    self.lc_table.setItem(row, col, dst)

    def _lc_jump_to_frame(self, row: int, _col: int):
        """lc_table の行ダブルクリックでフレームジャンプ"""
        item = self.lc_table.item(row, 0)
        if not item:
            return
        try:
            target_frame = int(item.text())
        except ValueError:
            return
        for i, (_, fn) in enumerate(self.image_paths):
            if fn == target_frame:
                self.current_frame_index = i
                self.load_image()
                return

    def run_phase2_check(self):
        """フェーズ2：全フレームのラベル数をチェックして一覧に表示"""
        if not self.image_paths:
            QtWidgets.QMessageBox.warning(self, "警告", "先に画像を読み込んでください。")
            return

        target_count = self.phase2_count_spin.value()
        ng_frames = []  # [(frame_number, actual_count), ...]

        for _, frame_number in self.image_paths:
            # 遅延ロード
            self._load_frame_if_needed(frame_number)
            actual = len(self.detections.get(frame_number, []))
            if actual != target_count:
                ng_frames.append((frame_number, actual))

        # テーブルを更新
        self.phase2_table.setRowCount(0)
        for frame_number, actual in ng_frames:
            row = self.phase2_table.rowCount()
            self.phase2_table.insertRow(row)
            fn_item = QtWidgets.QTableWidgetItem(str(frame_number))
            fn_item.setTextAlignment(QtCore.Qt.AlignCenter)
            cnt_item = QtWidgets.QTableWidgetItem(str(actual))
            cnt_item.setTextAlignment(QtCore.Qt.AlignCenter)
            # 0個は赤、多すぎは橙で色分け
            if actual == 0:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("red")))
            elif actual < target_count:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("orange")))
            else:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("blue")))
            self.phase2_table.setItem(row, 0, fn_item)
            self.phase2_table.setItem(row, 1, cnt_item)

        total = len(self.image_paths)
        ng = len(ng_frames)
        ok = total - ng
        if ng == 0:
            self.phase2_summary_label.setText(f"✅ 全 {total} フレームが {target_count} 個で一致しています。")
            self.phase2_summary_label.setStyleSheet("color: green; font-size: 11px;")
        else:
            self.phase2_summary_label.setText(
                f"⚠️ {ng} / {total} フレームで不一致（OK: {ok} / NG: {ng}）"
            )
            self.phase2_summary_label.setStyleSheet("color: red; font-size: 11px;")

    def _phase2_jump_to_frame(self, row: int, _col: int):
        """フェーズ2の一覧からフレームにジャンプ"""
        item = self.phase2_table.item(row, 0)
        if not item:
            return
        try:
            target_frame = int(item.text())
        except ValueError:
            return
        for i, (_, fn) in enumerate(self.image_paths):
            if fn == target_frame:
                self.current_frame_index = i
                self.load_image()
                # Phase1タブに切り替えて画像を確認しやすくする
                self.phase_tab.setCurrentIndex(0)
                return

    # -------- フェーズ切り替え --------
    # -------- Phase4 UI --------
    def _build_phase4_ui(self) -> QtWidgets.QWidget:
        """フェーズ4：ID追跡ズームUIを構築"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(self.dp(8), self.dp(8), self.dp(8), self.dp(8))
        v.setSpacing(self.dp(6))

        # --- 追跡IDの選択 ---
        lbl = QtWidgets.QLabel("追跡するIDを選択:")
        lbl.setStyleSheet("font-weight: bold;")
        v.addWidget(lbl)

        self.phase4_id_label = QtWidgets.QLabel("未選択")
        self.phase4_id_label.setStyleSheet(
            "color: blue; font-weight: bold; font-size: 13px;"
        )
        v.addWidget(self.phase4_id_label)

        # IDリスト（スクロール可能）
        self.phase4_id_scroll = QtWidgets.QScrollArea()
        self.phase4_id_scroll.setWidgetResizable(True)
        self.phase4_id_scroll.setMaximumHeight(self.em(10))
        self.phase4_id_inner = QtWidgets.QWidget()
        self.phase4_id_layout = QtWidgets.QVBoxLayout(self.phase4_id_inner)
        self.phase4_id_layout.setContentsMargins(0, 0, 0, 0)
        self.phase4_id_layout.setSpacing(2)
        self.phase4_id_scroll.setWidget(self.phase4_id_inner)
        v.addWidget(self.phase4_id_scroll)

        v.addSpacing(self.dp(4))

        # --- ズーム倍率設定 ---
        zoom_row = QtWidgets.QHBoxLayout()
        zoom_row.addWidget(QtWidgets.QLabel("ズーム倍率:"))
        self._phase4_panel_zoom_spin = QtWidgets.QDoubleSpinBox()
        self._phase4_panel_zoom_spin.setRange(1.0, 20.0)
        self._phase4_panel_zoom_spin.setSingleStep(0.5)
        self._phase4_panel_zoom_spin.setValue(4.0)
        self._phase4_panel_zoom_spin.setSuffix(" x")
        self._phase4_panel_zoom_spin.setFixedWidth(self.dp(80))
        zoom_row.addWidget(self._phase4_panel_zoom_spin)
        zoom_row.addStretch()
        v.addLayout(zoom_row)

        v.addSpacing(self.dp(4))

        # --- 再生コントロール ---
        play_row = QtWidgets.QHBoxLayout()
        self.phase4_play_btn = QtWidgets.QPushButton("▶ 再生")
        self.phase4_play_btn.setMinimumHeight(self.em(1.6))
        self.phase4_play_btn.clicked.connect(self._phase4_toggle_play)
        play_row.addWidget(self.phase4_play_btn)

        speed_lbl = QtWidgets.QLabel("速度:")
        play_row.addWidget(speed_lbl)
        self.phase4_speed_combo = QtWidgets.QComboBox()
        self.phase4_speed_combo.addItems(["0.5x", "1x", "2x", "3x", "5x"])
        self.phase4_speed_combo.setCurrentText("1x")
        self.phase4_speed_combo.setMinimumWidth(self.dp(70))
        play_row.addWidget(self.phase4_speed_combo)
        v.addLayout(play_row)

        # フレーム情報
        self.phase4_frame_label = QtWidgets.QLabel("フレーム: - / -")
        self.phase4_frame_label.setStyleSheet("font-size: 11px; color: gray;")
        v.addWidget(self.phase4_frame_label)

        hint = QtWidgets.QLabel(
            "A/D：1フレーム移動\n"
            "Space：再生/停止\n"
            "IDボタン：追跡対象を切り替え"
        )
        hint.setStyleSheet("font-size: 10px; color: gray;")
        v.addWidget(hint)

        v.addStretch()
        return w

    def _phase4_rebuild_id_list(self):
        """Phase4のIDリストボタンを再構築"""
        if not hasattr(self, 'phase4_id_layout'):
            return
        while self.phase4_id_layout.count():
            item = self.phase4_id_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for id_ in self.id_list:
            btn = QtWidgets.QPushButton(str(id_))
            btn.setFixedHeight(26)
            is_selected = (id_ == self.phase4_tracking_id)
            color = self.color_for(id_)
            r, g, b = color.red(), color.green(), color.blue()
            if is_selected:
                btn.setStyleSheet(
                    f"QPushButton{{background-color:rgb({r},{g},{b});"
                    f"color:white;font-weight:bold;border-radius:3px;"
                    f"border:2px solid #333;}}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton{{background-color:rgb({r},{g},{b},80);"
                    f"color:black;border-radius:3px;border:1px solid #aaa;}}"
                )
            btn.clicked.connect(lambda _, x=id_: self._phase4_select_id(x))
            self.phase4_id_layout.addWidget(btn)
        self.phase4_id_layout.addStretch()

    def _phase4_select_id(self, id_: str):
        """Phase4で追跡IDを選択"""
        self.phase4_tracking_id = id_
        self.phase4_id_label.setText(f"追跡中: {id_}")
        self._phase4_rebuild_id_list()
        # 現在フレームで即ズーム適用
        self._phase4_apply_zoom()

    def _phase4_apply_zoom(self):
        """現在フレームで現在IDのBBoxにズームを合わせる"""
        if not self.image_paths or not getattr(self, 'phase4_active', False):
            return
        tracking_id = self.current_id
        if not tracking_id:
            return
        _, frame_number = self.image_paths[self.current_frame_index]
        boxes = self.detections.get(frame_number, [])

        # 追跡IDのBBoxを探す
        target_box = None
        for box in boxes:
            if str(box[4]) == str(tracking_id):
                target_box = box
                break

        if target_box is not None:
            x, y, w, h = target_box[0], target_box[1], target_box[2], target_box[3]
            cx = x + w / 2.0
            cy = y + h / 2.0
            # 前回位置を更新
            self._phase4_last_cx = cx
            self._phase4_last_cy = cy
        else:
            # BBoxがないフレームは直前の位置をキープ
            if not hasattr(self, '_phase4_last_cx'):
                return
            cx = self._phase4_last_cx
            cy = self._phase4_last_cy

        # ズーム倍率を適用（画像の初期フィットスケール × 倍率）
        zoom_factor = self.phase4_zoom_spin.value()
        target_scale = self._min_zoom_scale * zoom_factor if self._min_zoom_scale > 0 else zoom_factor

        current_scale = self.view.transform().m11()
        if abs(current_scale - target_scale) > 0.01:
            self.view.resetTransform()
            self.view.scale(target_scale, target_scale)

        # BBox中心にビューを移動
        self.view.centerOn(cx, cy)

    def _phase4_toggle_play(self):
        """追跡ズーム中の再生/停止（Phase1 の再生に委譲）"""
        self.toggle_play_pause()

    def _phase4_play_next_frame(self):
        """Phase4再生中の次フレーム処理"""
        if self.current_frame_index < len(self.image_paths) - 1:
            self.current_frame_index += 1
            self._load_image_fast()
            self._phase4_apply_zoom()
            self._phase4_update_frame_label()
        else:
            # 末端で停止
            self.play_timer.stop()
            self.is_playing = False
            self.phase4_play_btn.setText("▶ 再生")
            # タイマーを通常の再生に戻す
            self._phase4_restore_play_timer()

    def _phase4_restore_play_timer(self):
        """play_timerのコネクションをPhase1の_play_next_frameに戻す"""
        try:
            self.play_timer.timeout.disconnect()
        except TypeError:
            pass
        self.play_timer.timeout.connect(self._play_next_frame)

    def _phase4_update_frame_label(self):
        """Phase4のフレームラベルを更新"""
        if not hasattr(self, 'phase4_frame_label') or not self.image_paths:
            return
        _, frame_number = self.image_paths[self.current_frame_index]
        self.phase4_frame_label.setText(
            f"フレーム: {self.current_frame_index + 1} / {len(self.image_paths)}"
            f"  (番号: {frame_number})"
        )

    def _phase4_on_activate(self):
        """追跡ズーム有効化：現在フレームにズームを適用"""
        self._phase4_apply_zoom()

    def _phase4_on_deactivate(self):
        """追跡ズーム無効化：ズームをリセット"""
        if self._min_zoom_scale > 0:
            self.view.resetTransform()
            self.view.scale(self._min_zoom_scale, self._min_zoom_scale)
            self.view.horizontalScrollBar().setValue(0)

    def switch_phase(self, phase: int):
        """フェーズを切り替える（1〜4）"""
        if not hasattr(self, 'phase_tab'):
            return
        if phase == 3:
            self._activate_phase3()
        else:
            # Phase3から離れる場合は先に後処理
            if self.current_phase == 3:
                self._deactivate_phase3()
            # タブは Phase1 のみ（Phase2/4 はタブなし）
            tab_map = {1: 0}
            self.phase_tab.setCurrentIndex(tab_map.get(phase, 0))

    def _activate_phase3(self):
        """Phase3全画面を表示"""
        if not self.image_paths:
            reply = QtWidgets.QMessageBox.question(
                self,
                "画像未読み込み",
                "追跡フェーズには画像と検出データが必要です。\n今すぐ画像フォルダを読み込みますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self.load_images()
            if not self.image_paths:
                return
        # Phase3移行前にID追跡の状態を保存し、一時的に無効化
        self._phase4_was_active = getattr(self, 'phase4_active', False)
        if getattr(self, 'phase4_active', False):
            self._phase4_on_deactivate()
            self.phase4_active = False
            if hasattr(self, 'phase4_toggle'):
                self.phase4_toggle.setChecked(False)
        self.current_phase = 3
        self.main_content_stack.setCurrentIndex(1)
        if hasattr(self, 'act_detect_phase'):
            self.act_detect_phase.setChecked(False)
        if hasattr(self, 'act_track_phase'):
            self.act_track_phase.setChecked(True)
        # Phase1の現在フレーム（原フレーム番号）を共有フレームとして保存
        if self.image_paths:
            _, frame_number = self.image_paths[self.current_frame_index]
            self.store.shared_frame = frame_number
        # 未読み込みフレームのアノテーションをまとめて読み込む
        self._preload_all_annotations()
        if _PHASE3_AVAILABLE and hasattr(self, 'phase3_widget'):
            self.phase3_widget.on_phase_activate()
            # Phase3の内部フレームを共有フレームにシーク
            orig = self.phase3_widget.original_frame_numbers
            if orig and self.store.shared_frame in orig:
                idx = orig.index(self.store.shared_frame)
                self.phase3_widget._seek(idx)

    def _preload_all_annotations(self):
        """全フレームのアノテーション（JSON）を遅延読み込みで一括取得する。
        追跡フェーズに切り替わる前に呼ぶことで、store.detections を完全な状態にする。"""
        for _, frame_number in self.image_paths:
            self._load_frame_if_needed(frame_number)

    def _deactivate_phase3(self):
        """Phase3全画面を閉じて通常レイアウトに戻す"""
        if _PHASE3_AVAILABLE and hasattr(self, 'phase3_widget'):
            # Phase3の現在フレーム（原フレーム番号）を共有フレームとして保存
            p3 = self.phase3_widget
            orig = p3.original_frame_numbers
            if orig and p3.current_frame < len(orig):
                self.store.shared_frame = orig[p3.current_frame]
            p3.on_phase_deactivate()
        self.current_phase = 1
        self.main_content_stack.setCurrentIndex(0)
        if hasattr(self, 'act_detect_phase'):
            self.act_detect_phase.setChecked(True)
        if hasattr(self, 'act_track_phase'):
            self.act_track_phase.setChecked(False)
        # Phase1の表示フレームを共有フレームに同期
        if self.store.shared_frame and self.image_paths:
            for idx, (_, fn) in enumerate(self.image_paths):
                if fn == self.store.shared_frame:
                    self.current_frame_index = idx
                    break
        self.load_image()
        self.rebuild_id_list_ui()
        # Phase3移行前にONだったID追跡チェックを復元
        if getattr(self, '_phase4_was_active', False):
            self._phase4_was_active = False
            if hasattr(self, 'phase4_toggle'):
                self.phase4_toggle.setChecked(True)  # toggled シグナルで _on_phase4_toggled が呼ばれる

    def _on_phase_tab_changed(self, new_idx: int):
        """タブ切り替え時の処理"""
        if new_idx == 1:
            # Phase 3 タブ → Phase3 全画面に切り替え
            self._activate_phase3()
        else:
            # Phase 1 タブ → Phase1 に戻る
            if self.current_phase == 3:
                self._deactivate_phase3()
    def _json_path_for_image(self, img_path: str):
        return os.path.splitext(img_path)[0] + ".json"

    def _read_labelme_json(self, json_path: str) -> List[Box]:
        boxes: List[Box] = []
        if not os.path.exists(json_path):
            return boxes
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for shp in data.get("shapes", []):
                if shp.get("shape_type") != "rectangle":
                    continue
                pts = shp.get("points", [])
                if len(pts) != 2:
                    continue
                (x1, y1), (x2, y2) = pts
                x, y = float(min(x1, x2)), float(min(y1, y2))
                w, h = float(abs(x2 - x1)), float(abs(y2 - y1))
                label = str(shp.get("label", "")).strip()
                if not label:
                    continue
                boxes.append([x, y, w, h, label])
        except Exception:
            pass
        return boxes

    def _write_labelme_json(self, img_path: str, frame_boxes: List[Box]):
        data = {
            "imagePath": os.path.basename(img_path),
            "imageData": None,
            "shapes": [],
            "version": "4.5.6",
            "flags": {},
        }
        for x, y, w, h, label in frame_boxes:
            data["shapes"].append({
                "label": str(label),
                "shape_type": "rectangle",
                "points": [[x, y], [x + w, y + h]],
                "group_id": None,
                "flags": {},
            })
        json_path = self._json_path_for_image(img_path)
        # ファイルが空の場合（ボックスなし）はJSONファイルは作成しない/削除する
        if not frame_boxes and os.path.exists(json_path):
            try:
                os.remove(json_path)
            except OSError:
                pass # 削除失敗は無視
            return
            
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_existing_annotations(self, progress=None) -> int:
        """既存JSONファイルをすべて一括読み込みする"""
        total_files = 0
        self.detections.clear()
        self.loaded_frames.clear()

        total = len(self.image_paths)
        for i, (img_path, frame_number) in enumerate(self.image_paths):
            json_path = self._json_path_for_image(img_path)
            if os.path.exists(json_path):
                total_files += 1
                boxes = self._read_labelme_json(json_path)
                if boxes:
                    self.detections[frame_number] = boxes
                    for _, _, _, _, lab in boxes:
                        if lab not in self.id_list:
                            self.id_list.append(lab)
            self.loaded_frames.add(frame_number)

            # 進捗バー更新（30% → 70% の範囲で）
            if progress and i % 20 == 0 and total > 0:
                percent = 30 + int(40 * i / total)
                progress.setLabelText(f"アノテーションを読み込み中... {percent}% ({i+1}/{total})")
                progress.setValue(percent)
                QtWidgets.QApplication.processEvents()

        if hasattr(self, 'ann_count_label'):
            self.ann_count_label.setText(f"JSONファイル数: {total_files} 件")
        return total_files
    
    def _load_frame_if_needed(self, frame_number: int):
        """指定フレームのアノテーションを遅延読み込み"""
        if frame_number in self.loaded_frames:
            return  # 既に読み込み済み
        
        # 該当する画像パスを探す
        img_path = None
        for path, fnum in self.image_paths:
            if fnum == frame_number:
                img_path = path
                break
        
        if img_path is None:
            return
        
        # JSONを読み込み
        boxes = self._read_labelme_json(self._json_path_for_image(img_path))
        if boxes:
            self.detections[frame_number] = boxes
            # 新しいIDを登録
            for _, _, _, _, lab in boxes:
                if lab not in self.id_list:
                    self.id_list.append(lab)
        
        self.loaded_frames.add(frame_number)
        self._ensure_color_map()

    def _save_meta(self):
        meta = {"id_list": self.id_list, "id_intervals": self.id_intervals}
        try:
            with open(self.meta_path(), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_meta(self):
        p = self.meta_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
            new_list = meta.get("id_list", [])
            if new_list:
                self.id_list = [str(x) for x in new_list]
            self.id_intervals = meta.get("id_intervals", {})
            for k in list(self.id_intervals.keys()):
                self.id_intervals[str(k)] = self.id_intervals.pop(k)
        except Exception:
            pass

    def _find_default_det_file(self, folder: str) -> Optional[str]:
        cand = ["detections.txt", "output.txt", "results.txt"]
        for c in cand:
            p = os.path.join(folder, c)
            if os.path.exists(p):
                return p
        txts = [f for f in os.listdir(folder) if f.lower().endswith(".txt")]
        return os.path.join(folder, txts[0]) if txts else None

    def _import_detections_from_path(self, path: str):
        tmp: Dict[int, List[Box]] = {}
        imported = 0
        
        # ファイルサイズを取得
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "読み込みエラー", "ファイルが見つかりません。")
            return
            
        file_size = os.path.getsize(path)
        
        # ファイル名からフレーム番号を抽出（ファイル名にフレーム番号がある場合は、フレーム番号なし形式のデフォルトフレームとして使用）
        basename = os.path.basename(path)
        digits = re.findall(r'\d+', basename)
        file_frame_number = int(digits[-1]) if digits else 0
        
        # 進捗ダイアログ作成
        progress = QtWidgets.QProgressDialog(
            "検出結果を読み込み中... 0%", "キャンセル", 0, 100, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()
        
        try:
            bytes_read = 0
            last_update_bytes = 0
            # file_sizeが0になるのを防ぐ
            update_interval = max(file_size // 100, 1024 * 1024) if file_size > 0 else 1024 * 1024  # 最低1MB間隔
            
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if progress.wasCanceled():
                        progress.close()
                        QtWidgets.QMessageBox.warning(self, "キャンセル", "読み込みがキャンセルされました。")
                        return
                    
                    # すでに読み込んだバイト数をカウント
                    if file_size > 0:
                        bytes_read += len(line.encode('utf-8'))
                    
                    # 進捗更新（一定バイト数ごと）
                    if file_size > 0 and bytes_read - last_update_bytes >= update_interval:
                        percent = int(100 * bytes_read / file_size)
                        progress.setLabelText(f"検出結果を読み込み中... {percent}%")
                        progress.setValue(min(percent, 99))  # 100%は処理完了後
                        QtWidgets.QApplication.processEvents()
                        last_update_bytes = bytes_read
                    
                    # カンマ or スペースで分割を試す
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    
                    # カンマがあればカンマ区切り、なければスペース区切り
                    if ',' in line_stripped:
                        parts = line_stripped.split(",")
                    else:
                        parts = line_stripped.split()
                    
                    # フォーマット判定
                    if len(parts) >= 6:
                        try:
                            # 最初の要素が整数（フレーム番号）かどうかで判定
                            try:
                                frame = int(parts[0])
                                label = str(parts[1]).strip()
                                # 座標の解釈: 4つの値を取得
                                val1, val2, val3, val4 = map(float, parts[2:6])
                            except ValueError:
                                # フレーム番号なし形式: x1 y1 x2 y2 class [confidence]
                                frame = file_frame_number
                                val1, val2, val3, val4 = map(float, parts[0:4])
                                label = str(parts[4]).strip()
                            
                            # 形式判定: val3 > val1 なら x2座標と判断 → (x1,y1,x2,y2)形式
                            if val3 > val1 and val4 > val2:
                                # (x1, y1, x2, y2) 形式 → (x, y, w, h) に変換
                                x1, y1, x2, y2 = val1, val2, val3, val4
                                x = x1
                                y = y1
                                w = x2 - x1
                                h = y2 - y1
                            else:
                                # (x, y, w, h) 形式
                                x, y, w, h = val1, val2, val3, val4
                            
                        except (ValueError, IndexError):
                            continue
                        
                        tmp.setdefault(frame, []).append([x, y, w, h, label])
                    elif len(parts) >= 5:
                        # フレーム番号なし形式: x1 y1 x2 y2 class [confidence]
                        try:
                            frame = file_frame_number
                            val1, val2, val3, val4 = map(float, parts[0:4])
                            label = str(parts[4]).strip()
                            
                            # 形式判定
                            if val3 > val1 and val4 > val2:
                                # (x1, y1, x2, y2) 形式 → (x, y, w, h) に変換
                                x1, y1, x2, y2 = val1, val2, val3, val4
                                x = x1
                                y = y1
                                w = x2 - x1
                                h = y2 - y1
                            else:
                                # (x, y, w, h) 形式
                                x, y, w, h = val1, val2, val3, val4
                            
                            tmp.setdefault(frame, []).append([x, y, w, h, label])
                        except (ValueError, IndexError):
                            continue
                            
        except Exception as e:
            progress.close()
            QtWidgets.QMessageBox.warning(self, "読み込みエラー", f"ファイルの読み込みに失敗しました:\n{str(e)}")
            return
        
        # データを転送
        progress.setLabelText("データを処理中... 99%")
        progress.setValue(99)
        QtWidgets.QApplication.processEvents()
        
        for _, frame_number in self.image_paths:
            if frame_number not in self.detections or not self.detections[frame_number]:
                if frame_number in tmp:
                    self.detections[frame_number] = tmp[frame_number]
                    self.loaded_frames.add(frame_number)
                    imported += len(tmp[frame_number])

        for frames in tmp.values():
            for _, _, _, _, lab in frames:
                if lab not in self.id_list:
                    self.id_list.append(lab)

        progress.setLabelText("完了! 100%")
        progress.setValue(100)
        QtWidgets.QApplication.processEvents()
        QtCore.QThread.msleep(200)  # 完了を表示
        progress.close()
        
        if hasattr(self, 'det_src_count_label'):
             self.det_src_count_label.setText(f"検出(テキスト)取込数: {imported} 枠")
        self.rebuild_id_list_ui()

    def _import_detections_from_folder(self, folder: str):
        """フォルダ内の全txtファイルから検出結果を読み込む (フレームごとのファイル対応)"""
        # txtファイルを検索
        # 画像フォルダと検出フォルダが異なる場合を想定
        txt_files = glob.glob(os.path.join(folder, "*.txt"))
        
        if not txt_files:
            QtWidgets.QMessageBox.warning(self, "警告", "指定フォルダにtxtファイルが見つかりませんでした。")
            return
        
        # 進捗ダイアログ作成
        total_files = len(txt_files)
        progress = QtWidgets.QProgressDialog(
            f"検出結果を読み込み中... 0% (0/{total_files})", "キャンセル", 0, total_files, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()
        
        tmp: Dict[int, List[Box]] = {}
        total_imported = 0
        
        # 画像のパスとフレーム番号のマップを作成
        image_frame_map = {os.path.basename(path).split('.')[0]: frame for path, frame in self.image_paths}
        
        for i, txt_path in enumerate(txt_files):
            if progress.wasCanceled():
                progress.close()
                QtWidgets.QMessageBox.warning(self, "キャンセル", "読み込みがキャンセルされました。")
                return
            
            # ファイル名からフレーム番号を決定
            basename = os.path.basename(txt_path)
            base_name_no_ext = os.path.splitext(basename)[0]
            
            # まず画像ファイル名との完全一致を試す
            frame_from_filename = image_frame_map.get(base_name_no_ext)
            
            if frame_from_filename is None:
                # ファイル名から数字を抽出してフレーム番号を推定
                digits = re.findall(r'\d+', basename)
                if digits:
                    # 右端の数字列をフレーム番号とする（画像読み込みと同じロジック）
                    frame_candidate = int(digits[-1])
                    
                    # この番号が画像のフレーム番号と一致するか確認
                    frame_numbers = [f for _, f in self.image_paths]
                    if frame_candidate in frame_numbers:
                        frame_from_filename = frame_candidate
                        if i < 3:  # 最初の3ファイルのみデバッグ出力
                            print(f"[フレーム対応] {basename} → フレーム {frame_candidate}")
                    else:
                        # 一致しない場合は0とする（スキップまたは警告）
                        frame_from_filename = 0
                        if i < 3:
                            print(f"[警告] {basename} のフレーム番号 {frame_candidate} が画像フレームに存在しません")
                else:
                    frame_from_filename = 0
                    if i < 3:
                        print(f"[警告] {basename} からフレーム番号を抽出できませんでした")
            else:
                if i < 3:
                    print(f"[完全一致] {basename} → フレーム {frame_from_filename}")
                
            current_frame = frame_from_filename

            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        
                        # カンマがあればカンマ区切り、なければスペース区切り
                        if ',' in line_stripped:
                            parts = line_stripped.split(",")
                        else:
                            parts = line_stripped.split()
                        
                        # フォーマット判定
                        # 6要素以上（フレーム番号,ラベル,x,y,w,h or x1,y1,x2,y2）
                        # 5要素（ラベル,x,y,w,h or x1,y1,x2,y2 - フレーム番号なし）
                        
                        if len(parts) >= 5:
                            try:
                                is_frame_explicit = False
                                try:
                                    # 最初の要素が整数（フレーム番号）かどうかを試す
                                    frame_candidate = int(parts[0])
                                    # 2番目がラベルっぽく文字列かどうかを試す
                                    label_candidate = str(parts[1]).strip()
                                    is_frame_explicit = True
                                except ValueError:
                                    # 最初の要素が座標値っぽい
                                    is_frame_explicit = False
                                    
                                
                                if is_frame_explicit and len(parts) >= 6:
                                    # 形式: frame, label, x, y, w, h
                                    frame = int(parts[0])
                                    label = str(parts[1]).strip()
                                    val1, val2, val3, val4 = map(float, parts[2:6])
                                else:
                                    # 形式: x1, y1, x2, y2, label [confidence] (YOLO形式)
                                    frame = current_frame
                                    val1, val2, val3, val4 = map(float, parts[0:4])
                                    label = str(parts[4]).strip()
                                
                                # 形式判定: val3 > val1 なら x2座標と判断 → (x1,y1,x2,y2)形式
                                if val3 > val1 and val4 > val2:
                                    # (x1, y1, x2, y2) 形式 → (x, y, w, h) に変換
                                    x1, y1, x2, y2 = val1, val2, val3, val4
                                    x = x1
                                    y = y1
                                    w = x2 - x1
                                    h = y2 - y1
                                else:
                                    # (x, y, w, h) 形式
                                    x, y, w, h = val1, val2, val3, val4
                                    
                                tmp.setdefault(frame, []).append([x, y, w, h, label])
                            
                            except (ValueError, IndexError, TypeError):
                                continue


            except Exception as e:
                # 個別ファイルの読み込みエラーは警告してスキップ
                print(f"Warning: Failed to read {txt_path}: {str(e)}")
                continue
            
            # 進捗更新
            percent = int(100 * (i + 1) / total_files)
            progress.setLabelText(f"検出結果を読み込み中... {percent}% ({i+1}/{total_files})")
            progress.setValue(i + 1)
            
            if i % 10 == 0:  # 10ファイルごとにUI更新
                QtWidgets.QApplication.processEvents()
        
        # データを転送
        for _, frame_number in self.image_paths:
            # 既存のJSONデータがないフレームにのみTXTデータをインポート
            if frame_number not in self.detections or not self.detections[frame_number]:
                if frame_number in tmp:
                    self.detections[frame_number] = tmp[frame_number]
                    self.loaded_frames.add(frame_number)
                    total_imported += len(tmp[frame_number])
        
        # IDリストを更新
        for frames in tmp.values():
            for _, _, _, _, lab in frames:
                if lab not in self.id_list:
                    self.id_list.append(lab)
        
        progress.close()
        
        if hasattr(self, 'det_src_count_label'):
            self.det_src_count_label.setText(f"検出(テキスト)取込数: {total_imported} 枠")
        self.rebuild_id_list_ui()
        
        QtWidgets.QMessageBox.information(
            self, 
            "読み込み完了", 
            f"{total_files}個のtxtファイルから{total_imported}個の検出枠を読み込みました。"
        )


    def load_detections_from_txt(self, is_folder: bool):
        # 画像が読み込まれていない場合は画像読み込みを促す
        if not self.image_paths:
            reply = QtWidgets.QMessageBox.question(
                self, 
                "画像未読み込み", 
                "先に画像フォルダを読み込む必要があります。\n今すぐ画像フォルダを選択しますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
            )
            if reply == QtWidgets.QMessageBox.Yes:
                self.load_images()
                # 画像読み込みがキャンセルされた場合は終了
                if not self.image_paths:
                    return
            else:
                return
        
        if not is_folder:
            # 単一ファイルを選択（一括保存形式、またはファイル名にフレーム番号がないフレームごとの形式）
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "検出結果ファイルを選択", filter="Text files (*.txt *.csv);;All files (*)"
            )
            if not path:
                return
            self._import_detections_from_path(path)
        else:
            # フォルダを選択（フレームごとのファイル形式を想定）
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "検出結果フォルダを選択"
            )
            if not folder:
                return
            self._import_detections_from_folder(folder)
        
        self.load_image()

    # -------- 画像読み込み --------
    def load_images(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "画像フォルダを選択")
        if not folder:
            return
        self.image_folder = folder

        # 進捗ダイアログ
        progress = QtWidgets.QProgressDialog(
            "画像ファイルを検索中... 0%", None, 0, 100, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(10)
        QtWidgets.QApplication.processEvents()

        exts = ("*.jpg", "*.png", "*.jpeg")
        self.image_paths = []
        for e in exts:
            for f in glob.glob(os.path.join(folder, e)):
                name = os.path.basename(f)
                # ファイル名中の連続する数字（何桁でもOK）をすべて抽出
                digits = re.findall(r'\d+', name)
                if digits:
                    # 右端の数字列をフレーム番号に採用
                    frame_number = int(digits[-1])
                    self.image_paths.append((f, frame_number))
        
        self.image_paths.sort(key=lambda x: x[1])
        
        if not self.image_paths:
            progress.close()
            QtWidgets.QMessageBox.warning(self, "警告", "指定フォルダに画像ファイルが見つかりませんでした。")
            return

        # UI部品の初期化（load_images実行時に loaded_ui がまだ構築されていない可能性があるため）
        if hasattr(self, 'image_count_label'):
            self.image_count_label.setText(f"画像数: {len(self.image_paths)}")
        
        self.current_frame_index = 0
        self._initial_fit_done = False

        progress.setLabelText("既存アノテーションを確認中... 30%")
        progress.setValue(30)
        QtWidgets.QApplication.processEvents()
        
        # 進捗バーを渡して更新しながら処理
        total_boxes = self._load_existing_annotations(progress)
        
        progress.setLabelText("メタ情報を読み込み中... 70%")
        progress.setValue(70)
        QtWidgets.QApplication.processEvents()
        
        self._load_meta()
        
        # 既存JSONファイルが見つからなかった場合の処理
        if total_boxes == 0:
            progress.setLabelText("検出結果ファイルを探しています... 85%")
            progress.setValue(85)
            QtWidgets.QApplication.processEvents()

            txt_files = glob.glob(os.path.join(folder, "*.txt"))
            progress.close()

            if len(txt_files) > 1:
                # フレームごとのTXTファイルが複数 → フォルダ一括読み込み
                self._import_detections_from_folder(folder)
            elif len(txt_files) == 1:
                # 単一TXTファイル → 単一ファイル読み込み
                self._import_detections_from_path(txt_files[0])
            else:
                # TXTもなければダイアログを表示
                self._prompt_tracking_load()
        else:
            progress.setLabelText("完了! 100%")
            progress.setValue(100)
            QtWidgets.QApplication.processEvents()
            QtCore.QThread.msleep(200)
            progress.close()

        # 画像読み込み成功後、操作UIに切り替える
        self.sidebar_stack.setCurrentIndex(1)
        self.rebuild_id_list_ui() # UI切り替え後にIDリストを構築/更新
        self.update_mode_label() # UI切り替え後にモードラベルを更新
        self.setFocus() # ★修正2: メインウィンドウにフォーカスを戻し、意図しない入力欄へのフォーカスを防ぐ
        self.load_image()

    # -------- UI操作 --------
    def prompt_add_id(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "ID追加", "英数字のIDを入力（例: 12, GK1, FW_A など）:")
        if not ok:
            return
        new_id = str(text).strip()
        if not re.fullmatch(r"[A-Za-z0-9]+", new_id):
            QtWidgets.QMessageBox.warning(self, "無効なID", "英数字のみで入力してください。")
            return
        if new_id in self.id_list:
            QtWidgets.QMessageBox.information(self, "重複", f"ID '{new_id}' は既に存在しています。")
            return
        self.id_list.append(new_id)
        self._ensure_color_map()
        self.rebuild_id_list_ui()
        self._save_meta()
        self._refresh_bulk_combo()

    def set_id(self, new_id: str):
        self.current_id = str(new_id)
        if hasattr(self, 'id_label'):
            self.id_label.setText(f"現在のID: {self.current_id}")
        self.rebuild_id_list_ui()
        self.load_image()
        self._refresh_bulk_combo() 

    def update_mode_label(self):
        if not hasattr(self, 'mode_label'):
            return # loaded_ui がまだ表示されていない可能性

        font = self.mode_label.font()
        font.setBold(True)
        font.setPointSize(int(self.font().pointSize() * 1.5))
        self.mode_label.setFont(font)
        self.guide_label.setVisible(True)

        # 常に通常モード表示（ラベルチェックは独立した機能として上乗せ）
        if self.mode == "select":
            self.mode_label.setText("選択モード")
            self.mode_label.setStyleSheet("color: blue; background: white; border-radius: 3px; padding: 1px 4px;")
            self.guide_label.setText("クリック：ID変更 → 自動で次フレーム")
        else:
            self.mode_label.setText("編集モード")
            self.mode_label.setStyleSheet("color: red; background: white; border-radius: 3px; padding: 1px 4px;")
            if self.require_w_for_add:
                self.guide_label.setText("クリック：削除 / W+ドラッグ：追加")
            else:
                self.guide_label.setText("クリック：削除 / ドラッグ：追加")

    # ---- 進捗/範囲 ----
    def _interval_frames_set(self, id_: str) -> Optional[set]:
        ivs = self.id_intervals.get(id_)
        if not ivs:
            return None
        fset = set()
        frames = self.frame_numbers()
        index = {fn: i for i, fn in enumerate(frames)}
        for s, e in ivs:
            start = s
            end = e if e is not None else frames[-1] if frames else 0
            if start not in index:
                continue
            end_val = end
            if end_val not in index:
                eligible = [fn for fn in frames if fn <= end_val]
                if not eligible:
                    continue
                end_val = eligible[-1]
            si = index[start]
            ei = index[end_val]
            for i in range(si, ei + 1):
                fset.add(frames[i])
        return fset

    def _progress_for_id(self, id_: str) -> Tuple[int, int]:
        frames = self.frame_numbers()
        if not frames:
            return 0, 0
        fset = self._interval_frames_set(id_)
        if fset is None:
            denom = len(frames)
            # 遅延ロードされたフレームのみをチェック
            num = sum(1 for _, fid in self.image_paths if fid in self.loaded_frames and any(b[4] == id_ for b in self.detections.get(fid, [])))
            # 区間がない場合は全フレームを対象
            return num, denom
        else:
            denom = len(fset)
            # 区間内のフレームで、かつ遅延ロードされたフレームのみをチェック
            num = sum(1 for _, fid in self.image_paths if fid in fset and fid in self.loaded_frames and any(b[4] == id_ for b in self.detections.get(fid, [])))
            return num, denom

    # ---- 区間操作 ----
    def current_frame_number(self) -> Optional[int]:
        if not self.image_paths:
            return None
        i = max(0, min(self.current_frame_index, len(self.image_paths) - 1))
        return self.image_paths[i][1]

    def interval_start(self, id_: str):
        fid = self.current_frame_number()
        if fid is None:
            return
        lst = self.id_intervals.setdefault(id_, [])
        # 既に未終了の区間があれば開始は拒否
        if lst and lst[-1][1] is None:
            QtWidgets.QMessageBox.information(self, "情報", f"{id_} は既に開始中です。終了を押して下さい。")
            return
        lst.append([fid, None])
        self._save_meta()
        self.rebuild_id_list_ui()

    def interval_end(self, id_: str):
        fid = self.current_frame_number()
        if fid is None:
            return
        lst = self.id_intervals.get(id_)
        if not lst:
            QtWidgets.QMessageBox.information(self, "情報", f"{id_} は開始されていません。")
            return
        # 末尾から未終了(None)の区間を探して閉じる
        open_idx = None
        for i in range(len(lst) - 1, -1, -1):
            if lst[i][1] is None:
                open_idx = i
                break
        if open_idx is None:
            QtWidgets.QMessageBox.information(self, "情報", f"{id_} は開始されていません。")
            return
        start = lst[open_idx][0]
        if fid < start:
            QtWidgets.QMessageBox.warning(self, "入力エラー", "終了フレームは開始以上である必要があります。")
            return
        lst[open_idx][1] = fid
        # マージ正規化
        self.id_intervals[id_] = self._normalize_intervals(lst)
        self._save_meta()
        self.rebuild_id_list_ui()

    def _normalize_intervals(self, intervals: List[Interval]) -> List[Interval]:
        if not intervals:
            return []
        items = sorted(intervals, key=lambda x: (x[0], x[1] if x[1] is not None else 10**12))
        merged: List[Interval] = []
        for s, e in items:
            if not merged:
                merged.append([s, e]); continue
            ps, pe = merged[-1]
            pe2 = pe if pe is not None else 10**12
            e2 = e if e is not None else 10**12
            if s <= pe2:
                new_end: Optional[int]
                if pe is None or e is None:
                    new_end = None
                else:
                    new_end = max(pe, e)
                merged[-1] = [ps, new_end]
            else:
                merged.append([s, e])
        return merged

    # Undo機能の実装
    def _save_undo_state(self, operation_name: str = "操作"):
        """現在の状態をUndoスタックに保存"""
        if not self.image_paths:
            return
        _, current_frame = self.image_paths[self.current_frame_index]
        state = {
            "operation": operation_name,
            "frame_number": current_frame,
            "detections": [box[:] for box in self.detections.get(current_frame, [])],
            "id_intervals": {k: [iv[:] for iv in v] for k, v in self.id_intervals.items()},
            "current_id": self.current_id,
            "frame_index": self.current_frame_index
        }
        self.undo_stack.append(state)
        if len(self.undo_stack) > self.max_undo_history:
            self.undo_stack.pop(0)

    def undo_last_operation(self):
        """最後の操作を元に戻す"""
        if not self.undo_stack:
            QtWidgets.QMessageBox.information(self, "Undo", "元に戻す操作がありません。")
            return
        state = self.undo_stack.pop()
        frame_number = state["frame_number"]
        self.detections[frame_number] = [box[:] for box in state["detections"]]
        self.id_intervals = {k: [iv[:] for iv in v] for k, v in state["id_intervals"].items()}
        self.current_id = state["current_id"]
        self.current_frame_index = state["frame_index"]
        if hasattr(self, 'id_combo'):
            self.id_combo.setCurrentText(self.current_id)
        self.load_image()
        self.rebuild_id_list_ui()
        print(f"[Undo] {state['operation']}を元に戻しました")

    def interval_edit(self, id_: str):
        frames = self.frame_numbers()
        dlg = IntervalEditor(self, id_, frames, self.id_intervals.get(id_, []))
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._save_undo_state("区間編集")
            self.id_intervals[id_] = self._normalize_intervals(dlg.result_intervals)
            self._save_meta()
            self.rebuild_id_list_ui()

    def _first_unassigned_frame_for_id(self, id_: str) -> Optional[int]:
        frames = self.frame_numbers()
        if not frames:
            return None
        fset = self._interval_frames_set(id_)
        target_frames = fset if fset is not None else set(frames)
        for _, fid in self.image_paths:
            if fid in target_frames:
                # フレームがまだロードされていない場合はロードを試みる
                self._load_frame_if_needed(fid)
                if not any(b[4] == id_ for b in self.detections.get(fid, [])):
                    return fid
        return None

    def _last_frame_in_scope_for_id(self, id_: str) -> Optional[int]:
        """区間があれば区間内の最後、なければ全体の最後"""
        frames = self.frame_numbers()
        if not frames:
            return None
        fset = self._interval_frames_set(id_)
        if fset:
            return max(fset)
        return frames[-1]

    def jump_to_unassigned_frame(self, id_: str):
        fid = self._first_unassigned_frame_for_id(id_)
        if fid is None:
            fid = self._last_frame_in_scope_for_id(id_)
            if fid is None:
                return
        for i, (_, f) in enumerate(self.image_paths):
            if f == fid:
                self.current_frame_index = i
                self.load_image()
                return

    def jump_to_frame(self):
        """入力されたフレーム番号にジャンプ"""
        if not self.image_paths:
            QtWidgets.QMessageBox.warning(self, "警告", "画像が読み込まれていません。")
            return
        
        # loaded_ui が表示されている前提
        if not hasattr(self, 'jump_frame_input'):
             return # UIが未ロード
             
        frame_text = self.jump_frame_input.text().strip()
        if not frame_text:
            QtWidgets.QMessageBox.warning(self, "入力エラー", "フレーム番号を入力してください。")
            return
        
        try:
            target_frame = int(frame_text)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "入力エラー", "フレーム番号は整数で入力してください。")
            return
        
        # 連番フレーム番号（1始まり）でジャンプ
        total = len(self.image_paths)
        if 1 <= target_frame <= total:
            self.current_frame_index = target_frame - 1
            self.load_image()
            self.jump_frame_input.clear()
            self.setFocus()
        else:
            QtWidgets.QMessageBox.warning(
                self,
                "フレームが見つかりません",
                f"フレーム番号 {target_frame} は範囲外です。\n"
                f"利用可能な範囲: 1 ～ {total}"
            )
            self.setFocus()

    def rebuild_id_list_ui(self):
        if not hasattr(self, 'id_scroll_layout'):
            return # loaded_ui がまだ構築されていない場合はスキップ

        while self.id_scroll_layout.count():
            item = self.id_scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        total_frames = len(self.image_paths)

        self._ensure_color_map()

        # IDボタンの最小幅を設定
        fm_global = self.fontMetrics()
        widest_text = ""
        widest_px = 0
        for _id in self.id_list:
            t = str(_id)                  
            w = fm_global.horizontalAdvance(t)
            if w > widest_px:
                widest_px, widest_text = w, t
        btn_min_w = widest_px + self.dp(20)

        for id_ in self.id_list:
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(2, 1, 2, 1)
            h.setSpacing(4)

            # IDボタン（固定幅）
            btn = QtWidgets.QPushButton(str(id_))
            btn.setFixedHeight(24)
            btn.setFixedWidth(btn_min_w + 8)
            btn.setStyleSheet("QPushButton{padding:1px 4px; margin:0; font-size:12px;}")
            btn.clicked.connect(lambda _, x=id_: self.set_id(x))
            h.addWidget(btn)

            # 進捗ラベル（フレーム数の桁数に合わせた幅）
            done, denom = self._progress_for_id(id_)
            denom_val = denom if denom > 0 else total_frames
            status = f"{done}/{denom_val}"
            prog = QtWidgets.QLabel(status)
            # font-size:11px に対応したQFontで幅を計算（親ウィジェット未設定時の誤差を防ぐ）
            _fnt11 = QtGui.QFont()
            _fnt11.setPixelSize(11)
            _fm11 = QtGui.QFontMetrics(_fnt11)
            _n = max(len(str(done)), len(str(denom_val)))
            _sample = '9' * _n + '/' + '9' * _n
            prog.setFixedWidth(_fm11.horizontalAdvance(_sample) + self.dp(20))
            prog.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            prog.setStyleSheet("QLabel{margin:0; padding:0 2px; font-size:11px;}")
            h.addWidget(prog)

            # 小ボタン群（均等伸縮）
            s = "QPushButton{padding:1px 2px; margin:0; font-size:11px;}"

            def mk(label, cb, tip):
                b = QtWidgets.QPushButton(label)
                b.setFixedHeight(24)
                b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                b.setStyleSheet(s)
                b.setToolTip(tip)
                b.clicked.connect(cb)
                return b

            h.addWidget(mk("開始", lambda _, x=id_: self.interval_start(x), "区間開始"), 1)
            h.addWidget(mk("終了", lambda _, x=id_: self.interval_end(x),   "区間終了"), 1)
            h.addWidget(mk("⋯",   lambda _, x=id_: self.interval_edit(x),   "区間編集"), 1)
            h.addWidget(mk("➡",   lambda _, x=id_: self.jump_to_unassigned_frame(x), "未付与フレームへ"), 1)

            self.id_scroll_layout.addWidget(row)

        if hasattr(self, 'id_scroll_layout'):
            self.id_scroll_layout.addStretch(1)

        if hasattr(self, 'id_label'):
            self.id_label.setText(f"現在のID: {self.current_id}")

        self._refresh_bulk_combo()
        self._refresh_filter_combo()  # フィルターコンボも更新


    def _refresh_bulk_combo(self):
        """コンボボックスのID一覧を最新化し、現在IDに合わせて選択を更新"""
        if not hasattr(self, "bulk_combo"):
            return
        cur = self.bulk_combo.currentText() if self.bulk_combo.count() else None
        self.bulk_combo.blockSignals(True)
        self.bulk_combo.clear()
        for lab in self.id_list:
            self.bulk_combo.addItem(str(lab))
        # 可能なら current_id を選択、なければ以前の選択を復元
        if self.current_id in self.id_list:
            idx = self.id_list.index(self.current_id)
            self.bulk_combo.setCurrentIndex(idx)
        elif cur and cur in self.id_list:
            self.bulk_combo.setCurrentIndex(self.id_list.index(cur))
        self.bulk_combo.blockSignals(False)
    
    def _refresh_filter_combo(self):
        """フィルターコンボボックスのID一覧を更新"""
        if not hasattr(self, "filter_combo"):
            return
        cur = self.filter_combo.currentText() if self.filter_combo.count() else None
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        for lab in self.id_list:
            self.filter_combo.addItem(str(lab))
        # 可能なら current_id を選択、なければ最初のIDを選択
        if self.current_id in self.id_list:
            idx = self.id_list.index(self.current_id)
            self.filter_combo.setCurrentIndex(idx)
        elif cur and cur in self.id_list:
            self.filter_combo.setCurrentIndex(self.id_list.index(cur))
        elif self.id_list:
            self.filter_combo.setCurrentIndex(0)
        self.filter_combo.blockSignals(False)

    def delete_selected_id_globally(self):
        """コンボで選択中のIDを全フレームから一括削除"""
        if not hasattr(self, "bulk_combo") or self.bulk_combo.count() == 0:
            return
        target = self.bulk_combo.currentText().strip()
        if not target:
            return
        self.delete_all_for_id(target)

    def delete_all_for_id(self, target_id: str):
        """指定IDの枠を全フレームから削除し、保存・UI更新"""
        total = 0
        for boxes in self.detections.values():
            total += sum(1 for b in boxes if b[4] == target_id)

        reply = QtWidgets.QMessageBox.question(
            self,
            "確認",
            f"ID '{target_id}' の枠が全フレーム合計 {total}個 存在します。全て削除しますか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        for frame_number, boxes in self.detections.items():
            self.detections[frame_number] = [b for b in boxes if b[4] != target_id]

        self.load_image()
        self.rebuild_id_list_ui() # ★修正1: 全削除後に進捗表示を更新
        QtWidgets.QMessageBox.information(self, "完了", f"ID '{target_id}' の枠を全て削除しました（{total}個）。")

    # -------- 画像表示 --------
    def load_image(self):
        if not self.image_paths:
            return
        
        # 再生中の場合でも、現在のフレームのアノテーションを遅延読み込み
        img_path, frame_number = self.image_paths[self.current_frame_index]
        self._load_frame_if_needed(frame_number)

        # QPixmapを作成
        pixmap = QtGui.QPixmap(img_path)
        if pixmap.isNull():
            return

        self.scene.clear()
        # scene.clear()で全アイテムが削除されるため、hover_item/drag_rectの参照もリセット
        self.hover_item = None
        self.hover_box_index = None
        self.drag_rect = None # ★修正2: ドラッグ中の矩形アイテムもリセット
        
        self.scene.addPixmap(pixmap)
        self.scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())

        # 既存の検出ボックスを描画
        frame_boxes = self.detections.get(frame_number, [])
        for i, (x, y, w, h, label) in enumerate(frame_boxes):
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            box_item.setData(0, i)
            
            # テキストラベルを描画
            text_item = self.scene.addText(str(label))
            text_item.setDefaultTextColor(color)
            text_item.setPos(x, y - 20)

        # フレーム情報を更新
        if hasattr(self, 'frame_index_label'):
            self.frame_index_label.setText(
                f"フレーム: {self.current_frame_index + 1} / {len(self.image_paths)} (番号: {frame_number})"
            )
            
        # ★修正1: フレーム移動/描画時に進捗を更新
        self.rebuild_id_list_ui()

        # ラベルチェックモード ON のときインジケーターを更新
        if getattr(self, 'label_check_mode', False):
            self._update_label_check_indicator()

        # 初回のみ縦ピッタリフィット＆最小スケールを記録
        if not self._initial_fit_done:
            view_h = self.view.viewport().height()
            img_h  = pixmap.height()
            img_w  = pixmap.width()
            if img_h > 0:
                fit_scale = view_h / img_h
                self.view.resetTransform()
                self.view.scale(fit_scale, fit_scale)
                # 横方向を左端に合わせる
                self.view.horizontalScrollBar().setValue(0)
            else:
                self.view.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)
            # 初期スケール（最小ズームとして使う）を保存
            self._min_zoom_scale = self.view.transform().m11()
            self._initial_fit_done = True

        # 追跡ズーム ON のときズームを適用
        if getattr(self, 'phase4_active', False):
            self._phase4_apply_zoom()

    def _update_hover_overlay(self, frame_id: int, box_idx: Optional[int]):
        """ホバー時のハイライト表示を更新"""
        # 前のハイライトを削除
        if self.hover_item is not None:
            # アイテムがまだシーンに属しているか確認
            if self.hover_item.scene() == self.scene:
                self.scene.removeItem(self.hover_item)
            self.hover_item = None
        
        if box_idx is None:
            self.hover_box_index = None
            return

        # 新しいハイライトを描画
        boxes = self.detections.get(frame_id, [])
        if 0 <= box_idx < len(boxes):
            x, y, w, h, label = boxes[box_idx]
            color = self.color_for(label)
            
            # 枠線を太く設定
            pen = QtGui.QPen(color, 4)
            pen.setStyle(QtCore.Qt.DashLine)
            
            # 枠の色を使った半透明の背景色を設定
            brush_color = QtGui.QColor(color)
            brush_color.setAlpha(80)  # 透明度を80/255に設定
            brush = QtGui.QBrush(brush_color)
            
            # 矩形を描画（枠線 + 塗りつぶし）
            self.hover_item = self.scene.addRect(x, y, w, h, pen, brush)
            self.hover_box_index = box_idx
    
    def _quick_redraw_boxes(self):
        """BBox追加・削除時の軽量な再描画（画像は再読み込みしない）"""
        if not self.image_paths:
            return
        
        _, frame_number = self.image_paths[self.current_frame_index]
        
        # 既存のBBoxアイテムとテキストを削除（画像は残す）
        for item in self.scene.items():
            # QGraphicsRectItemとQGraphicsTextItemを削除
            if isinstance(item, (QtWidgets.QGraphicsRectItem, QtWidgets.QGraphicsTextItem)):
                self.scene.removeItem(item)
        
        # hover/drag関連をリセット
        self.hover_item = None
        self.hover_box_index = None
        
        # 既存の検出ボックスを再描画
        frame_boxes = self.detections.get(frame_number, [])
        for i, (x, y, w, h, label) in enumerate(frame_boxes):
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            box_item.setData(0, i)
            
            # テキストラベルを描画
            text_item = self.scene.addText(str(label))
            text_item.setDefaultTextColor(color)
            text_item.setPos(x, y - 20)
        
        # 進捗表示の更新をタイマーで遅延実行（操作が連続する場合は更新を先延ばし）
        self.progress_update_timer.stop()
        self.progress_update_timer.start(self.progress_update_delay)
            
    def _update_drag_rect(self, drag_end_pos: QtCore.QPointF):
        """ドラッグ中の矩形描画を更新"""
        if self.drag_start is None:
            return

        x1, y1 = self.drag_start
        x2, y2 = drag_end_pos.x(), drag_end_pos.y()
        
        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)
        
        # 既存の描画を削除
        if self.drag_rect is not None:
            if self.drag_rect.scene() == self.scene:
                self.scene.removeItem(self.drag_rect)
            self.drag_rect = None
        
        # 新しい描画を追加
        pen = QtGui.QPen(self.color_for(self.current_id), 2)
        pen.setStyle(QtCore.Qt.DotLine)
        brush = QtGui.QBrush(self.color_for(self.current_id), QtCore.Qt.Dense7Pattern)
        brush.setColor(QtGui.QColor(self.color_for(self.current_id).red(), self.color_for(self.current_id).green(), self.color_for(self.current_id).blue(), 64)) # 透明度を下げたブラシ
        
        self.drag_rect = self.scene.addRect(x, y, w, h, pen, brush)


    def eventFilter(self, source, event):
        """マウスイベントをフィルタリング"""

        # Phase3 のジャンプ入力欄でEnterキーを押したらジャンプ実行
        if (event.type() == QtCore.QEvent.KeyPress
                and self.current_phase == 3
                and event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
                and isinstance(source, QtWidgets.QLineEdit)
                and hasattr(self, 'phase3_widget')
                and hasattr(self.phase3_widget, 'ctrl')
                and source is self.phase3_widget.ctrl.jumpEdit):
            self.phase3_widget._on_jump()
            return True

        # Phase3 表示中：テキスト入力以外のキーイベントを DetectionEditor に転送
        if (event.type() == QtCore.QEvent.KeyPress
                and self.current_phase == 3
                and source is not self
                and not isinstance(source, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox))
                and QtWidgets.QApplication.activeWindow() is self):
            self.keyPressEvent(event)
            return True

        # ピンチイン/アウト（トラックパッド NativeGesture、Cmd不要）
        # Phase3のときは P3ImageView 側で処理するため、ここではスキップ
        if event.type() == QtCore.QEvent.NativeGesture and self.current_phase != 3:
            if event.gestureType() == QtCore.Qt.ZoomNativeGesture:
                factor = 1.0 + event.value()
                factor = max(0.85, min(1.15, factor))  # 1ステップの変化量を制限

                # 最小ズーム制限（初期フィットサイズより小さくしない）
                if self._min_zoom_scale > 0:
                    current_scale = self.view.transform().m11()
                    new_scale = current_scale * factor
                    if new_scale < self._min_zoom_scale:
                        factor = self._min_zoom_scale / current_scale

                # AnchorUnderMouseが設定済みのためscaleのみでカーソル中心ズームになる
                self.view.scale(factor, factor)
                return True

        if source is not self.view.viewport():
            return super().eventFilter(source, event)

        if not self.image_paths:
            return super().eventFilter(source, event)

        _, frame_id = self.image_paths[self.current_frame_index]

        # ホバー検出 and ドラッグ中描画 (MouseMove)
        if event.type() == QtCore.QEvent.MouseMove:
            pos = self.view.mapToScene(event.pos())

            # 右ドラッグによる視点移動
            if self._pan_start is not None:
                if not (event.buttons() & QtCore.Qt.RightButton):
                    # 右ボタンが既に離されているならリセット（ContextMenuによるリリース未着を補正）
                    self._pan_start = None
                    self.view.viewport().setCursor(QtCore.Qt.ArrowCursor)
                else:
                    delta = event.pos() - self._pan_start
                    self._pan_start = event.pos()
                    hbar = self.view.horizontalScrollBar()
                    vbar = self.view.verticalScrollBar()
                    hbar.setValue(hbar.value() - delta.x())
                    vbar.setValue(vbar.value() - delta.y())
                    return True

            # 既存ボックスの移動ドラッグ
            if self._moving_box_index is not None and self.mode == "edit":
                vp_dx = event.pos().x() - self._press_pos_viewport.x()
                vp_dy = event.pos().y() - self._press_pos_viewport.y()
                if not self._is_dragging_box and vp_dx * vp_dx + vp_dy * vp_dy > 9:
                    self._is_dragging_box = True
                if self._is_dragging_box:
                    ox, oy, ow, oh, olabel = self._moving_box_original
                    dx = pos.x() - self._moving_press_scene.x()
                    dy = pos.y() - self._moving_press_scene.y()
                    nx, ny = ox + dx, oy + dy
                    if self._moving_ghost is not None and self._moving_ghost.scene() == self.scene:
                        self.scene.removeItem(self._moving_ghost)
                    color = self.color_for(olabel)
                    pen = QtGui.QPen(color, 2)
                    pen.setStyle(QtCore.Qt.DotLine)
                    brush = QtGui.QBrush(QtGui.QColor(color.red(), color.green(), color.blue(), 64))
                    self._moving_ghost = self.scene.addRect(nx, ny, ow, oh, pen, brush)
                return True

            # ★修正2: ドラッグ中の矩形を描画/更新
            if self.drag_start is not None and self.mode == "edit" and (self.add_with_w or not self.require_w_for_add):
                 self._update_drag_rect(pos)
                 return True # ドラッグ中は他のMouseMove処理をスキップ

            # ホバー検出（ドラッグ中でない場合）
            boxes = self.detections.get(frame_id, [])
            found_idx = None
            for i, (x, y, w, h, _) in enumerate(boxes):
                if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                    found_idx = i
                    break
            
            if found_idx != self.hover_box_index:
                self._update_hover_overlay(frame_id, found_idx)

        # マウスボタンイベント (MousePress)
        elif event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == QtCore.Qt.RightButton:
                self._pan_start = event.pos()
                self.view.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
                return True
            if event.button() == QtCore.Qt.LeftButton:
                pos = self.view.mapToScene(event.pos())
                boxes = self.detections.get(frame_id, [])
                self._press_pos_viewport = event.pos() # ドラッグ判定用にビューポート座標を保存

                if self.mode == "select":
                    # 選択モード：IDを変更
                    for i, (x, y, w, h, _) in enumerate(boxes):
                        if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                            self._save_undo_state("ID変更")
                            # IDを変更
                            boxes[i][4] = self.current_id
                            self.detections[frame_id] = boxes  # 明示的に保存
                            # 軽量な再描画
                            self._quick_redraw_boxes()
                            # 最後のフレームでない場合のみ次へ移動
                            if self.current_frame_index < len(self.image_paths) - 1:
                                self.next_frame()
                            return True
                elif self.mode == "edit":
                    # 編集モード：枠クリックで移動準備 or 空き地で新規追加
                    clicked_box = False
                    for i, (x, y, w, h, _) in enumerate(boxes):
                        if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                            # 即削除せず、移動 or クリック削除を MouseRelease で判定
                            self._moving_box_index = i
                            self._moving_box_original = boxes[i][:]
                            self._moving_press_scene = pos
                            self._is_dragging_box = False
                            clicked_box = True
                            break

                    if not clicked_box:
                        # 新規ボックスの開始
                        if self.require_w_for_add and not self.add_with_w:
                            return True
                        drag_pos = self.view.mapToScene(event.pos())
                        self.drag_start = (drag_pos.x(), drag_pos.y())
                        return True

        # マウスボタンイベント (MouseRelease)
        elif event.type() == QtCore.QEvent.MouseButtonRelease:
            if event.button() == QtCore.Qt.RightButton and self._pan_start is not None:
                self._pan_start = None
                self.view.viewport().setCursor(QtCore.Qt.ArrowCursor)
                return True
            if event.button() == QtCore.Qt.LeftButton and self._moving_box_index is not None:
                # ゴースト矩形を削除
                if self._moving_ghost is not None and self._moving_ghost.scene() == self.scene:
                    self.scene.removeItem(self._moving_ghost)
                self._moving_ghost = None

                boxes = self.detections.get(frame_id, [])
                if self._is_dragging_box:
                    # ドラッグ → 移動を確定
                    release_pos = self.view.mapToScene(event.pos())
                    dx = release_pos.x() - self._moving_press_scene.x()
                    dy = release_pos.y() - self._moving_press_scene.y()
                    ox, oy, ow, oh, olabel = self._moving_box_original
                    self._save_undo_state("BBox移動")
                    boxes[self._moving_box_index][0] = ox + dx
                    boxes[self._moving_box_index][1] = oy + dy
                    self.detections[frame_id] = boxes
                    self._quick_redraw_boxes()
                else:
                    # クリックのみ → 削除
                    self._save_undo_state("BBox削除")
                    boxes.pop(self._moving_box_index)
                    self.detections[frame_id] = boxes
                    self._quick_redraw_boxes()

                self._moving_box_index = None
                self._moving_box_original = None
                self._moving_press_scene = None
                self._is_dragging_box = False
                self._press_pos_viewport = None
                return True

            if event.button() == QtCore.Qt.LeftButton and self.drag_start is not None:
                # ★修正2: ドラッグ中の矩形を削除
                if self.drag_rect is not None:
                    self.scene.removeItem(self.drag_rect)
                    self.drag_rect = None
                    
                # ドラッグ終了：新しいボックスを追加
                pos = self.view.mapToScene(event.pos())
                x1, y1 = self.drag_start
                x2, y2 = pos.x(), pos.y()
                
                x = min(x1, x2)
                y = min(y1, y2)
                w = abs(x2 - x1)
                h = abs(y2 - y1)
                
                # ドラッグの最小距離をチェック（クリックと見なされないように）
                dx = event.pos().x() - self._press_pos_viewport.x()
                dy = event.pos().y() - self._press_pos_viewport.y()
                if dx*dx + dy*dy > 4: # 2ピクセル以上の移動
                    if w >= self.min_new_box_w and h >= self.min_new_box_h:
                        self._save_undo_state("BBox追加")
                        if frame_id not in self.detections:
                            self.detections[frame_id] = []
                        self.detections[frame_id].append([x, y, w, h, self.current_id])
                        self._quick_redraw_boxes()
                
                self.drag_start = None
                self._press_pos_viewport = None
                return True

        # ホイールイベント
        elif event.type() == QtCore.QEvent.Wheel:
            mods = event.modifiers()
            angle_y = event.angleDelta().y()
            pixel = event.pixelDelta()

            if mods == QtCore.Qt.ShiftModifier:
                # Shift+スクロール：横スクロール
                hbar = self.view.horizontalScrollBar()
                step = pixel.y() if pixel.manhattanLength() > 0 else angle_y
                hbar.setValue(hbar.value() - step)
                return True
            elif pixel.manhattanLength() > 0 and not (mods & QtCore.Qt.ControlModifier):
                # トラックパッドの2本指スクロール → 視点移動（パン）
                hbar = self.view.horizontalScrollBar()
                vbar = self.view.verticalScrollBar()
                hbar.setValue(hbar.value() - pixel.x())
                vbar.setValue(vbar.value() - pixel.y())
                return True
            elif mods in (QtCore.Qt.NoModifier, QtCore.Qt.ControlModifier):
                # マウスホイール or Ctrl+スクロール → ズーム
                if angle_y != 0:
                    factor = 1.15 if angle_y > 0 else 1.0 / 1.15
                    # 最小ズーム制限
                    if self._min_zoom_scale > 0 and factor < 1.0:
                        current_scale = self.view.transform().m11()
                        if current_scale * factor < self._min_zoom_scale:
                            factor = self._min_zoom_scale / current_scale
                    # AnchorUnderMouseが設定済みのためscaleのみでカーソル中心ズームになる
                    self.view.scale(factor, factor)
                return True
        
        elif event.type() == QtCore.QEvent.ContextMenu and source is self.view.viewport():
            # 右クリックのコンテキストメニューを抑制（右ドラッグパンを優先）
            return True

        elif event.type() == QtCore.QEvent.Leave and source is self.view.viewport():
            self._update_hover_overlay(frame_id, None)
            # ★修正2: ドラッグ中の矩形も削除
            if self.drag_rect is not None:
                self.scene.removeItem(self.drag_rect)
                self.drag_rect = None
            return super().eventFilter(source, event)

        return super().eventFilter(source, event)

    def _ensure_color_map(self):
        used = {idx for id_, idx in self.id_color_map.items() if id_ in self.id_list and 0 <= idx < len(self.palette)}
        for id_ in self.id_list:
            if id_ not in self.id_color_map or not (0 <= self.id_color_map[id_] < len(self.palette)):
                # まだ色が余っていれば最小の未使用インデックスを配る
                free_idx = next((i for i in range(len(self.palette)) if i not in used), None)
                if free_idx is not None:
                    self.id_color_map[id_] = free_idx
                    used.add(free_idx)
                else:
                    # ここからは色不足（IDがパレット数を超過）→ 重複やむなしだが安定させる
                    md5 = hashlib.md5(id_.encode("utf-8")).hexdigest()
                    self.id_color_map[id_] = int(md5, 16) % len(self.palette)

    def showEvent(self, event):
        super().showEvent(event)
        # 初回表示時にサイドバー:画像 = 250:残り に設定
        total = self.content_splitter.width()
        sidebar_w = self.dp(250)
        self.content_splitter.setSizes([sidebar_w, max(100, total - sidebar_w)])

    def keyPressEvent(self, event):
        # Escキー：フォーカスをメインウィンドウに戻す
        if event.key() == QtCore.Qt.Key_Escape:
            self.setFocus()
            return

        # 追跡フェーズ（Phase3）のキーショートカット（他のフェーズより先に処理）
        if self.current_phase == 3 and _PHASE3_AVAILABLE and hasattr(self, 'phase3_widget'):
            p3 = self.phase3_widget
            # Ctrl+Z: アンドゥ
            if event.key() == QtCore.Qt.Key_Z and event.modifiers() == QtCore.Qt.ControlModifier:
                p3.undo_last()
                return
            # Shift / Alt + A/D/←/→: ±10フレーム移動
            if event.modifiers() & (QtCore.Qt.ShiftModifier | QtCore.Qt.AltModifier):
                if event.key() in (QtCore.Qt.Key_D, QtCore.Qt.Key_Right):
                    p3._step(+10)
                    return
                elif event.key() in (QtCore.Qt.Key_A, QtCore.Qt.Key_Left):
                    p3._step(-10)
                    return
            # A/D/←/→: ±1フレーム移動
            if event.key() in (QtCore.Qt.Key_D, QtCore.Qt.Key_Right):
                p3._step(+1)
                return
            elif event.key() in (QtCore.Qt.Key_A, QtCore.Qt.Key_Left):
                p3._step(-1)
                return
            elif event.key() == QtCore.Qt.Key_Space:
                p3._toggle_play()
                return
            elif event.key() == QtCore.Qt.Key_S and event.modifiers() == QtCore.Qt.ControlModifier:
                self.show_save_dialog()
                return
            elif event.key() == QtCore.Qt.Key_S and event.modifiers() == (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
                self.save_all_txt()
                return
            elif event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                p3._on_jump()
                return

        # Spaceキー：再生/停止の切り替え（Phase1/2）
        if event.key() == QtCore.Qt.Key_Space:
            self.toggle_play_pause()
            return

        # Shift / Alt + A/D/←/→: ±10フレーム移動（Phase1/2）
        if event.modifiers() & (QtCore.Qt.ShiftModifier | QtCore.Qt.AltModifier):
            if event.key() in (QtCore.Qt.Key_D, QtCore.Qt.Key_Right):
                self.move_by_frames(+10)
                return
            elif event.key() in (QtCore.Qt.Key_A, QtCore.Qt.Key_Left):
                self.move_by_frames(-10)
                return

        # 追跡ズーム ON のとき A/D → 1フレーム移動＋ズーム追従、Space → 再生
        if getattr(self, 'phase4_active', False):
            if event.key() == QtCore.Qt.Key_D:
                self.next_frame()
                self._phase4_apply_zoom()
                self._phase4_update_frame_label()
                return
            elif event.key() == QtCore.Qt.Key_A:
                self.prev_frame()
                self._phase4_apply_zoom()
                self._phase4_update_frame_label()
                return
            elif event.key() == QtCore.Qt.Key_Space:
                self._phase4_toggle_play()
                return

        if event.key() == QtCore.Qt.Key_Q:
            self.mode = "edit" if self.mode == "select" else "select"
            self.update_mode_label()
        elif event.key() == QtCore.Qt.Key_D:
            self.next_frame()
        elif event.key() == QtCore.Qt.Key_A:
            self.prev_frame()
        elif event.key() == QtCore.Qt.Key_Right:
            self.next_frame()
        elif event.key() == QtCore.Qt.Key_Left:
            self.prev_frame()
        elif event.key() == QtCore.Qt.Key_J:
            # Jキーでフレームジャンプ入力欄にフォーカス
            if self.current_phase == 3 and hasattr(self, 'phase3_widget'):
                self.phase3_widget.ctrl.jumpEdit.setFocus()
                self.phase3_widget.ctrl.jumpEdit.selectAll()
            elif hasattr(self, 'jump_frame_input'):
                self.jump_frame_input.setFocus()
                self.jump_frame_input.selectAll()
        elif event.key() == QtCore.Qt.Key_S and event.modifiers() == QtCore.Qt.ControlModifier:
            self.show_save_dialog()
        elif event.key() == QtCore.Qt.Key_S and event.modifiers() == (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            self.save_all_txt() # 一括保存
        elif event.key() == QtCore.Qt.Key_W:
            self.add_with_w = True  # 押下中フラグON
            # Wキー押下中にドラッグが開始されていたら、ドラッグ矩形を描画開始
            if self.drag_start is not None and self.mode == "edit":
                 # マウスの位置を取得してドラッグ矩形を更新
                 current_mouse_pos = self.view.mapToScene(self.mapFromGlobal(QtGui.QCursor.pos()))
                 self._update_drag_rect(current_mouse_pos)



    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key_W:
            self.add_with_w = False  # 解放でOFF
        else:
            super().keyReleaseEvent(event)

    def _phase2_move_to_ng_frame(self, direction: int):
        """フェーズ2用：不一致フレーム一覧の中で前/次へ移動（direction: +1=次, -1=前）"""
        if self.phase2_table.rowCount() == 0:
            # 未チェックまたは全一致 → 通常の1フレーム移動にフォールバック
            if direction > 0:
                self.next_frame()
            else:
                self.prev_frame()
            return

        # 一覧のフレーム番号リストを取得
        ng_frame_numbers = []
        for row in range(self.phase2_table.rowCount()):
            item = self.phase2_table.item(row, 0)
            if item:
                try:
                    ng_frame_numbers.append(int(item.text()))
                except ValueError:
                    pass

        if not ng_frame_numbers:
            return

        current_fn = self.image_paths[self.current_frame_index][1] if self.image_paths else None
        if current_fn is None:
            return

        if direction > 0:
            # 現在フレームより大きいNGフレームの最小値
            candidates = [fn for fn in ng_frame_numbers if fn > current_fn]
            target = min(candidates) if candidates else ng_frame_numbers[0]  # 末端なら先頭に戻る
        else:
            # 現在フレームより小さいNGフレームの最大値
            candidates = [fn for fn in ng_frame_numbers if fn < current_fn]
            target = max(candidates) if candidates else ng_frame_numbers[-1]  # 先頭なら末端に戻る

        # ジャンプ
        for i, (_, fn) in enumerate(self.image_paths):
            if fn == target:
                self.current_frame_index = i
                self.load_image()
                # テーブルの対応行をハイライト
                for row in range(self.phase2_table.rowCount()):
                    item = self.phase2_table.item(row, 0)
                    if item and int(item.text()) == target:
                        self.phase2_table.selectRow(row)
                        self.phase2_table.scrollToItem(item)
                        break
                return

    def move_by_frames(self, delta: int):
        """現在位置から delta 分、フレームインデックスで前後移動（自動保存なし）。"""
        if not self.image_paths:
            return
        # 目的地をクランプ
        new_index = max(0, min(self.current_frame_index + delta, len(self.image_paths) - 1))
        if new_index != self.current_frame_index:
            self.current_frame_index = new_index
            self.load_image()


    # -------- フレーム移動（自動保存なし） --------
    def next_frame(self):
        if self.current_frame_index < len(self.image_paths) - 1:
            self.current_frame_index += 1
            self.load_image()

    def prev_frame(self):
        if self.current_frame_index > 0:
            self.current_frame_index -= 1
            self.load_image()

    # -------- 保存形式選択ダイアログ --------
    def show_save_dialog(self):
        """Ctrl+S で呼ばれる保存形式選択ダイアログ"""
        if not self.image_folder:
            QtWidgets.QMessageBox.warning(self, "警告", "画像フォルダが選択されていません。")
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("保存形式を選択")
        dlg.setModal(True)
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("保存形式を選択してください："))
        v.addSpacing(8)
        btn_json = QtWidgets.QPushButton("JSON形式で保存（フレームごと）")
        btn_txt_bulk = QtWidgets.QPushButton("TXT形式で保存（一括ファイル）")
        btn_txt_frame = QtWidgets.QPushButton("TXT形式で保存（フレームごと）")
        btn_cancel = QtWidgets.QPushButton("キャンセル")
        for btn in [btn_json, btn_txt_bulk, btn_txt_frame]:
            btn.setMinimumHeight(36)
        btn_cancel.setMinimumHeight(32)
        v.addWidget(btn_json)
        v.addWidget(btn_txt_bulk)
        v.addWidget(btn_txt_frame)
        v.addSpacing(4)
        v.addWidget(btn_cancel)
        dlg.setMinimumWidth(320)
        btn_json.clicked.connect(lambda: (dlg.accept(), self.save_all_json()))
        btn_txt_bulk.clicked.connect(lambda: (dlg.accept(), self.save_all_txt()))
        btn_txt_frame.clicked.connect(lambda: (dlg.accept(), self.save_all_txt_per_frame()))
        btn_cancel.clicked.connect(dlg.reject)
        dlg.exec_()

    # -------- JSON形式で保存 (フレームごと) --------
    def save_all_json(self):
        """全フレームの検出結果をJSON形式（フレームごと）で保存"""
        if not self.image_folder:
            QtWidgets.QMessageBox.warning(self, "警告", "画像フォルダが選択されていません。")
            return
        
        total = len(self.image_paths)
        # 進捗ダイアログ作成
        progress = QtWidgets.QProgressDialog(
            f"JSON形式で保存中... 0% (0/{total})", "キャンセル", 0, total, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()
        
        saved_count = 0
        for i, (img_path, frame_number) in enumerate(self.image_paths):
            if progress.wasCanceled():
                break
            
            # 遅延ロードされているデータがある場合はそれを保存
            self._load_frame_if_needed(frame_number)
            frame_boxes = self.detections.get(frame_number, [])
            
            # JSON保存（ボックスがない場合はファイルを削除/作成しない）
            self._write_labelme_json(img_path, frame_boxes)
            if frame_boxes:
                saved_count += 1
            
            # 進捗更新
            percent = int(100 * (i + 1) / total)
            progress.setLabelText(f"JSON形式で保存中... {percent}% ({i+1}/{total})")
            progress.setValue(i + 1)
            
            if i % 10 == 0:  # 10ファイルごとにUI更新
                QtWidgets.QApplication.processEvents()
        
        progress.close()
        
        self._save_meta()
        QtWidgets.QMessageBox.information(self, "保存完了", f"JSON形式で{saved_count}ファイル保存しました（全{total}画像）。")

    # -------- TXT形式で保存 (一括ファイル) --------
    def save_all_txt(self):
        """全フレームの検出結果をTXT形式（一括ファイル）で保存"""
        if not self.image_folder:
            QtWidgets.QMessageBox.warning(self, "警告", "画像フォルダが選択されていません。")
            return
        
        # 保存先ファイルを選択
        default_path = os.path.join(self.image_folder, "detections.txt")
        save_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 
            "TXT保存先を選択 (一括ファイル)", 
            default_path,
            "Text files (*.txt);;All files (*)"
        )
        
        if not save_path:
            return
        
        total = len(self.image_paths)
        # 進捗ダイアログ作成
        progress = QtWidgets.QProgressDialog(
            f"TXT形式で保存中... 0% (0/{total})", "キャンセル", 0, total, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()
        
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                # フレーム番号順にソートして出力
                sorted_frames = sorted(self.image_paths, key=lambda x: x[1])
                for i, (_, frame_number) in enumerate(sorted_frames):
                    if progress.wasCanceled():
                        progress.close()
                        QtWidgets.QMessageBox.warning(self, "キャンセル", "保存がキャンセルされました。")
                        return
                    
                    # 遅延ロードされているデータがある場合はそれをロード
                    self._load_frame_if_needed(frame_number)
                    boxes = self.detections.get(frame_number, [])
                    
                    for x, y, w, h, label in boxes:
                        # フォーマット: frame_number,label,x,y,w,h
                        f.write(f"{frame_number},{label},{x},{y},{w},{h}\n")
                    
                    # 進捗更新
                    percent = int(100 * (i + 1) / total)
                    progress.setLabelText(f"TXT形式で保存中... {percent}% ({i+1}/{total})")
                    progress.setValue(i + 1)
                    
                    if i % 10 == 0:  # 10フレームごとにUI更新
                        QtWidgets.QApplication.processEvents()
            
            progress.close()
            QtWidgets.QMessageBox.information(self, "保存完了", f"TXT形式（一括ファイル）で保存しました:\n{save_path}")
        except Exception as e:
            progress.close()
            QtWidgets.QMessageBox.critical(self, "保存エラー", f"保存に失敗しました:\n{str(e)}")

    # -------- TXT形式で保存 (フレームごと) --------
    def save_all_txt_per_frame(self):
        """全フレームの検出結果をTXT形式（フレームごと）で保存"""
        if not self.image_folder:
            QtWidgets.QMessageBox.warning(self, "警告", "画像フォルダが選択されていません。")
            return
        
        # 保存先フォルダを選択
        save_folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "TXT保存先フォルダを選択 (フレームごと)", self.image_folder
        )
        
        if not save_folder:
            return
            
        total = len(self.image_paths)
        # 進捗ダイアログ作成
        progress = QtWidgets.QProgressDialog(
            f"TXT形式 (フレームごと) で保存中... 0% (0/{total})", "キャンセル", 0, total, self
        )
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QtWidgets.QApplication.processEvents()
        
        saved_count = 0
        for i, (img_path, frame_number) in enumerate(self.image_paths):
            if progress.wasCanceled():
                progress.close()
                QtWidgets.QMessageBox.warning(self, "キャンセル", "保存がキャンセルされました。")
                return
            
            # 遅延ロードされているデータがある場合はそれをロード
            self._load_frame_if_needed(frame_number)
            boxes = self.detections.get(frame_number, [])
            
            img_basename = os.path.basename(img_path)
            base_name_no_ext = os.path.splitext(img_basename)[0]
            txt_path = os.path.join(save_folder, base_name_no_ext + ".txt")
            
            # ボックスがない場合はファイルを作成しない/削除する
            if not boxes:
                if os.path.exists(txt_path):
                    try:
                        os.remove(txt_path)
                    except OSError:
                        pass
                continue # 次のフレームへ
                
            try:
                with open(txt_path, "w", encoding="utf-8") as f:
                    for x, y, w, h, label in boxes:
                        # 形式: x1 y1 x2 y2 label confidence
                        # x, y, w, h から x1, y1, x2, y2 に変換
                        x1 = x
                        y1 = y
                        x2 = x + w
                        y2 = y + h
                        # confidence は常に 1.000 として出力（編集後のデータのため）
                        confidence = 1.000
                        f.write(f"{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f} {label} {confidence:.3f}\n")
                saved_count += 1
            except Exception as e:
                # 個別ファイルの書き込みエラーは警告してスキップ
                print(f"Warning: Failed to write {txt_path}: {str(e)}")
            
            # 進捗更新
            percent = int(100 * (i + 1) / total)
            progress.setLabelText(f"TXT形式 (フレームごと) で保存中... {percent}% ({i+1}/{total})")
            progress.setValue(i + 1)
            
            if i % 10 == 0:  # 10フレームごとにUI更新
                QtWidgets.QApplication.processEvents()
        
        progress.close()
        QtWidgets.QMessageBox.information(
            self, 
            "保存完了", 
            f"TXT形式（フレームごと）で{saved_count}ファイル保存しました。:\n{save_folder}"
        )

    # ================================================================
    # CSV 読み込み (frame, id, x1, y1, x2, y2)
    # ================================================================
    def open_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "CSVファイルを選択", filter="CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        try:
            self.load_tracks_csv(path)
            QtWidgets.QMessageBox.information(self, "読み込み完了", f"CSVを読み込みました:\n{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "CSVエラー", str(e))

    def load_tracks_csv(self, path: str):
        """CSV (frame, id, x1, y1, x2, y2) を読み込み self.detections へ反映"""
        import csv as _csv
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            required = {"frame", "id", "x1", "y1", "x2", "y2"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSVヘッダー不足: {missing}")
            rows = list(reader)

        if not rows:
            raise ValueError("CSVにデータがありません。")

        self.detections.clear()
        ids_seen: set = set()
        for r in rows:
            try:
                fno  = int(r["frame"])
                sid  = str(r["id"])
                x1   = float(r["x1"]); y1 = float(r["y1"])
                x2   = float(r["x2"]); y2 = float(r["y2"])
                if x2 < x1: x1, x2 = x2, x1
                if y2 < y1: y1, y2 = y2, y1
                x = x1; y = y1; w = x2 - x1; h = y2 - y1
            except Exception:
                continue
            self.detections.setdefault(fno, []).append([x, y, w, h, sid])
            ids_seen.add(sid)

        # id_list を同期
        sorted_ids = sorted(ids_seen, key=lambda v: (int(v),) if v.isdigit() else (10**9, v))
        self.id_list = sorted_ids if sorted_ids else self.id_list
        self.rebuild_id_list_ui()

    # ================================================================
    # LabelMe JSON フォルダ読み込み
    # ================================================================
    def open_labelme_json_folder(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "LabelMe JSONフォルダを選択"
        )
        if not directory:
            return
        try:
            self.load_labelme_jsons_folder(directory)
            QtWidgets.QMessageBox.information(self, "読み込み完了", f"LabelMe JSONを読み込みました:\n{directory}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "JSONエラー", str(e))

    def load_labelme_jsons_folder(self, directory: str):
        """LabelMe JSONフォルダを読み込み self.detections へ反映"""
        json_files = sorted(
            [p for p in glob.glob(os.path.join(directory, "*.json"))],
            key=lambda s: [int(t) if t.isdigit() else t.lower()
                           for t in re.split(r'(\d+)', os.path.basename(s))]
        )
        if not json_files:
            raise ValueError("選択フォルダに JSON ファイルが見つかりません。")

        # image_paths からファイル名 → フレーム番号のマップを作成
        name_to_frame: Dict[str, int] = {}
        for img_path, fno in self.image_paths:
            bn = os.path.basename(img_path)
            name_to_frame[bn] = fno
            name_to_frame[os.path.splitext(bn)[0]] = fno

        self.detections.clear()
        ids_seen: set = set()
        dropped = 0
        for jp in json_files:
            with open(jp, "r", encoding="utf-8") as f:
                data = json.load(f)

            img_name = str(data.get("imagePath") or "")
            fno: Optional[int] = name_to_frame.get(img_name) or name_to_frame.get(
                os.path.splitext(img_name)[0]
            )
            if fno is None:
                m = re.search(r'(\d+)', os.path.basename(jp))
                fno = int(m.group(1)) if m else None
            if fno is None:
                continue

            for sh in data.get("shapes", []):
                if str(sh.get("shape_type")) != "rectangle":
                    continue
                sid = str(sh.get("label", "")).strip()
                pts = sh.get("points", [])
                if not sid or len(pts) < 2:
                    dropped += 1
                    continue
                try:
                    (x1, y1), (x2, y2) = pts[0], pts[1]
                    x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
                    if x2 < x1: x1, x2 = x2, x1
                    if y2 < y1: y1, y2 = y2, y1
                    x, y, w, h = x1, y1, x2 - x1, y2 - y1
                except Exception:
                    dropped += 1
                    continue
                self.detections.setdefault(fno, []).append([x, y, w, h, sid])
                ids_seen.add(sid)

        sorted_ids = sorted(ids_seen, key=lambda v: (int(v),) if v.isdigit() else (10**9, v))
        if sorted_ids:
            self.id_list = sorted_ids
        self.rebuild_id_list_ui()
        if dropped:
            QtWidgets.QMessageBox.information(self, "読み込み完了",
                                              f"JSON読み込み: {dropped}件の矩形をスキップしました。")

    # ================================================================
    # CSV 保存 (frame, id, x1, y1, x2, y2)
    # ================================================================
    def save_as_csv(self):
        if not self.detections:
            QtWidgets.QMessageBox.warning(self, "データなし", "保存できる検出データがありません。")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "CSVで保存", filter="CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            self._export_csv_to(path)
            QtWidgets.QMessageBox.information(self, "保存完了", f"CSVを保存しました:\n{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存エラー", str(e))

    def _export_csv_to(self, path: str):
        """self.detections を CSV (frame,id,x1,y1,x2,y2) で書き出す"""
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow(["frame", "id", "x1", "y1", "x2", "y2"])
            for fno in sorted(self.detections.keys()):
                for item in self.detections[fno]:
                    x, y, w, h, sid = item[0], item[1], item[2], item[3], str(item[4])
                    writer.writerow([fno, sid, x, y, x + w, y + h])

    # ================================================================
    # LabelMe JSON フォルダへ保存
    # ================================================================
    def save_as_labelme_json(self):
        if not self.detections:
            QtWidgets.QMessageBox.warning(self, "データなし", "保存できる検出データがありません。")
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "LabelMe JSON保存先フォルダを選択")
        if not directory:
            return
        try:
            self._export_labelme_to(directory)
            QtWidgets.QMessageBox.information(self, "保存完了", f"LabelMe JSONを保存しました:\n{directory}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存エラー", str(e))

    def _export_labelme_to(self, directory: str):
        """self.detections を LabelMe JSON フォルダへ書き出す"""
        os.makedirs(directory, exist_ok=True)
        frame_to_img: Dict[int, str] = {}
        for img_path, fno in self.image_paths:
            frame_to_img[fno] = img_path

        for fno in sorted(self.detections.keys()):
            items = self.detections.get(fno, [])
            img_path = frame_to_img.get(fno)
            img_name = os.path.basename(img_path) if img_path else f"{fno:06d}.png"

            shapes = []
            for item in items:
                x, y, w, h, sid = item[0], item[1], item[2], item[3], str(item[4])
                x1, y1, x2, y2 = float(x), float(y), float(x) + float(w), float(y) + float(h)
                shapes.append({
                    "label": sid,
                    "line_color": None,
                    "fill_color": None,
                    "points": [[x1, y1], [x2, y2]],
                    "shape_type": "rectangle",
                    "flags": {},
                })

            data = {
                "version": "5.0.1",
                "flags": {},
                "shapes": shapes,
                "imagePath": img_name,
                "imageData": None,
                "imageHeight": None,
                "imageWidth": None,
            }
            out_name = os.path.splitext(img_name)[0] + ".json"
            with open(os.path.join(directory, out_name), "w", encoding="utf-8") as jf:
                json.dump(data, jf, ensure_ascii=False, indent=2)

    def _export_txt_direct(self, path: str):
        """self.detections を TXT一括ファイル (frame,id,x,y,w,h) へ書き出す（ダイアログなし）"""
        with open(path, "w", encoding="utf-8") as f:
            for fno in sorted(self.detections.keys()):
                for item in self.detections.get(fno, []):
                    x, y, w, h, sid = item[0], item[1], item[2], item[3], str(item[4])
                    f.write(f"{fno},{sid},{x},{y},{w},{h}\n")

    # ================================================================
    # 一括エクスポート (CSV + TXT + JSON)
    # ================================================================
    def export_all_formats(self):
        if not self.detections:
            QtWidgets.QMessageBox.warning(self, "データなし", "エクスポートできる検出データがありません。")
            return
        base_dir = QtWidgets.QFileDialog.getExistingDirectory(self, "エクスポート先フォルダを選択")
        if not base_dir:
            return
        import hashlib, time
        ts = hashlib.md5(str(time.time()).encode()).hexdigest()[:6]
        out_dir = os.path.join(base_dir, f"tracks_export_{ts}")
        os.makedirs(out_dir, exist_ok=True)
        try:
            self._export_csv_to(os.path.join(out_dir, "tracks.csv"))
            self._export_txt_direct(os.path.join(out_dir, "tracks.txt"))
            json_dir = os.path.join(out_dir, "labelme_json")
            os.makedirs(json_dir, exist_ok=True)
            self._export_labelme_to(json_dir)
            QtWidgets.QMessageBox.information(
                self, "エクスポート完了",
                f"CSV・TXT・JSONを一括保存しました:\n{out_dir}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "エクスポートエラー", str(e))

    # ================================================================
    # 起動時ダイアログ（画像フォルダ読み込みを促す）
    # ================================================================
    def _prompt_initial_image_load(self):
        msg = QtWidgets.QMessageBox(self)
        msg.setIcon(QtWidgets.QMessageBox.Question)
        msg.setWindowTitle("初期読み込み")
        msg.setText("画像フォルダを読み込みますか？")
        msg.setInformativeText("開始するには、まず画像フォルダを選択してください。")
        btn_yes = msg.addButton("Yes", QtWidgets.QMessageBox.AcceptRole)
        btn_no  = msg.addButton("No",  QtWidgets.QMessageBox.RejectRole)
        btn_yes.setStyleSheet(
            "QPushButton{background-color:#6c6c6c;color:white;border-radius:5px;padding:4px 16px;}"
            "QPushButton:hover{background-color:#555;}"
        )
        btn_no.setStyleSheet(
            "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 16px;}"
            "QPushButton:hover{background-color:#0062d4;}"
        )
        msg.setDefaultButton(btn_no)
        msg.exec_()
        if msg.clickedButton() == btn_yes:
            self.load_images()

    # ================================================================
    # 画像読み込み後ダイアログ（追跡データ読み込みを促す）
    # ================================================================
    def _prompt_tracking_load(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("追跡データの読み込み")
        v = QtWidgets.QVBoxLayout(dlg)
        info = QtWidgets.QLabel(
            "追跡データを読み込みますか？\n\n"
            "  CSV          : frame, id, x1, y1, x2, y2 形式\n"
            "  TXT(一括)    : frame,id,x,y,w,h 形式の1ファイル\n"
            "  TXT(フォルダ): フレームごとのtxtをフォルダから読み込み\n"
            "  JSON         : LabelMe形式フォルダ\n"
            "  スキップ     : あとでメニューから読み込めます"
        )
        info.setWordWrap(True)
        v.addWidget(info)
        v.addSpacing(8)

        btn_row = QtWidgets.QHBoxLayout()
        _action_style = (
            "QPushButton{background-color:#6c6c6c;color:white;border-radius:5px;padding:4px 10px;}"
            "QPushButton:hover{background-color:#555;}"
        )
        _skip_style = (
            "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 10px;}"
            "QPushButton:hover{background-color:#0062d4;}"
        )
        btn_csv      = QtWidgets.QPushButton("CSV");           btn_csv.setStyleSheet(_action_style)
        btn_txt_bulk = QtWidgets.QPushButton("TXT\n(一括)");   btn_txt_bulk.setStyleSheet(_action_style)
        btn_txt_dir  = QtWidgets.QPushButton("TXT\n(フォルダ)"); btn_txt_dir.setStyleSheet(_action_style)
        btn_json     = QtWidgets.QPushButton("JSON");          btn_json.setStyleSheet(_action_style)
        btn_skip     = QtWidgets.QPushButton("スキップ");      btn_skip.setStyleSheet(_skip_style)
        for b in (btn_csv, btn_txt_bulk, btn_txt_dir, btn_json, btn_skip):
            btn_row.addWidget(b)
        v.addLayout(btn_row)

        result = [None]
        btn_csv.clicked.connect(lambda:      [result.__setitem__(0, "csv"),      dlg.accept()])
        btn_txt_bulk.clicked.connect(lambda: [result.__setitem__(0, "txt_bulk"), dlg.accept()])
        btn_txt_dir.clicked.connect(lambda:  [result.__setitem__(0, "txt_dir"),  dlg.accept()])
        btn_json.clicked.connect(lambda:     [result.__setitem__(0, "json"),     dlg.accept()])
        btn_skip.clicked.connect(dlg.reject)
        dlg.exec_()

        if result[0] == "csv":
            self.open_csv()
        elif result[0] == "txt_bulk":
            self.load_detections_from_txt(is_folder=False)
        elif result[0] == "txt_dir":
            self.load_detections_from_txt(is_folder=True)
        elif result[0] == "json":
            self.open_labelme_json_folder()

    # ================================================================
    # FPS設定ダイアログ
    # ================================================================
    def _open_fps_settings(self):
        dlg = FPSSettingsDialog(self, self.original_fps)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            new_fps = dlg.get_fps()
            self.original_fps = new_fps
            # 再生中であれば速度を再適用
            if self.is_playing:
                interval = int(1000 / max(1, self.original_fps * self.playback_speed))
                self.play_timer.setInterval(interval)

    # ================================================================
    # レイアウト調整ダイアログ
    # ================================================================
    def _open_layout_adjuster(self):
        dlg = LayoutAdjusterDialog(self, self.content_splitter)
        dlg.exec_()

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

    # ベースフォントはポイント指定（DPIに追随）
    base_font = QtGui.QFont("Noto Sans JP", 10)
    app.setFont(base_font)

    editor = DetectionEditor()
    app.exec_()