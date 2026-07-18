import os
import glob
import json
import re
import csv
import hashlib
import datetime
import time
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtCore
from models import Interval, BoxList as Box


class FileIOMixin:
    """DetectionEditorのファイルI/O関連メソッド群"""

    def _mark_saved(self):
        """保存が成功した時刻を記録する（終了時の保存確認に使用）"""
        self._last_save_time = time.time()

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
        pass  # _editor_meta.json は出力しない

    def _load_meta(self):
        p = self.meta_path()
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
            # 検出データに実在するIDのセットを取得
            ids_with_data: set = set()
            for boxes in self.detections.values():
                for box in boxes:
                    ids_with_data.add(str(box[4]))
            # 実在するIDのみ復元（不要なデフォルトIDを排除）
            new_list = meta.get("id_list", [])
            if new_list:
                filtered = [str(x) for x in new_list if str(x) in ids_with_data]
                if filtered:
                    self.id_list = filtered
                else:
                    self.id_list = list(ids_with_data)
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

    @staticmethod
    def _coords_to_xywh(v1: float, v2: float, v3: float, v4: float) -> Tuple[float, float, float, float]:
        """(x1,y1,x2,y2) または (x,y,w,h) の4値を (x,y,w,h) に正規化する。"""
        if v3 > v1 and v4 > v2:
            return v1, v2, v3 - v1, v4 - v2
        return v1, v2, v3, v4

    def _confirm_replace_detections(self) -> bool:
        """既に検出データが読み込まれている場合、新しいファイルで置き換える前に確認する。
        保存していない変更は新規読み込みで失われるため、続行するかどうかをユーザーに確認する。
        既存データが無ければ確認なしでTrueを返す。"""
        if not self.detections:
            return True
        return self._ask_confirm(
            "確認",
            "現在の検出データは保存済みですか？\n"
            "新しいファイルを読み込むと、現在のデータは新しい内容で置き換えられます\n"
            "（保存していない変更は失われます）。\n\n"
            "続行しますか？"
        )

    def _reset_detections_for_new_load(self):
        """新しい検出データを読み込む前に、既存のdetections/id_listを完全にクリアする。
        （マージではなく置き換えにするため）"""
        self.detections.clear()
        self.loaded_frames.clear()
        self.id_list.clear()

    def _apply_tmp_detections(self, tmp: Dict[int, list]) -> int:
        """tmpのデータをself.detectionsに転送し、IDリストを更新する。インポート数を返す。"""
        imported = 0
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
        return imported

    def _update_import_label(self, imported: int):
        """インポート数ラベルを更新し、IDリストUIを再構築する。"""
        if hasattr(self, 'det_src_count_label'):
            self.det_src_count_label.setText(f"検出(テキスト)取込数: {imported} 枠")
        self.rebuild_id_list_ui()

    def _import_detections_from_path(self, path: str):
        tmp: Dict[int, List[Box]] = {}
        imported = 0
        
        # ファイルサイズを取得
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "読み込みエラー", "ファイルが見つかりません。")
            return
            
        file_size = os.path.getsize(path)
        self.last_txt_import_folder = os.path.dirname(path)

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
                            
                            x, y, w, h = self._coords_to_xywh(val1, val2, val3, val4)

                        except (ValueError, IndexError):
                            continue

                        tmp.setdefault(frame, []).append([x, y, w, h, label])
                    elif len(parts) >= 5:
                        # フレーム番号なし形式: x1 y1 x2 y2 class [confidence]
                        try:
                            frame = file_frame_number
                            val1, val2, val3, val4 = map(float, parts[0:4])
                            label = str(parts[4]).strip()
                            
                            x, y, w, h = self._coords_to_xywh(val1, val2, val3, val4)

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

        imported = self._apply_tmp_detections(tmp)

        progress.setLabelText("完了! 100%")
        progress.setValue(100)
        QtWidgets.QApplication.processEvents()
        QtCore.QThread.msleep(200)  # 完了を表示
        progress.close()

        self._update_import_label(imported)

    def _import_detections_from_folder(self, folder: str):
        """フォルダ内の全txtファイルから検出結果を読み込む (フレームごとのファイル対応)"""
        # txtファイルを検索
        # 画像フォルダと検出フォルダが異なる場合を想定
        txt_files = glob.glob(os.path.join(folder, "*.txt"))
        
        if not txt_files:
            QtWidgets.QMessageBox.warning(self, "警告", "指定フォルダにtxtファイルが見つかりませんでした。")
            return

        self.last_txt_import_folder = folder

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
                                
                                x, y, w, h = self._coords_to_xywh(val1, val2, val3, val4)

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
        total_imported = self._apply_tmp_detections(tmp)

        progress.close()

        self._update_import_label(total_imported)
        
        QtWidgets.QMessageBox.information(
            self, 
            "読み込み完了", 
            f"{total_files}個のtxtファイルから{total_imported}個の検出枠を読み込みました。"
        )


    def load_detections_from_txt(self, is_folder: bool):
        # 画像が読み込まれていない場合は画像読み込みを促す
        if not self.image_paths:
            if self._ask_confirm(
                "画像未読み込み",
                "先に画像フォルダを読み込む必要があります。\n今すぐ画像フォルダを選択しますか？"
            ):
                self.load_images()
                # 画像読み込みがキャンセルされた場合は終了
                if not self.image_paths:
                    return
            else:
                return

        # 既に検出データがある場合は、置き換えてよいか確認する
        if not self._confirm_replace_detections():
            return

        if not is_folder:
            # 単一ファイルを選択（一括保存形式、またはファイル名にフレーム番号がないフレームごとの形式）
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "検出結果ファイルを選択", filter="Text files (*.txt *.csv);;All files (*)"
            )
            if not path:
                return
            self._reset_detections_for_new_load()
            self._import_detections_from_path(path)
        else:
            # フォルダを選択（フレームごとのファイル形式を想定）
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, "検出結果フォルダを選択"
            )
            if not folder:
                return
            self._reset_detections_for_new_load()
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
        self._mark_saved()
        QtWidgets.QMessageBox.information(self, "保存完了", f"JSON形式で{saved_count}ファイル保存しました（全{total}画像）。")

    # -------- TXT形式で保存 (一括ファイル) --------
    def save_all_txt(self):
        """全フレームの検出結果をTXT形式（一括ファイル）で保存"""
        if not self.image_folder:
            QtWidgets.QMessageBox.warning(self, "警告", "画像フォルダが選択されていません。")
            return
        
        # 保存先ファイルを選択（デフォルト名は日付_時刻.txt、保存先フォルダはtxt読み込み元を優先）
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default_folder = self.last_txt_import_folder or self.image_folder
        default_path = os.path.join(default_folder, f"{timestamp}.txt")
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
            self._mark_saved()
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
        
        # 保存先フォルダを選択（txt読み込み元を優先）
        save_folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "TXT保存先フォルダを選択 (フレームごと)", self.last_txt_import_folder or self.image_folder
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
        self._mark_saved()
        QtWidgets.QMessageBox.information(
            self,
            "保存完了",
            f"TXT形式（フレームごと）で{saved_count}ファイル保存しました。:\n{save_folder}"
        )

    # ================================================================
    # CSV 読み込み (frame, id, x1, y1, x2, y2)
    # ================================================================
    def open_csv(self):
        if not self._confirm_replace_detections():
            return
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
        # loaded_framesもリセットしないと、_load_frame_if_needed() が
        # フレーム表示のたびに画像フォルダの古いLabelMe JSONを読み直し、
        # 今読み込んだ結果を上書きしてしまう。
        self.loaded_frames.clear()
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
            self.loaded_frames.add(fno)
            ids_seen.add(sid)

        # id_list を同期
        sorted_ids = sorted(ids_seen, key=lambda v: (int(v),) if v.isdigit() else (10**9, v))
        self.id_list = sorted_ids if sorted_ids else self.id_list
        self.rebuild_id_list_ui()

    # ================================================================
    # LabelMe JSON フォルダ読み込み
    # ================================================================
    def open_labelme_json_folder(self):
        if not self._confirm_replace_detections():
            return
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
        # loaded_framesもリセットしないと、_load_frame_if_needed() が
        # フレーム表示のたびにこのJSONを読み直そうとして二度手間になる
        # （実害はないが、他フォーマットの読み込みと挙動を揃えておく）。
        self.loaded_frames.clear()
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
                self.loaded_frames.add(fno)
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
            self._mark_saved()
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
    # DeepOCSORT形式 (frame,id,x,y,w,h,conf,cls,vis) 読み込み/保存
    # id=-1 はトラッキング前の生検出（DeepOCSORTへの入力用）を表す。
    # ================================================================
    def open_deepocsort_txt(self):
        if not self._confirm_replace_detections():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "DeepOCSORT形式ファイルを選択 (frame,id,x,y,w,h,conf,cls,vis)",
            filter="CSV/Text files (*.csv *.txt);;CSV files (*.csv);;Text files (*.txt);;All Files (*)"
        )
        if not path:
            return
        try:
            n = self.load_deepocsort_txt(path)
            self.last_txt_import_folder = os.path.dirname(path)
            # 実際に読み込まれたIDの内訳をここで見せる。
            # ID=-1（未追跡の生検出）しか無い場合は、ファイルを間違えている可能性が高いので気付けるようにする。
            n_ids = len(self.id_list)
            id_preview = ", ".join(self.id_list[:8]) + ("..." if n_ids > 8 else "")
            if self.id_list == ["-1"]:
                id_summary = "⚠ 全ボックスがID=-1（未追跡の生検出）でした。ファイルを間違えていませんか？"
            else:
                id_summary = f"ID種類: {n_ids}種類 ({id_preview})"
            QtWidgets.QMessageBox.information(
                self, "読み込み完了",
                f"DeepOCSORT形式を読み込みました（{n}件）:\n{path}\n\n{id_summary}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "読み込みエラー", str(e))

    def load_deepocsort_txt(self, path: str) -> int:
        """DeepOCSORT用フォーマット (frame,id,x,y,w,h[,conf,cls,vis]) を
        読み込み self.detections へ反映する。id=-1 は未追跡（生検出）として
        文字列 "-1" のラベルになる。カンマ区切り優先、無ければ空白区切り。
        読み込んだボックス数を返す。"""
        rows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",") if "," in line else line.split()
                if len(parts) < 6:
                    continue
                rows.append(parts)

        if not rows:
            raise ValueError("読み込めるデータがありません。")

        self.detections.clear()
        # loaded_framesもリセットしないと、_load_frame_if_needed() が
        # フレーム表示のたびに画像フォルダの古いLabelMe JSONを読み直し、
        # 今読み込んだ結果を上書きしてしまう。
        self.loaded_frames.clear()
        ids_seen: set = set()
        count = 0
        for parts in rows:
            try:
                fno = int(float(parts[0]))
                sid = str(int(float(parts[1])))
                x, y, w, h = (float(v) for v in parts[2:6])
            except (ValueError, IndexError):
                continue
            self.detections.setdefault(fno, []).append([x, y, w, h, sid])
            self.loaded_frames.add(fno)
            ids_seen.add(sid)
            count += 1

        sorted_ids = sorted(
            ids_seen, key=lambda v: (int(v),) if v.lstrip('-').isdigit() else (10**9, v)
        )
        if sorted_ids:
            self.id_list = sorted_ids
        self.rebuild_id_list_ui()
        return count

    def save_as_deepocsort_txt(self):
        if not self.detections:
            QtWidgets.QMessageBox.warning(self, "データなし", "保存できる検出データがありません。")
            return
        default_folder = self.last_txt_import_folder or self.image_folder or ""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "DeepOCSORT形式で保存 (frame,id,x,y,w,h,conf,cls,vis)",
            os.path.join(default_folder, "detections.txt"),
            "Text files (*.txt);;All Files (*)"
        )
        if not path:
            return
        try:
            self._export_deepocsort_txt_to(path)
            self._mark_saved()
            QtWidgets.QMessageBox.information(self, "保存完了", f"DeepOCSORT形式で保存しました:\n{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存エラー", str(e))

    def _export_deepocsort_txt_to(self, path: str):
        """self.detections を DeepOCSORTの推論入力に使える形式
        (frame,id,x,y,w,h,conf,cls,vis) で書き出す。
        ラベルが数値のボックスはそのIDを、数値でなければ -1（未追跡の生検出扱い）を書き出す。
        confidenceは常に1.00、class/visibilityは単一クラス運用のため固定値(0, 1)とする。"""
        with open(path, "w", encoding="utf-8") as f:
            for fno in sorted(self.detections.keys()):
                for x, y, w, h, label in self.detections[fno]:
                    try:
                        track_id = int(float(str(label)))
                    except ValueError:
                        track_id = -1
                    f.write(f"{fno},{track_id},{x:.1f},{y:.1f},{w:.1f},{h:.1f},1.00,0,1\n")

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
            self._mark_saved()
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
            self._mark_saved()
            QtWidgets.QMessageBox.information(
                self, "エクスポート完了",
                f"CSV・TXT・JSONを一括保存しました:\n{out_dir}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "エクスポートエラー", str(e))

    # ================================================================
    # 起動時ダイアログ（画像フォルダ読み込みを促す）
    # ================================================================
    def _prompt_tracking_load(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("追跡データの読み込み")
        v = QtWidgets.QVBoxLayout(dlg)
        info = QtWidgets.QLabel(
            "追跡データを読み込みますか？\n\n"
            "  DOC          : frame,id,x,y,w,h,conf,cls,vis 形式 (DeepOCSORT)\n"
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
        btn_doc      = QtWidgets.QPushButton("DOC");           btn_doc.setStyleSheet(_action_style)
        btn_txt_bulk = QtWidgets.QPushButton("TXT\n(一括)");   btn_txt_bulk.setStyleSheet(_action_style)
        btn_txt_dir  = QtWidgets.QPushButton("TXT\n(フォルダ)"); btn_txt_dir.setStyleSheet(_action_style)
        btn_json     = QtWidgets.QPushButton("JSON");          btn_json.setStyleSheet(_action_style)
        btn_skip     = QtWidgets.QPushButton("スキップ");      btn_skip.setStyleSheet(_skip_style)
        for b in (btn_doc, btn_txt_bulk, btn_txt_dir, btn_json, btn_skip):
            btn_row.addWidget(b)
        v.addLayout(btn_row)

        result = [None]
        btn_doc.clicked.connect(lambda:      [result.__setitem__(0, "doc"),      dlg.accept()])
        btn_txt_bulk.clicked.connect(lambda: [result.__setitem__(0, "txt_bulk"), dlg.accept()])
        btn_txt_dir.clicked.connect(lambda:  [result.__setitem__(0, "txt_dir"),  dlg.accept()])
        btn_json.clicked.connect(lambda:     [result.__setitem__(0, "json"),     dlg.accept()])
        btn_skip.clicked.connect(dlg.reject)
        dlg.exec_()

        if result[0] == "doc":
            self.open_deepocsort_txt()
        elif result[0] == "txt_bulk":
            self.load_detections_from_txt(is_folder=False)
        elif result[0] == "txt_dir":
            self.load_detections_from_txt(is_folder=True)
        elif result[0] == "json":
            self.open_labelme_json_folder()

    # ================================================================
    # FPS設定ダイアログ
    # ================================================================
