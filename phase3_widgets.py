import copy
import time
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtCore import QRectF, QLineF, QPointF, QTimer, pyqtSignal, Qt
from PyQt5.QtGui import QColor, QPainter, QPen, QFont, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
    QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSpinBox,
    QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)
from models import (Box, Lane, Span, EditorStore, _PALETTE, _color_for_id,
                    ZOOM_PRESETS, ZOOM_DEFAULT_TEXT, zoom_combo_value, apply_tracking_zoom)


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
        self._pixmap_cache: Dict[str, QPixmap] = {}  # パスをキーにしたPixmapキャッシュ
        self._max_cache_size = 30  # キャッシュ上限（枚数）
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

        # キャッシュから取得、なければディスクから読んでキャッシュに保存
        if path in self._pixmap_cache:
            px = self._pixmap_cache[path]
        else:
            px = QPixmap(path)
            if px.isNull():
                return
            if len(self._pixmap_cache) >= self._max_cache_size:
                first_key = next(iter(self._pixmap_cache))
                del self._pixmap_cache[first_key]
            self._pixmap_cache[path] = px

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
                    if not self._playing:  # 再生中はラベル描画をスキップ
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
            if not self._playing:  # 再生中はラベル描画をスキップ
                label = self.lane_labels.get(bx.lane_index, str(bx.lane_index))
                self._draw_id_tag(sc, x1, y1, str(label), color)

    def _draw_id_tag(self, sc, x: float, y: float, label: str, color: QColor):
        """ボックスの左上角にIDテキストを描画してリストに追加する（Phase1スタイル）。"""
        ti = sc.addText(label)
        ti.setDefaultTextColor(color)
        ti.setPos(x, y - 20)
        self._label_items.append(ti)

    def _zoom_at(self, view_pos, factor: float):
        old_pos = self.mapToScene(view_pos)
        self.scale(factor, factor)
        new_pos = self.mapToScene(view_pos)
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def wheelEvent(self, e):
        from models import handle_view_wheel
        handle_view_wheel(e, self,
                          lambda f: self._zoom_at(e.pos(), f),
                          self._min_zoom_scale)
        e.accept()

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
    scrubToFrame      = pyqtSignal(int)
    swapRequested     = pyqtSignal(int, int, int, int)

    def __init__(self, total_frames: int, fps: float, lanes: List[Lane], parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.total_frames = max(1, total_frames)
        self.fps = fps
        self.lanes: List[Lane] = lanes
        self.current_frame = 0

        self.row_height = 26
        self.margin_top = 30
        self.margin_left = 80
        self.visible_rows = 12
        self.max_visible_frames = 200
        self.pixels_per_frame = 8.0
        self._user_ppf: Optional[float] = None  # ユーザーによるズーム上書き値

        self._occlusions: set = set()
        self.cut_mode = False
        self.cut_to_end_mode = False
        self.sel1: Optional[tuple] = None
        self._sel_anchor_lane: Optional[int] = None
        self._sel_anchor_frame: Optional[int] = None
        self.hover_lane: Optional[int] = None
        self.dragging_sel = False

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
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
        self._user_ppf = None  # 新データロード時はズームリセット
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
        if self._user_ppf is not None:
            self.pixels_per_frame = self._user_ppf
        else:
            self.pixels_per_frame = timeline_width / frames_in_view

        scene_height = self.margin_top + len(self.lanes) * self.row_height + 4
        total_width  = self.margin_left + self.total_frames * self.pixels_per_frame + 4
        self.setSceneRect(QRectF(0, 0, total_width, scene_height))

        visible = max(1, min(self.visible_rows, len(self.lanes)))
        view_height = self.margin_top + visible * self.row_height + 4
        self.setMaximumHeight(int(view_height))
        self.setMinimumHeight(120)

        self.scene().update()

    def wheelEvent(self, e):
        mods = e.modifiers()
        delta = e.angleDelta().y()
        if mods & Qt.ControlModifier:
            # Ctrl+スクロール → ズーム（幅変更）
            if delta != 0:
                factor = 1.15 if delta > 0 else (1.0 / 1.15)
                self._zoom_timeline(factor, e.pos().x())
        elif mods & Qt.ShiftModifier:
            # Shift+スクロール → 横スクロール
            step = e.pixelDelta().y() if e.pixelDelta().manhattanLength() > 0 else delta
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - step)
        else:
            # スクロールのみ → 縦スクロール（ID行を見る）
            pixel = e.pixelDelta()
            if pixel.manhattanLength() > 0:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - pixel.y())
            else:
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - delta)
        e.accept()

    def _zoom_timeline(self, factor: float, viewport_x: int):
        """タイムラインのズーム（カーソル位置を固定して拡縮）"""
        scene_x = self.mapToScene(viewport_x, 0).x()
        frame_at_cursor = self.x_to_frame(scene_x)

        new_ppf = max(1.0, min(80.0, self.pixels_per_frame * factor))
        self._user_ppf = new_ppf
        self.rebuild()

        # カーソル下のフレームが同じビューポート位置に来るようにスクロール調整
        new_frame_x = self.frame_to_x(frame_at_cursor)
        target_scroll = int(new_frame_x - viewport_x)
        self.horizontalScrollBar().setValue(max(0, target_scroll))

    def shift_window_by_frames(self, delta_frames: int):
        start, _ = self.visible_frame_range()
        self._scroll_to_frame_left(start + delta_frames)

    def _scroll_to_frame_left(self, start_frame: int):
        x_left = self.margin_left + start_frame * self.pixels_per_frame
        self.horizontalScrollBar().setValue(int(x_left))

    def _ensure_center_on_playhead(self):
        if self.total_frames <= self.max_visible_frames:
            self.horizontalScrollBar().setValue(0)
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

        # アンカー表示（1回目クリック済み、2回目待ち）
        if self.cut_mode and self._sel_anchor_lane is not None and self._sel_anchor_frame is not None and not self.sel1:
            ay = self.margin_top + self._sel_anchor_lane * self.row_height
            ax = self.frame_to_x(self._sel_anchor_frame)
            painter.setPen(QPen(QColor(255, 200, 0), 2, Qt.DashLine))
            painter.drawLine(QLineF(ax, ay, ax, ay + self.row_height))

    def drawForeground(self, painter, rect):
        """ルーラー帯＋左固定カラム（IDラベル）を常にビューポート座標で描画"""
        painter.save()
        painter.resetTransform()

        col_w  = self.margin_left
        band_h = self.margin_top
        vw = self.viewport().width()
        vh = self.viewport().height()

        # ─── 背景
        painter.fillRect(0, 0, col_w, vh, QColor(45, 49, 54))               # 左カラム
        painter.fillRect(col_w, 0, vw - col_w, band_h, QColor(55, 60, 65))  # ルーラー帯

        # ─── 境界線
        painter.setPen(QPen(QColor(80, 84, 90), 1))
        painter.drawLine(col_w, 0, col_w, vh)      # 左カラム右端（縦）
        painter.drawLine(0, band_h, vw, band_h)    # ルーラー下端（横）

        # ─── "ID / Frame" ヘッダー
        font_hdr = QFont()
        font_hdr.setPointSize(10)
        font_hdr.setBold(True)
        painter.setFont(font_hdr)
        painter.setPen(QColor(230, 230, 230))
        painter.drawText(QRectF(0, 0, col_w, band_h), Qt.AlignCenter, "ID / Frame")

        # ─── ルーラー目盛り
        ppf = max(self.pixels_per_frame, 0.1)
        _nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
        major_interval = next((n for n in _nice if n * ppf >= 50), _nice[-1])
        minor_pen = QPen(QColor(90, 96, 100));   minor_pen.setWidthF(1)
        major_pen = QPen(QColor(200, 200, 200)); major_pen.setWidthF(1)
        font_lbl = QFont(); font_lbl.setPointSize(9)
        painter.setFont(font_lbl)

        start_frame, end_frame = self.visible_frame_range()
        for f in range(start_frame, end_frame + 1):
            vx = self.mapFromScene(self.frame_to_x(f), 0).x()
            if vx < col_w or vx > vw:
                continue
            if f % major_interval == 0:
                painter.setPen(major_pen)
                painter.drawLine(int(vx), band_h - 16, int(vx), band_h)
                painter.setPen(QColor(200, 200, 200))
                painter.drawText(int(vx + 2), band_h - 17, str(f))
            else:
                painter.setPen(minor_pen)
                painter.drawLine(int(vx), band_h - 8, int(vx), band_h)

        # ─── プレイヘッド（ルーラー帯内）
        px_vp = self.mapFromScene(self.frame_to_x(self.current_frame), 0).x()
        painter.setPen(QPen(QColor(255, 60, 60), 2))
        painter.drawLine(int(px_vp), 0, int(px_vp), band_h)

        # ─── 各レーンのIDラベル（ルーラー帯より下にクリップして被りを防ぐ）
        painter.setClipRect(QRectF(0, band_h, col_w, vh - band_h))
        painter.setPen(QColor(200, 200, 200))
        font_id = QFont(); font_id.setPointSize(11)
        painter.setFont(font_id)
        for i, lane in enumerate(self.lanes):
            y_scene = self.margin_top + i * self.row_height
            y_vp    = self.mapFromScene(0, y_scene).y()
            if y_vp + self.row_height < band_h or y_vp > vh:
                continue
            painter.drawText(QRectF(2, y_vp, col_w - 4, self.row_height),
                             Qt.AlignVCenter | Qt.AlignLeft, f"  ID {lane.id_value}")
        painter.setClipping(False)

        painter.restore()

    def scrollContentsBy(self, dx, dy):
        """スクロール時に左固定カラム（drawForeground）を必ず再描画"""
        super().scrollContentsBy(dx, dy)
        self.viewport().update()

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
                # Cancel(左・青) / Yes(右・灰)
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
        self.status = QLabel("フレーム：0 / 0")
        status_font = QFont("arial", 13)
        self.status.setFont(status_font)
        row.addWidget(self.status)

        self.jumpEdit = QLineEdit()
        self.jumpEdit.setFixedWidth(80)
        self.jumpEdit.setFixedHeight(26)
        self.jumpEdit.setPlaceholderText("フレーム番号")
        self.jumpBtn = QPushButton("➤ ジャンプ")
        self.jumpBtn.setFixedWidth(65)
        self.jumpBtn.setFixedHeight(26)
        row.addWidget(self.jumpEdit)
        row.addWidget(self.jumpBtn)

        row.addSpacing(20)

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
        self.speedCombo.setCurrentText("2x")
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
        self.zoomCombo = QComboBox()
        self.zoomCombo.addItems(ZOOM_PRESETS)
        self.zoomCombo.setCurrentText(ZOOM_DEFAULT_TEXT)
        self.zoomCombo.setFixedWidth(66)
        self.zoomCombo.setVisible(False)
        row.addWidget(self.zoomCombo)

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
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([540, 320])
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
        self.ctrl.zoomCombo.currentIndexChanged.connect(
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

        # ズーム倍率を適用（共通ユーティリティ）
        apply_tracking_zoom(self.mainView,
                            zoom_combo_value(self.ctrl.zoomCombo),
                            self.mainView._min_zoom_scale, cx, cy)

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
        self.ctrl.jumpEdit.clearFocus()

    def _update_status(self):
        self.ctrl.status.setText(
            f"フレーム：{self.current_frame + 1} / {self.total_frames}"
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
        self.ctrl.zoomCombo.setVisible(checked)
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

    def _on_swap(self, src_lane: int, dst_lane: int, s: int, e_: int):
        self._push_undo("swap")
        self._swap_ranges(src_lane, dst_lane, s, e_)
        self._rebuild_lanes_from_boxes()
        # _render_boxes が store.detections を参照するため、描画前にストアへ反映する
        self._sync_to_store()
        self.boxes_by_frame = dict(self._boxes_by_frame_all)
        self.mainView.boxes_by_frame = self.boxes_by_frame
        self.mainView._render_boxes()
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
        self.boxes_by_frame = dict(self._boxes_by_frame_all)
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


