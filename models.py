import re
import hashlib
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor


_PHASE3_AVAILABLE = True

FrameNumber = int
BoxList = List[object]  # [x, y, w, h, label(str)]  ← 型エイリアス（dataclass Box と区別）
Interval = List[Optional[int]]  # [start_frame_number, end_frame_number_or_None]


def natural_sort_key(s):
    """ID文字列を自然順（"2" < "10"、"GK1" < "GK10"）で並べるためのソートキー。
    id_listの並び順を常にこのキーで統一することで、サイドバー・追跡フェーズなど
    どこで表示してもID順がバラつかないようにする。"""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]


def color_index_for_id(id_value: str, palette_len: int, used_indices) -> int:
    """指定IDに割り当てるパレット色番号を決める。
    未使用の最小インデックスがあればそれを、無ければIDのハッシュ値から
    決定的に選ぶ（同じIDなら常に同じ色になる）。
    検出フェーズ・追跡フェーズの両方でこの関数を使うことで、色の割り当て
    ロジックを一本化し、同じIDが異なる色で表示されないようにする。"""
    free_idx = next((i for i in range(palette_len) if i not in used_indices), None)
    if free_idx is not None:
        return free_idx
    md5 = hashlib.md5(str(id_value).encode("utf-8")).hexdigest()
    return int(md5, 16) % palette_len


# =====================================================================
# Phase3 データクラス群
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




# =====================================================================
# ID追跡ズーム共通定数・ユーティリティ（Phase1/3共用）
# =====================================================================
ZOOM_PRESETS     = ["1x", "1.5x", "2x", "3x", "4x", "5x", "7x", "10x", "15x", "20x"]
ZOOM_DEFAULT_TEXT = "5x"


def zoom_combo_value(combo) -> float:
    """ID追跡ズームコンボボックスから倍率(float)を取得"""
    try:
        return float(combo.currentText().replace("x", "").strip())
    except (ValueError, AttributeError):
        return 5.0


def apply_tracking_zoom(view, zoom_factor: float, min_scale: float,
                        cx: float, cy: float) -> None:
    """ID追跡ズームを view に適用する（Phase1/3共通）"""
    target_scale = min_scale * zoom_factor if min_scale > 0 else zoom_factor
    if abs(view.transform().m11() - target_scale) > 0.01:
        view.resetTransform()
        view.scale(target_scale, target_scale)
    view.centerOn(cx, cy)


# =====================================================================
# 画像ビュー共通ホイールイベント処理
# =====================================================================
def handle_view_wheel(event, view, zoom_fn, min_scale: float = 0.0) -> bool:
    """
    ホイールイベントをマウス/トラックパッドで振り分ける共通処理（Phase1/3共用）。
    - 物理マウス (phase=NoScrollPhase): zoom_fn(factor) でズーム
    - トラックパッド (phase が Begin/Update 等): view をパン
    Returns True（常にイベントを消費）。
    """
    is_trackpad = event.phase() not in (Qt.NoScrollPhase, Qt.ScrollEnd)
    if not is_trackpad:
        angle_y = event.angleDelta().y()
        if angle_y != 0:
            factor = 1.15 if angle_y > 0 else 1.0 / 1.15
            if min_scale > 0 and factor < 1.0:
                current_scale = view.transform().m11()
                if current_scale * factor < min_scale:
                    factor = min_scale / current_scale
            zoom_fn(factor)
    else:
        pixel = event.pixelDelta()
        hbar = view.horizontalScrollBar()
        vbar = view.verticalScrollBar()
        if pixel.manhattanLength() > 0:
            hbar.setValue(hbar.value() - pixel.x())
            vbar.setValue(vbar.value() - pixel.y())
        else:
            hbar.setValue(hbar.value() - event.angleDelta().x() // 8)
            vbar.setValue(vbar.value() - event.angleDelta().y() // 8)
    return True


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
        self.id_list: List[str] = []
        self.image_paths: List[Tuple] = []
        self.image_folder: str = ""
        self.shared_frame: int = 0  # フェーズ間で共有するフレーム番号（原フレーム番号）
        self.id_color_map: Dict[str, int] = {}  # ID→パレット色番号（検出・追跡フェーズで共有し、同じIDは同じ色にする）
