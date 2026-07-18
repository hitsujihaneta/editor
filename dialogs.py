from typing import List, Optional
from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from models import Interval

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


