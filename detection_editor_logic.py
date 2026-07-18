import copy
import os
import re
import hashlib
import time
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPixmap, QPainter, QPen, QFont
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)
from dialogs import IntervalEditor, FPSSettingsDialog, LayoutAdjusterDialog
from models import _HEX_PALETTE, Interval, _PHASE3_AVAILABLE, handle_view_wheel, \
    zoom_combo_value, apply_tracking_zoom


class CoreLogicMixin:
    """DetectionEditorのコアロジック関連メソッド群"""

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

    def _current_screen(self):
        """ウィンドウが現在表示されている（表示されようとしている）モニターを返す"""
        screen = self.screen() if hasattr(self, 'screen') else None
        if not screen:
            wh = self.windowHandle()
            screen = wh.screen() if wh else None
        if not screen:
            screen = QtWidgets.QApplication.primaryScreen()
        return screen

    def _apply_screen_aware_window_size(self):
        """ウィンドウサイズを、現在使用中のモニターの解像度に応じて決める。
        ノートPCのような小さい画面では既定サイズ(700x700)を維持し、
        4Kモニターのような大画面では縦横に余裕を持たせて、
        ラベルチェックパネル表示時にID一覧欄が圧迫されないようにする。"""
        default_w, default_h = 700, 700
        screen = self._current_screen()
        if not screen:
            self.resize(default_w, default_h)
            return
        avail = screen.availableGeometry()
        target_w = min(max(default_w, int(avail.width() * 0.85)), avail.width())
        target_h = min(max(default_h, int(avail.height() * 0.85)), avail.height())
        self.resize(target_w, target_h)

    def _on_screen_changed(self, screen):
        """ウィンドウが別のモニターに移動した際、そのモニターの解像度に合わせてサイズを再調整する。
        （最大化・全画面中はQtが自動でフィットさせるため何もしない）"""
        if self.isMaximized() or self.isFullScreen():
            return
        self._apply_screen_aware_window_size()

    def _ask_confirm(self, title: str, text: str, yes_text: str = "はい",
                      no_text: str = "いいえ", yes_default: bool = False) -> bool:
        """Yes/No確認ダイアログを表示する。
        QMessageBoxの標準Yes/Noはボタンの役割(Role)を元にOSごとに左右の並びが
        自動で入れ替わる（Windows/macOSで逆順になる）ため、それを避けて
        常に左=否定側・右=肯定側で統一した自前レイアウトのダイアログを使う。
        Yesが選ばれればTrueを返す。"""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        v = QtWidgets.QVBoxLayout(dlg)
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        v.addSpacing(10)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        btn_no = QtWidgets.QPushButton(no_text)
        btn_yes = QtWidgets.QPushButton(yes_text)
        btn_no.setMinimumWidth(80)
        btn_yes.setMinimumWidth(80)
        btn_row.addWidget(btn_no)
        btn_row.addWidget(btn_yes)
        v.addLayout(btn_row)

        result = {"yes": False}
        btn_no.clicked.connect(lambda: (result.__setitem__("yes", False), dlg.accept()))
        btn_yes.clicked.connect(lambda: (result.__setitem__("yes", True), dlg.accept()))
        if yes_default:
            btn_yes.setDefault(True)
        else:
            btn_no.setDefault(True)
        dlg.exec_()
        return result["yes"]

    def _play_next_frame(self):
        """再生時に次のフレームに進む"""
        last = len(self.image_paths) - 1

        if self.playback_speed >= 3.0:
            # 3倍速以上：パターンで間引き
            # 3倍速: 1個表示・2個スキップ → 1,4,7,10...
            # 5倍速: 2個表示・3個スキップ → 1,2,5,6,10,11...
            step = self._frame_skip_step(self.current_frame_index)
        else:
            step = 1

        self.current_frame_index = min(self.current_frame_index + step, last)
        self._load_image_fast()

        if self.current_frame_index >= last:
            self.toggle_play_pause()

    def _frame_skip_step(self, current: int) -> int:
        """5倍速時の次フレームへの増分を返す（2表示・3スキップのパターン）
        例）speed=5, show=2 のとき
          pos=0 → +1（frame 0→1）
          pos=1 → +4（frame 1→5）  ← グループ末尾からジャンプ
          pos=2,3,4 → 途中から入った場合の補正（次グループ先頭へ）
        """
        speed = int(self.playback_speed)   # 5
        show  = max(1, round(speed * 1 / 5))  # 5倍速 → 1, 3倍速 → 1
        pos   = current % speed
        if pos < show - 1:
            return 1              # 表示ゾーン内 → 次も表示
        elif pos == show - 1:
            return speed - pos    # 表示ゾーン末尾 → 次グループ先頭へジャンプ
        else:
            return speed - pos    # スキップゾーン（途中開始の補正）
    
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

        # hover_item を先にシーンから除去してから参照を破棄
        if self.hover_item is not None and self.hover_item.scene() == self.scene:
            self.scene.removeItem(self.hover_item)
        # 管理リストから直接削除（scene.items()スキャン不要）
        for item in self._bbox_items:
            self.scene.removeItem(item)
        self._bbox_items.clear()

        self.hover_item = None
        self.hover_box_index = None
        self.drag_rect = None

        # PixmapItemはキャッシュ済みの参照を使う
        if self._play_pixmap_item is not None:
            self._play_pixmap_item.setPixmap(pixmap)
        else:
            self._play_pixmap_item = self.scene.addPixmap(pixmap)
            self.scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())

        # 既存の検出ボックスを描画（テキストなし - 高速化のため）
        frame_boxes = self.detections.get(frame_number, [])

        # フィルター適用（全ID表示がOFFの場合）
        if hasattr(self, 'show_all_checkbox') and not self.show_all_checkbox.isChecked():
            if hasattr(self, 'filter_combo') and self.filter_combo.count() > 0:
                filter_id = self.filter_combo.currentText()
                frame_boxes = [box for box in frame_boxes if box[4] == filter_id]

        for i, (x, y, w, h, label) in enumerate(frame_boxes):
            is_hidden = label in self.hidden_ids
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            if is_hidden:
                box_item.setOpacity(0.4)
            box_item.setData(0, i)
            self._bbox_items.append(box_item)  # 管理リストに追加

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
            self._bbox_items = []        # 再生開始：管理リストを初期化
            self._play_pixmap_item = None
            self.play_timer.start(interval)
        else:
            self.play_pause_btn.setText("▶ 再生")
            self.play_timer.stop()
            self._bbox_items = []        # 再生停止：管理リストを破棄（以後は通常描画に戻る）
            self._play_pixmap_item = None
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
    def _on_phase4_toggled(self, checked: bool):
        """ID追跡トグル ON/OFF"""
        if checked and not self.image_paths:
            QtWidgets.QMessageBox.warning(self, "警告", "先に画像を読み込んでください。")
            self.phase4_toggle.setChecked(False)
            return
        self.phase4_active = checked
        if hasattr(self, '_phase4_id_combo_label'):
            self._phase4_id_combo_label.setVisible(checked)
        self.phase4_id_combo.setVisible(checked)
        self._phase4_zoom_label.setVisible(checked)
        self.phase4_zoom_combo.setVisible(checked)
        if checked:
            self._refresh_phase4_id_combo()
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
        """現在フレームのラベル数を確認し、インジケーター色とlc_tableのハイライトを更新する"""
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
        self._update_lc_table_highlight(frame_number)

    def _update_lc_table_highlight(self, frame_number):
        """lc_table で現在フレームの行を選択・スクロール（表にない場合は選択解除）"""
        if not hasattr(self, 'lc_table'):
            return
        for row in range(self.lc_table.rowCount()):
            item = self.lc_table.item(row, 0)
            if not item:
                continue
            fn = item.data(QtCore.Qt.UserRole)
            if fn is None:
                try:
                    fn = int(item.text())
                except ValueError:
                    continue
            if fn == frame_number:
                self.lc_table.selectRow(row)
                self.lc_table.scrollTo(self.lc_table.model().index(row, 0))
                return
        self.lc_table.clearSelection()

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

    def _label_check_step(self, direction: int):
        """ラベルチェックモード時のフレーム移動：次/前のNGフレームへジャンプ"""
        if not self.image_paths:
            return
        target = self.lc_count_spin.value() if hasattr(self, 'lc_count_spin') else 0
        n = len(self.image_paths)
        f = self.current_frame_index + direction
        while 0 <= f < n:
            _, fn = self.image_paths[f]
            self._load_frame_if_needed(fn)
            actual = len(self.detections.get(fn, []))
            if actual != target:
                self.current_frame_index = f
                self.load_image()
                if getattr(self, 'phase4_active', False):
                    self._phase4_apply_zoom()
                    self._phase4_update_frame_label()
                return
            f += direction

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
                    if col == 0:
                        dst.setData(QtCore.Qt.UserRole, src.data(QtCore.Qt.UserRole))
                    self.lc_table.setItem(row, col, dst)

    def _lc_jump_to_frame(self, row: int, _col: int):
        """lc_table の行ダブルクリックでフレームジャンプ"""
        item = self.lc_table.item(row, 0)
        if not item:
            return
        fn = item.data(QtCore.Qt.UserRole)
        if fn is None:
            try:
                fn = int(item.text())
            except ValueError:
                return
        for i, (_, frame_number) in enumerate(self.image_paths):
            if frame_number == fn:
                self.current_frame_index = i
                self.load_image()
                return

    def run_phase2_check(self):
        """フェーズ2：全フレームのラベル数をチェックして一覧に表示"""
        if not self.image_paths:
            QtWidgets.QMessageBox.warning(self, "警告", "先に画像を読み込んでください。")
            return

        target_count = self.phase2_count_spin.value()
        ng_frames = []  # [(seq_1indexed, frame_number, actual_count)]

        for seq_i, (_, frame_number) in enumerate(self.image_paths):
            # 遅延ロード
            self._load_frame_if_needed(frame_number)
            actual = len(self.detections.get(frame_number, []))
            if actual != target_count:
                ng_frames.append((seq_i + 1, frame_number, actual))

        # テーブルを更新
        self.phase2_table.setRowCount(0)
        for seq_num, frame_number, actual in ng_frames:
            row = self.phase2_table.rowCount()
            self.phase2_table.insertRow(row)
            fn_item = QtWidgets.QTableWidgetItem(str(seq_num))  # 1-indexed表示
            fn_item.setData(QtCore.Qt.UserRole, frame_number)   # 原フレーム番号を保持
            fn_item.setTextAlignment(QtCore.Qt.AlignCenter)
            cnt_item = QtWidgets.QTableWidgetItem(str(actual))
            cnt_item.setTextAlignment(QtCore.Qt.AlignCenter)
            # 0個は赤、多すぎは橙で色分け
            if actual == 0:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("white")))
            elif actual < target_count:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("yellow")))
            else:
                cnt_item.setForeground(QtGui.QBrush(QtGui.QColor("red")))
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
        fn = item.data(QtCore.Qt.UserRole)
        if fn is None:
            try:
                fn = int(item.text())
            except ValueError:
                return
        for i, (_, frame_number) in enumerate(self.image_paths):
            if frame_number == fn:
                self.current_frame_index = i
                self.load_image()
                self.phase_tab.setCurrentIndex(0)
                return

    # -------- フェーズ切り替え --------
    # -------- Phase4 UI --------
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
        tracking_id = (self.phase4_id_combo.currentText()
                       if hasattr(self, 'phase4_id_combo') and self.phase4_id_combo.count() > 0
                       else self.current_id)
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

        # ズーム倍率を適用（共通ユーティリティ）
        apply_tracking_zoom(self.view,
                            zoom_combo_value(self.phase4_zoom_combo),
                            self._min_zoom_scale, cx, cy)

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
        # 検出モードで再生中なら先に止める
        if self.is_playing:
            self.toggle_play_pause()
        if not self.image_paths:
            if self._ask_confirm(
                "画像未読み込み",
                "追跡フェーズには画像と検出データが必要です。\n今すぐ画像フォルダを読み込みますか？"
            ):
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
    def prompt_add_id(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("ID追加")
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.addWidget(QtWidgets.QLabel("英数字のIDを入力（例: 12, GK1, FW_A など）:"))
        line_edit = QtWidgets.QLineEdit()
        layout.addWidget(line_edit)
        btn_row = QtWidgets.QHBoxLayout()
        cancel_btn = QtWidgets.QPushButton("キャンセル")
        cancel_btn.setStyleSheet(
            "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 16px;}"
            "QPushButton:hover{background-color:#0062d4;}"
        )
        ok_btn = QtWidgets.QPushButton("追加")
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn.clicked.connect(dlg.accept)
        line_edit.returnPressed.connect(dlg.accept)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        new_id = line_edit.text().strip()
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
        self._refresh_phase4_id_combo()

    def set_id(self, new_id: str):
        self.current_id = str(new_id)
        if hasattr(self, 'id_label'):
            self.id_label.setText(f"現在のID: {self.current_id}")
        self.rebuild_id_list_ui()
        self.load_image()
        self._refresh_phase4_id_combo()

    def _toggle_id_visibility(self, id_str: str):
        """IDの表示/非表示を切り替え"""
        if id_str in self.hidden_ids:
            self.hidden_ids.discard(id_str)
        else:
            self.hidden_ids.add(id_str)
        self.rebuild_id_list_ui()
        self._quick_redraw_boxes()

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

    def prompt_delete_id(self):
        """削除するIDをダイアログで選択して全フレームから一括削除"""
        if not self.id_list:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("IDを削除")
        layout = QtWidgets.QVBoxLayout(dlg)
        layout.addWidget(QtWidgets.QLabel("削除するIDを選択:"))
        combo = QtWidgets.QComboBox()
        for id_ in self.id_list:
            combo.addItem(str(id_))
        if self.current_id in self.id_list:
            combo.setCurrentText(self.current_id)
        layout.addWidget(combo)
        btn_row = QtWidgets.QHBoxLayout()
        cancel_btn = QtWidgets.QPushButton("キャンセル")
        cancel_btn.setStyleSheet(
            "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 16px;}"
            "QPushButton:hover{background-color:#0062d4;}"
        )
        ok_btn = QtWidgets.QPushButton("削除")
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)
        cancel_btn.clicked.connect(dlg.reject)
        ok_btn.clicked.connect(dlg.accept)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            target = combo.currentText().strip()
            if target:
                self.delete_all_for_id(target)

    def delete_selected_id_globally(self):
        """後方互換用（prompt_delete_id に委譲）"""
        self.prompt_delete_id()

    def delete_all_for_id(self, target_id: str):
        """指定IDの枠・IDリスト・カラーマップを全て削除"""
        if not self._ask_confirm("確認", f"ID '{target_id}' を本当に削除しますか？"):
            return

        for frame_number, boxes in self.detections.items():
            self.detections[frame_number] = [b for b in boxes if b[4] != target_id]

        if target_id in self.id_list:
            self.id_list.remove(target_id)
        self.id_color_map.pop(target_id, None)

        self.load_image()
        self.rebuild_id_list_ui()

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
            is_hidden = label in self.hidden_ids
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            box_item.setData(0, i)
            if is_hidden:
                box_item.setOpacity(0.4)
            else:
                # テキストラベルを描画（非表示IDはラベルも省略）
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
            is_hidden = label in self.hidden_ids
            color = self.color_for(label)
            pen = QtGui.QPen(color, 2)
            box_item = self.scene.addRect(x, y, w, h, pen)
            box_item.setData(0, i)
            if is_hidden:
                box_item.setOpacity(0.4)
            else:
                text_item = self.scene.addText(str(label))
                text_item.setDefaultTextColor(color)
                text_item.setPos(x, y - 20)
        
        # ラベルチェックインジケーターをリアルタイム更新
        if getattr(self, 'label_check_mode', False):
            self._update_label_check_indicator()

        # 進捗表示の更新をタイマーで遅延実行（操作が連続する場合は更新を先延ばし）
        self.progress_update_timer.stop()
        self.progress_update_timer.start(self.progress_update_delay)
            
    def _on_tab_released(self):
        """TABキーのデバウンス後に実行される本物のリリース処理"""
        self._tab_held = False
        if hasattr(self, 'tab_hold_label'):
            self.tab_hold_label.setVisible(False)

    def _tab_update_hover_only(self):
        """フレーム遷移後にカーソル位置でホバーを更新するだけ（付与は行わない）。
        連鎖的な自動付与を防ぐため、付与はMouseMoveイベントに任せる。"""
        if not self.image_paths:
            return
        cursor_scene = self.view.mapToScene(
            self.view.viewport().mapFromGlobal(QtGui.QCursor.pos()))
        _, frame_id = self.image_paths[self.current_frame_index]
        boxes = self.detections.get(frame_id, [])
        found_idx = None
        for i, (x, y, w, h, lbl) in enumerate(boxes):
            if lbl in self.hidden_ids:
                continue
            if x <= cursor_scene.x() <= x + w and y <= cursor_scene.y() <= y + h:
                found_idx = i
                break
        self._update_hover_overlay(frame_id, found_idx)

    def _tab_assign_at_hover(self):
        """TABキー押下時：現在ホバー中の枠にcurrent_idを付与"""
        if not self.image_paths or self.hover_box_index is None:
            return
        _, frame_id = self.image_paths[self.current_frame_index]
        self._tab_assign_at_hover_idx(frame_id, self.hover_box_index)

    def _tab_assign_at_hover_idx(self, frame_id, box_idx):
        """指定boxにcurrent_idを付与（変化なしならスキップ）"""
        if not self.current_id:
            QtWidgets.QMessageBox.warning(self, "ID未選択", "現在のIDが選択されていません。\nID管理からIDを選択してください。")
            self._tab_held = False
            return
        boxes = self.detections.get(frame_id, [])
        if box_idx >= len(boxes):
            return
        if boxes[box_idx][4] == self.current_id:
            return  # 既に同じIDなのでundoを積まない
        self._save_undo_state("ID変更")
        boxes[box_idx][4] = self.current_id
        self.detections[frame_id] = boxes
        self._quick_redraw_boxes()
        # _quick_redraw_boxes がhover状態をリセットするので再設定
        self._update_hover_overlay(frame_id, box_idx)
        if self.current_frame_index < len(self.image_paths) - 1:
            self.next_frame()
            # フレーム遷移後はホバーのみ更新（付与はMouseMoveで行い連鎖を防ぐ）
            if self._tab_held:
                QtCore.QTimer.singleShot(0, self._tab_update_hover_only)

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

        # TABキー長押し：選択モードでカーソル下の枠に現在のIDを付与
        if event.type() == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Tab:
            if self.mode == "select" and self.current_phase != 3:
                if not event.isAutoRepeat():
                    self._tab_held = True
                    if hasattr(self, 'tab_hold_label'):
                        self.tab_hold_label.setVisible(True)
                    self._tab_assign_at_hover()
                else:
                    # 長押し中の自動繰り返し → 解放タイマーをキャンセル（合成イベント誤検知防止）
                    if hasattr(self, '_tab_release_timer') and self._tab_release_timer.isActive():
                        self._tab_release_timer.stop()
                return True  # フォーカス移動を抑制

        if event.type() == QtCore.QEvent.KeyRelease and event.key() == QtCore.Qt.Key_Tab:
            # rebuild_id_list_ui などが生成する合成イベントと本物のリリースを区別するため
            # デバウンス処理：100ms以内にauto-repeatが来たら合成イベントとみなしキャンセル
            if not hasattr(self, '_tab_release_timer'):
                self._tab_release_timer = QtCore.QTimer(self)
                self._tab_release_timer.setSingleShot(True)
                self._tab_release_timer.timeout.connect(self._on_tab_released)
            self._tab_release_timer.start(100)
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

            # ホバー検出（ドラッグ中でない場合、非表示IDはスキップ）
            boxes = self.detections.get(frame_id, [])
            found_idx = None
            for i, (x, y, w, h, lbl) in enumerate(boxes):
                if lbl in self.hidden_ids:
                    continue
                if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                    found_idx = i
                    break
            
            if found_idx != self.hover_box_index:
                self._update_hover_overlay(frame_id, found_idx)

            # TAB長押し中：ホバー中の枠にIDを付与
            if self._tab_held and self.mode == "select" and found_idx is not None:
                self._tab_assign_at_hover_idx(frame_id, found_idx)

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
                    # 選択モード：IDを変更（非表示IDはスキップ）
                    for i, (x, y, w, h, lbl) in enumerate(boxes):
                        if lbl in self.hidden_ids:
                            continue
                        if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                            if not self.current_id:
                                QtWidgets.QMessageBox.warning(self, "ID未選択", "現在のIDが選択されていません。\nID管理からIDを選択してください。")
                                return True
                            self._save_undo_state("ID変更")
                            boxes[i][4] = self.current_id
                            self.detections[frame_id] = boxes
                            self._quick_redraw_boxes()
                            if self.current_frame_index < len(self.image_paths) - 1:
                                self.next_frame()
                            return True
                elif self.mode == "edit":
                    # 編集モード：枠クリックで移動準備 or 空き地で新規追加（非表示IDはスキップ）
                    clicked_box = False
                    for i, (x, y, w, h, lbl) in enumerate(boxes):
                        if lbl in self.hidden_ids:
                            continue
                        if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                            self._moving_box_index = i
                            self._moving_box_original = boxes[i][:]
                            self._moving_press_scene = pos
                            self._is_dragging_box = False
                            clicked_box = True
                            break

                    if not clicked_box:
                        # 新規ボックスの開始
                        if not self.current_id:
                            QtWidgets.QMessageBox.warning(self, "ID未選択", "現在のIDが選択されていません。\nID管理からIDを選択してください。")
                            return True
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

        # ホイールイベント（Phase3と共通処理: マウス→ズーム, トラックパッド→パン）
        elif event.type() == QtCore.QEvent.Wheel:
            handle_view_wheel(event, self.view,
                              lambda f: self.view.scale(f, f),
                              self._min_zoom_scale)
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

        # ラベルチェック ON のとき Shift+A/D → 次/前の NG フレームへジャンプ
        # （A/D は通常通り下のロジックで処理）
        if getattr(self, 'label_check_mode', False):
            if event.modifiers() & (QtCore.Qt.ShiftModifier | QtCore.Qt.AltModifier):
                if event.key() in (QtCore.Qt.Key_D, QtCore.Qt.Key_Right):
                    self._label_check_step(+1)
                    return
                elif event.key() in (QtCore.Qt.Key_A, QtCore.Qt.Key_Left):
                    self._label_check_step(-1)
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
        btn_cancel.setStyleSheet(
            "QPushButton{background-color:#0a7aff;color:white;border-radius:5px;padding:4px 16px;}"
            "QPushButton:hover{background-color:#0062d4;}"
        )
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
    def _prompt_initial_image_load(self):
        if self._ask_confirm(
            "初期読み込み",
            "画像フォルダを読み込みますか？\n開始するには、まず画像フォルダを選択してください。",
            yes_text="Yes", no_text="No",
        ):
            self.load_images()

    def _startup_update_check_then_prompt(self):
        """起動時の更新チェック → アプリを再起動する場合以外は画像読み込みを促す。
        更新チェックで確認ダイアログが出ている間は、画像読み込みポップアップを
        同時に出さないようにするため、更新チェックの完了を待ってから呼び出す。"""
        restarting = self.check_for_updates(silent=True)
        if not restarting:
            self._prompt_initial_image_load()

    # ================================================================
    # 画像読み込み後ダイアログ（追跡データ読み込みを促す）
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

    # ================================================================
    # 更新確認 (git fetch/pull)
    # ================================================================
    def _repo_root(self) -> str:
        """このスクリプトが置かれているフォルダ（gitリポジトリのルート想定）"""
        return os.path.dirname(os.path.abspath(__file__))

    def _find_git_exe(self) -> str:
        """git実行ファイルのパスを解決する。

        PATH上に"git"が無くても（例えば有効なconda環境にgitが入っておらず、
        別の環境やシステムにしか入っていない場合）よくあるインストール場所を
        直接探すことで、PATHの状態に依存しないようにする。"""
        if getattr(self, '_git_exe_cache', None):
            return self._git_exe_cache

        import shutil
        found = shutil.which("git")
        if not found:
            candidates = [
                r"C:\Program Files\Git\cmd\git.exe",
                r"C:\Program Files\Git\bin\git.exe",
                r"C:\Program Files (x86)\Git\cmd\git.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
                "/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git",
            ]
            for c in candidates:
                if c and os.path.isfile(c):
                    found = c
                    break
        self._git_exe_cache = found or "git"
        return self._git_exe_cache

    def _run_git(self, *args, cwd=None, timeout=30):
        """gitコマンドを実行し (returncode, stdout, stderr) を返す"""
        import subprocess
        try:
            result = subprocess.run(
                [self._find_git_exe(), *args],
                cwd=cwd or self._repo_root(),
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except FileNotFoundError:
            return -1, "", "gitコマンドが見つかりません。Gitをインストールしてください。"
        except Exception as e:
            return -1, "", str(e)

    def check_for_updates(self, silent: bool = False) -> bool:
        """originのmainブランチと比較し、更新があれば確認のうえpullしてアプリを再起動する。

        silent=True の場合（起動時の自動チェック用）は、更新が無い・確認できない・
        未コミットの変更があるなど「今アクションできない」状況ではポップアップを出さず、
        本当に適用可能な更新がある時だけ確認ダイアログを表示する。
        メニューから手動実行した場合（silent=False）は、どの状況でも結果を知らせる。

        どちらの場合も「確認中」であることが分かるよう、
        メニューの表示を一時的に「更新を確認中...」に変え、待機カーソルを出す。

        戻り値: 更新を適用してアプリの再起動処理に入った場合True、それ以外はFalse。
        """
        act = getattr(self, 'act_check_update', None)
        orig_text = act.text() if act else None
        if act:
            act.setEnabled(False)
            act.setText("更新を確認中...")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        QtWidgets.QApplication.processEvents()
        try:
            return self._check_for_updates_impl(silent=silent)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            if act:
                act.setEnabled(True)
                act.setText(orig_text)

    def _check_for_updates_impl(self, silent: bool) -> bool:
        """戻り値: 更新を適用してアプリの再起動処理に入ればTrue、それ以外はFalse。"""
        root = self._repo_root()

        rc, out, err = self._run_git("rev-parse", "--is-inside-work-tree", cwd=root)
        if rc != 0:
            if not silent:
                QtWidgets.QMessageBox.warning(
                    self, "更新確認",
                    "このコピーはgit管理されていないか、確認中にエラーが発生しました。\n"
                    "git cloneで取得したフォルダで実行してください。\n\n"
                    f"確認先フォルダ: {root}\n"
                    f"戻り値: {rc}\n"
                    f"詳細: {err or out or '(メッセージなし)'}"
                )
            return False

        # 自動チェック時はネットワークが遅い/繋がらない場合に起動が止まらないよう短めのタイムアウトにする
        rc, _, err = self._run_git("fetch", "origin", "main", cwd=root, timeout=6 if silent else 30)
        if rc != 0:
            if not silent:
                QtWidgets.QMessageBox.warning(self, "更新確認", f"リモートの取得に失敗しました:\n{err}")
            return False

        rc1, local_hash, _ = self._run_git("rev-parse", "HEAD", cwd=root)
        rc2, remote_hash, _ = self._run_git("rev-parse", "origin/main", cwd=root)
        if rc1 != 0 or rc2 != 0:
            if not silent:
                QtWidgets.QMessageBox.warning(self, "更新確認", "コミット情報の取得に失敗しました。")
            return False

        if local_hash == remote_hash:
            if not silent:
                QtWidgets.QMessageBox.information(self, "更新確認", "最新版です。")
            return False

        _, log, _ = self._run_git("log", "--oneline", f"{local_hash}..{remote_hash}", cwd=root)
        n_commits = len(log.splitlines()) if log else 0

        # 未コミットの変更があると更新で壊れる可能性があるため、pull前にチェックする
        _, dirty, _ = self._run_git("status", "--porcelain", cwd=root)
        if dirty:
            if not silent:
                QtWidgets.QMessageBox.warning(
                    self, "更新確認",
                    f"更新が{n_commits}件あります:\n\n{log}\n\n"
                    "ただし、このフォルダには未コミットの変更があります。\n"
                    "更新を適用する前に、変更を保存またはコミットしてください。"
                )
            return False

        # ここまで来たら「適用可能な更新がある」ので、silentでも確認ダイアログは出す
        # （右=更新する・左=更新しない で統一。QMessageBox標準Yes/NoはOSごとに左右が入れ替わるため使わない）
        if not self._ask_confirm(
            "更新の確認",
            f"新しいバージョンがあります（{n_commits}件の更新）:\n\n{log}\n\n"
            "更新しますか？（適用後アプリを再起動します）",
            yes_text="更新する", no_text="更新しない",
        ):
            return False

        rc, out, err = self._run_git("pull", "--ff-only", "origin", "main", cwd=root)
        if rc != 0:
            QtWidgets.QMessageBox.critical(
                self, "更新確認",
                f"更新の適用に失敗しました:\n{err or out}\n\n"
                "手動で `git pull` を実行して確認してください。"
            )
            return False

        QtWidgets.QMessageBox.information(self, "更新確認", "更新を適用しました。アプリを再起動します。")
        self._restart_app()
        return True

    def _restart_app(self):
        """アプリを同じコマンドで再起動する。失敗した場合は手動再起動を促す。"""
        import sys
        try:
            QtWidgets.QApplication.quit()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "再起動エラー",
                f"自動再起動に失敗しました:\n{e}\n\nアプリを手動で再起動してください。"
            )

