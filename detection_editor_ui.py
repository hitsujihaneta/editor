import re
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
    QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QSplitter,
    QTabWidget, QVBoxLayout, QWidget,
)
from phase3_widgets import Phase3Widget
from models import _PHASE3_AVAILABLE, ZOOM_PRESETS, ZOOM_DEFAULT_TEXT
from dialogs import FPSSettingsDialog, LayoutAdjusterDialog, IntervalEditor


class UIBuilderMixin:
    """DetectionEditorのUI構築関連メソッド群"""

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
        act_save_deepocsort  = save_menu.addAction("🎯 DeepOCSORT形式で保存 (frame,id,x,y,w,h,conf,cls,vis)...")
        save_menu.addSeparator()
        act_export_all       = save_menu.addAction("📦 一括エクスポート (CSV + TXT + JSON)...")

        # 読み込みサブメニュー
        act_load_det_file   = act_load_det.addAction("📄 TXTファイル（全フレーム一括）")
        act_load_det_folder = act_load_det.addAction("📁 TXTフォルダ（フレームごと）")
        act_load_det.addSeparator()
        act_load_csv        = act_load_det.addAction("📊 CSVファイル (frame,id,x1,y1,x2,y2)")
        act_load_json_folder = act_load_det.addAction("🗂 LabelMe JSONフォルダ")
        act_load_det.addSeparator()
        act_load_deepocsort = act_load_det.addAction("🎯 DeepOCSORT形式 (frame,id,x,y,w,h,conf,cls,vis)")

        # 接続
        act_load_img.triggered.connect(self.load_images)
        act_load_det_file.triggered.connect(lambda: self.load_detections_from_txt(is_folder=False))
        act_load_det_folder.triggered.connect(lambda: self.load_detections_from_txt(is_folder=True))
        act_load_csv.triggered.connect(self.open_csv)
        act_load_json_folder.triggered.connect(self.open_labelme_json_folder)
        act_load_deepocsort.triggered.connect(self.open_deepocsort_txt)
        act_save_json.triggered.connect(self.save_all_json)
        act_save_txt.triggered.connect(self.save_all_txt)
        act_save_txt_per_frame.triggered.connect(self.save_all_txt_per_frame)
        act_save_csv.triggered.connect(self.save_as_csv)
        act_save_labelme.triggered.connect(self.save_as_labelme_json)
        act_save_deepocsort.triggered.connect(self.save_as_deepocsort_txt)
        act_export_all.triggered.connect(self.export_all_formats)

        # --- Undo メニュー ---
        undo_menu = self.menu_bar.addMenu("Undo")
        self.act_undo = undo_menu.addAction("⟲ Undo (Ctrl+Z)")
        self.act_undo.setShortcut("Ctrl+Z")
        self.act_undo.triggered.connect(self.undo_last_operation)

        # --- 更新 メニュー ---
        update_menu = self.menu_bar.addMenu("🔄 更新")
        self.act_check_update = update_menu.addAction("更新を確認...")
        self.act_check_update.triggered.connect(self.check_for_updates)

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
        self.content_splitter.setStretchFactor(0, 2)
        self.content_splitter.setStretchFactor(1, 8)
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
        self.jump_frame_input.setMinimumHeight(self.em(1.6))
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
        playback_layout.addStretch()

        # 再生速度選択コンボボックス
        speed_label = QtWidgets.QLabel("速度:")
        speed_label.setStyleSheet("font-size: 12px;")
        playback_layout.addWidget(speed_label)
        
        self.speed_combo = QtWidgets.QComboBox()
        self.speed_combo.addItems(["0.5x", "1x", "2x", "3x", "5x"])
        self.speed_combo.setCurrentText("2x")  # デフォルト2倍速
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

        filter_layout.addStretch()  # チェックボックス左詰、以降右詰

        self._phase4_id_combo_label = QtWidgets.QLabel("ID:")
        self._phase4_id_combo_label.setStyleSheet("font-size: 12px;")
        self._phase4_id_combo_label.setVisible(False)
        filter_layout.addWidget(self._phase4_id_combo_label)

        self.phase4_id_combo = QtWidgets.QComboBox()
        self.phase4_id_combo.setStyleSheet("font-size: 12px;")
        self.phase4_id_combo.setFixedWidth(self.dp(70))
        self.phase4_id_combo.setVisible(False)
        self.phase4_id_combo.currentIndexChanged.connect(self._phase4_apply_zoom)
        filter_layout.addWidget(self.phase4_id_combo)

        self._phase4_zoom_label = QtWidgets.QLabel("倍率:")
        self._phase4_zoom_label.setStyleSheet("font-size: 12px;")
        self._phase4_zoom_label.setVisible(False)
        filter_layout.addWidget(self._phase4_zoom_label)

        self.phase4_zoom_combo = QtWidgets.QComboBox()
        self.phase4_zoom_combo.setStyleSheet("font-size: 12px;")
        self.phase4_zoom_combo.addItems(ZOOM_PRESETS)
        self.phase4_zoom_combo.setCurrentText(ZOOM_DEFAULT_TEXT)
        self.phase4_zoom_combo.setFixedWidth(self.dp(75))
        self.phase4_zoom_combo.setVisible(False)
        self.phase4_zoom_combo.currentIndexChanged.connect(self._phase4_apply_zoom)
        filter_layout.addWidget(self.phase4_zoom_combo)

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
        self.tab_hold_label = QtWidgets.QLabel("TAB長押し中")
        self.tab_hold_label.setStyleSheet(
            "color: white; background: #e07b00; border-radius: 3px;"
            " padding: 1px 6px; font-weight: bold;"
        )
        self.tab_hold_label.setVisible(False)
        mode_row.addWidget(self.tab_hold_label)
        mode_row.addStretch()
        self.sidebar.addLayout(mode_row)
        self.guide_label = QtWidgets.QLabel()
        self.guide_label.setFont(_sf)
        self.guide_label.setStyleSheet("font-size: 12px;")
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
        self.id_label = QtWidgets.QLabel("現在のID: 未選択")
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

        self.bulk_del_btn = QtWidgets.QPushButton("🗑 ID削除")
        self.bulk_del_btn.setMinimumHeight(self.em(1.4))
        self.bulk_del_btn.clicked.connect(self.prompt_delete_id)
        add_v.addWidget(self.bulk_del_btn)

        # ID一覧のスクロールエリア（IDの実数に応じて rebuild_id_list_ui() が高さ上限を可変にする）
        self.id_scroll = QtWidgets.QScrollArea()
        self.id_scroll.setWidgetResizable(True)
        self.id_scroll.setMinimumHeight(self.em(4))
        self.id_scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.id_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.id_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)

        self.id_scroll_widget = QtWidgets.QWidget()
        self.id_scroll_layout = QtWidgets.QVBoxLayout(self.id_scroll_widget)
        self.id_scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.id_scroll_layout.setSpacing(self.dp(3))
        self.id_scroll.setWidget(self.id_scroll_widget)
        # stretchは大きめに設定し、末尾の余白スペーサー(stretch=1)より優先的に
        # 空間を確保させる。実際の高さは rebuild_id_list_ui() の
        # setMaximumHeight() がID数に応じて頭打ちにするため、IDが少ない時は
        # そこで止まり、余った分は末尾スペーサーに回る。
        add_v.addWidget(self.id_scroll, 20)
        add_v.addSpacing(self.dp(6))

        # --- ラベルチェックトグル（ID一覧の下、常に表示） ---
        self.label_check_toggle = QtWidgets.QCheckBox("ラベル チェック")
        self.label_check_toggle.setFont(_sf)
        self.label_check_toggle.toggled.connect(self._on_label_check_toggled)
        add_v.addWidget(self.label_check_toggle, 0)  # stretch=0: 高さ固定で必ず表示

        # --- ラベルチェック展開パネル（トグルON時に表示） ---
        self.label_check_panel = self._build_label_check_panel()
        self.label_check_panel.setVisible(False)
        add_v.addWidget(self.label_check_panel, 0)  # stretch=0: 高さ固定

        # 余った縦スペースはここ（一番下）に集める
        add_v.addStretch(1)

        self.sidebar.addWidget(add_id_box, 1)  # stretch=1: 残り空間をすべて占有

        # --- 7. ヘルプボタン（左下固定） ---
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
            ("Shift + A / D",   "±10フレーム移動"),
            ("　　（ラベルチェックON時）", ""),
            ("A / D",           "前 / 次フレーム"),
            ("Shift + A / D",   "次 / 前の NG フレームへジャンプ"),
            ("G",               "フレームジャンプ入力欄へ移動"),
            ("Space",           "再生 / 停止"),
            ("Q",               "モード切替（選択 / 編集）"),
            ("Ctrl+S",          "保存形式選択"),
            ("マウススクロール",        "ズーム"),
            ("右ドラック",        "視点移動"),
            ("開 / 終",          "出場区間の設定"),
            ("⋯",               "出場区間の手動編集"),
            ("➡",               "未付与の最小フレームにジャンプ"),
            ("　　（選択モード）", ""),
            ("TAB長押し",        "クリック省略モード"),
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

        # チェックするラベル数（label_check_toggle と同じ _sf サイズ = self.font()*1.5）
        count_row = QtWidgets.QHBoxLayout()
        lc_count_label = QtWidgets.QLabel("チェックするラベル数:")
        _lf = QtGui.QFont(self.font())
        _lf_pt = int(self.font().pointSize() * 1.2)
        if _lf_pt > 0:
            _lf.setPointSize(_lf_pt)
        lc_count_label.setFont(_lf)
        count_row.addWidget(lc_count_label)
        self.lc_count_spin = QtWidgets.QSpinBox()
        self.lc_count_spin.setRange(1, 100)
        self.lc_count_spin.setValue(11)
        self.lc_count_spin.setFont(_lf)
        self.lc_count_spin.setFixedWidth(self.dp(90))
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
        self.phase4_speed_combo.setCurrentText("2x")
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
            self.guide_label.setText("クリック：ID変更 → 自動で次フレーム\nTAB長押し：クリック省略モード")
        else:
            self.mode_label.setText("編集モード")
            self.mode_label.setStyleSheet("color: red; background: white; border-radius: 3px; padding: 1px 4px;")
            if self.require_w_for_add:
                self.guide_label.setText("クリック：削除 / W+ドラッグ：追加")
            else:
                self.guide_label.setText("クリック：削除 / ドラッグ：追加")

    # ---- 進捗/範囲 ----
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
        def _natural_key(s):
            return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]
        sorted_ids = sorted(self.id_list, key=_natural_key)

        fm_global = self.fontMetrics()
        widest_text = ""
        widest_px = 0
        for _id in sorted_ids:
            t = str(_id)
            w = fm_global.horizontalAdvance(t)
            if w > widest_px:
                widest_px, widest_text = w, t
        btn_min_w = widest_px + self.dp(20)

        for id_ in sorted_ids:
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

            # 表示/非表示トグルボタン（IDボタンとフレーム進捗の間）
            is_visible = id_ not in self.hidden_ids
            id_color = self.color_for(id_)
            vis_btn = QtWidgets.QPushButton("●")
            vis_btn.setFixedSize(22, 24)
            vis_btn.setToolTip("表示/非表示を切り替え")
            vis_btn.setStyleSheet(
                f"QPushButton{{color:{id_color.name()}; font-size:13px;"
                f" padding:0; margin:0; border:none; background:transparent;}}"
            )
            if not is_visible:
                effect = QtWidgets.QGraphicsOpacityEffect()
                effect.setOpacity(0.4)
                vis_btn.setGraphicsEffect(effect)
            vis_btn.clicked.connect(lambda _, x=id_: self._toggle_id_visibility(x))
            h.addWidget(vis_btn)

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

        # IDリストの実サイズに応じてスクロールエリアの高さ上限を決める。
        # IDが少ない場合（例：playerのみ）にid_scrollが残り空間を全部占有すると
        # ラベルチェックが画面の一番下に追いやられて使いづらいため、
        # 行数ぶんの実高さまでしか広げないようにし、ラベルチェックを手前に寄せる。
        if sorted_ids:
            first_row = self.id_scroll_layout.itemAt(0).widget()
            row_h = first_row.sizeHint().height() if first_row else self.em(1.6)
            spacing = self.id_scroll_layout.spacing()
            content_h = row_h * len(sorted_ids) + spacing * (len(sorted_ids) - 1)
        else:
            content_h = 0
        self.id_scroll.setMaximumHeight(max(content_h + self.dp(8), self.em(4)))

        if hasattr(self, 'id_scroll_layout'):
            self.id_scroll_layout.addStretch(1)

        if hasattr(self, 'id_label'):
            label_text = f"現在のID: {self.current_id}" if self.current_id else "現在のID: 未選択"
            self.id_label.setText(label_text)

        self._refresh_phase4_id_combo()
        self._refresh_filter_combo()  # フィルターコンボも更新


    def _refresh_phase4_id_combo(self):
        """ID追跡コンボのID一覧を最新化"""
        if not hasattr(self, "phase4_id_combo"):
            return
        cur = self.phase4_id_combo.currentText() if self.phase4_id_combo.count() else None
        self.phase4_id_combo.blockSignals(True)
        self.phase4_id_combo.clear()
        for lab in self.id_list:
            self.phase4_id_combo.addItem(str(lab))
        if cur and cur in self.id_list:
            self.phase4_id_combo.setCurrentText(cur)
        elif self.current_id in self.id_list:
            self.phase4_id_combo.setCurrentText(self.current_id)
        self.phase4_id_combo.blockSignals(False)
    
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

    def showEvent(self, event):
        super().showEvent(event)
        # 初回表示時も _sidebar_ratio に従って比率で設定
        total = self.content_splitter.width()
        if total > 0:
            sidebar_w = max(self.dp(200), int(total * self._sidebar_ratio))
            self.content_splitter.setSizes([sidebar_w, max(100, total - sidebar_w)])

