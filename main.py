import sys
import os
import math

from gerbyx import logger
from gerbyx.tokenizer import tokenize_gerber
from gerbyx.parser import GerberParser
from gerbyx.processor import GerberProcessor

from shapely.geometry import LineString, Polygon, MultiPolygon, Point
from shapely.affinity import rotate, scale, translate
from shapely.ops import unary_union

from PyQt6 import QtWidgets, QtCore, QtGui

class LaserGraphicsView(QtWidgets.QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # БЕЗ OPENGL: Используем только стандартный движок, но с агрессивным кэшированием
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False) # Выключаем сглаживание для скорости
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        
        # Обновляем только те пиксели, которые реально изменились (экономит CPU)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        
        # ИСПРАВЛЕНО ДЛЯ PyQt6: Включаем кэширование заднего фона (координатной сетки)
        self.setCacheMode(QtWidgets.QGraphicsView.CacheModeFlag.CacheBackground)
        
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        
        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        
    def wheelEvent(self, event: QtGui.QWheelEvent):
        """Плавный Zoom колесиком мыши относительно курсора"""
        zoom_factor = 1.25
        if event.angleDelta().y() > 0:
            self.scale(zoom_factor, zoom_factor)
        else:
            self.scale(1.0 / zoom_factor, 1.0 / zoom_factor)

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF):
        """Динамическая миллиметровая сетка (Оптимизированная под CPU)"""
        painter.fillRect(rect, QtGui.QColor("#e8e8e8"))
        
        scene_rect = self.sceneRect()
        left = int(math.floor(scene_rect.left()))
        right = int(math.ceil(scene_rect.right()))
        top = int(math.floor(scene_rect.top()))
        bottom = int(math.ceil(scene_rect.bottom()))
        
        # Толщина 0 включает режим косметического пера (Fast Line Drawing)
        pen_grid_1mm = QtGui.QPen(QtGui.QColor("#dcdcdc"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_grid_10mm = QtGui.QPen(QtGui.QColor("#b8b8b8"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_axes = QtGui.QPen(QtGui.QColor("#808080"), 0, QtCore.Qt.PenStyle.SolidLine)
        
        # Показываем 1мм сетку только при близком зуме
        show_1mm = (right - left) < 150
        
        for x in range(left - 10, right + 10):
            if x % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(x, top - 10, x, bottom + 10)
            elif show_1mm and x % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(x, top - 10, x, bottom + 10)
                
        for y in range(top - 10, bottom + 10):
            if y % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(left - 10, y, right + 10, y)
            elif show_1mm and y % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(left - 10, y, right + 10, y)

        painter.setPen(pen_axes)
        painter.drawLine(0, top - 10, 0, bottom + 10)
        painter.drawLine(left - 10, 0, right + 10, 0)
        
        font = painter.font()
        font.setPointSizeF(2.0)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#555555"))
        
        for x in range((left // 10) * 10, right + 10, 10):
            if x != 0:
                painter.drawText(QtCore.QRectF(x - 5, 0.5, 10, 3), QtCore.Qt.AlignmentFlag.AlignCenter, str(x))
                
        for y in range((top // 10) * 10, bottom + 10, 10):
            if y != 0:
                label_y = -y
                painter.drawText(QtCore.QRectF(-12.0, y - 1.5, 11.0, 3.0), QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, str(label_y))


class LaserConverterApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        ini_path = os.path.expanduser("~/.LaserConverterApp.ini")
        self.settings = QtCore.QSettings(ini_path, QtCore.QSettings.Format.IniFormat)
        
        self.current_gerber_geometry = None
        self.generated_gcode = None
        self.gerber_is_inches = False
        
        self.init_ui()
        self.load_saved_settings()

    def init_ui(self):
        self.setWindowTitle("LaserGRBL Raster Converter & Native Visualizer")
        self.setMinimumWidth(1100)
        self.setMinimumHeight(670)

        self.layout_horizontal = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout_horizontal)

        self.left_panel = QtWidgets.QWidget()
        self.left_layout = QtWidgets.QVBoxLayout()
        self.left_panel.setLayout(self.left_layout)
        self.left_panel.setFixedWidth(420)
        self.layout_horizontal.addWidget(self.left_panel)

        self.file_group = QtWidgets.QGroupBox("Исходный файл Gerber")
        self.file_layout = QtWidgets.QHBoxLayout()
        self.file_group.setLayout(self.file_layout)
        self.entry_path = QtWidgets.QLineEdit()
        self.entry_path.setPlaceholderText("Выберите .gbr файл...")
        self.file_layout.addWidget(self.entry_path)
        self.btn_browse = QtWidgets.QPushButton("Обзор...")
        self.btn_browse.clicked.connect(self.browse_file)
        self.file_layout.addWidget(self.btn_browse)
        self.left_layout.addWidget(self.file_group)

        self.param_group = QtWidgets.QGroupBox("Параметры лазера и станка")
        self.param_grid = QtWidgets.QGridLayout()
        self.param_group.setLayout(self.param_grid)

        self.param_grid.addWidget(QtWidgets.QLabel("Режим лазера GRBL:"), 0, 0)
        self.combo_laser_mode = QtWidgets.QComboBox()
        self.combo_laser_mode.addItems(["M4 (Динамическая мощность)", "M3 (Постоянная мощность)"])
        self.param_grid.addWidget(self.combo_laser_mode, 0, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Макс. мощность лазера (S):"), 1, 0)
        self.spin_power = QtWidgets.QSpinBox()
        self.spin_power.setRange(1, 2000)
        self.spin_power.setValue(255)
        self.param_grid.addWidget(self.spin_power, 1, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Скорость гравировки (мм/мин):"), 2, 0)
        self.spin_feed = QtWidgets.QSpinBox()
        self.spin_feed.setRange(1, 30000)
        self.spin_feed.setValue(1500)
        self.param_grid.addWidget(self.spin_feed, 2, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Шаг строки / Луч (мм):"), 3, 0)
        self.spin_step = QtWidgets.QDoubleSpinBox()
        self.spin_step.setDecimals(4)
        self.spin_step.setRange(0.0010, 10.0000)
        self.spin_step.setSingleStep(0.01)
        self.spin_step.setValue(0.1000)
        self.param_grid.addWidget(self.spin_step, 3, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Вылет каретки Overscan (мм):"), 4, 0)
        self.spin_overscan = QtWidgets.QDoubleSpinBox()
        self.spin_overscan.setDecimals(1)
        self.spin_overscan.setRange(0.0, 50.0)
        self.spin_overscan.setValue(2.0)
        self.param_grid.addWidget(self.spin_overscan, 4, 1)
        
        self.param_grid.addWidget(QtWidgets.QLabel("Точный поворот стола (град):"), 5, 0)
        self.spin_rotate = QtWidgets.QDoubleSpinBox()
        self.spin_rotate.setDecimals(3)
        self.spin_rotate.setRange(-360.000, 360.000)
        self.spin_rotate.setSingleStep(0.01)
        self.spin_rotate.setValue(0.000)
        self.param_grid.addWidget(self.spin_rotate, 5, 1)
        self.left_layout.addWidget(self.param_group)

        self.modes_group = QtWidgets.QGroupBox("Режимы работы и зеркалирование")
        self.modes_layout = QtWidgets.QGridLayout()
        self.modes_group.setLayout(self.modes_layout)
        self.cb_snake = QtWidgets.QCheckBox("Сканирование змейкой")
        self.cb_snake.setChecked(True)
        self.modes_layout.addWidget(self.cb_snake, 0, 0)
        self.cb_invert = QtWidgets.QCheckBox("ИНВЕРСИЯ / НЕГАТИВ (маска)")
        self.modes_layout.addWidget(self.cb_invert, 0, 1)
        self.cb_flip_x = QtWidgets.QCheckBox("Отзеркалить по X")
        self.modes_layout.addWidget(self.cb_flip_x, 1, 0)
        self.cb_flip_y = QtWidgets.QCheckBox("Отзеркалить по Y")
        self.modes_layout.addWidget(self.cb_flip_y, 1, 1)
        self.left_layout.addWidget(self.modes_group)

        self.combo_laser_mode.currentIndexChanged.connect(self.save_current_settings)
        self.spin_power.valueChanged.connect(self.save_current_settings)
        self.spin_feed.valueChanged.connect(self.save_current_settings)
        self.spin_step.valueChanged.connect(self.save_current_settings)
        
        self.spin_rotate.valueChanged.connect(self.update_interactive_preview)
        self.spin_overscan.valueChanged.connect(self.update_interactive_preview)
        self.cb_invert.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_x.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_y.stateChanged.connect(self.update_interactive_preview)

        self.status_label = QtWidgets.QLabel("Статус: Ожидание выбора файла...")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")
        self.left_layout.addWidget(self.status_label)

        self.btn_convert = QtWidgets.QPushButton("Рассчитать траекторию и превью")
        self.btn_convert.setStyleSheet("font-weight: bold; font-size: 13px; padding: 6px; background-color: #0288d1; color: white;")
        self.btn_convert.clicked.connect(self.process_conversion)
        self.left_layout.addWidget(self.btn_convert)

        self.btn_save = QtWidgets.QPushButton("Скачать / Сохранить G-Code")
        self.btn_save.setStyleSheet("font-weight: bold; font-size: 14px; padding: 10px; background-color: #2e7d32; color: white;")
        self.btn_save.setDisabled(True)
        self.btn_save.clicked.connect(self.save_gcode_dialog)
        self.left_layout.addWidget(self.btn_save)
        self.left_layout.addStretch(1)

        self.plot_group = QtWidgets.QGroupBox("Экран интерактивной визуализации векторов")
        self.plot_layout = QtWidgets.QVBoxLayout()
        self.plot_group.setLayout(self.plot_layout)
        self.view = LaserGraphicsView()
        self.plot_layout.addWidget(self.view)
        self.layout_horizontal.addWidget(self.plot_group)

    def load_saved_settings(self):
        self.combo_laser_mode.blockSignals(True)
        self.spin_power.blockSignals(True)
        self.spin_feed.blockSignals(True)
        self.spin_step.blockSignals(True)
        self.spin_overscan.blockSignals(True)
        self.spin_rotate.blockSignals(True)
        self.cb_snake.blockSignals(True)
        self.cb_invert.blockSignals(True)
        self.cb_flip_x.blockSignals(True)
        self.cb_flip_y.blockSignals(True)

        try:
            self.combo_laser_mode.setCurrentIndex(int(self.settings.value("laser_mode_idx", 0)))
            self.spin_power.setValue(int(self.settings.value("laser_power", 255)))
            self.spin_feed.setValue(int(self.settings.value("feed_rate", 1500)))
            self.spin_step.setValue(float(self.settings.value("raster_step", 0.1000)))
            self.spin_overscan.setValue(float(self.settings.value("overscan_dist", 2.0)))
            self.spin_rotate.setValue(float(self.settings.value("rotate_angle", 0.000)))
            self.cb_snake.setChecked(self.settings.value("cb_snake", "true") == "true")
            self.cb_invert.setChecked(self.settings.value("cb_invert", "false") == "true")
            self.cb_flip_x.setChecked(self.settings.value("cb_flip_x", "false") == "true")
            self.cb_flip_y.setChecked(self.settings.value("cb_flip_y", "false") == "true")
        except Exception as e:
            print(f"Инициализация INI: {str(e)}")
        finally:
            self.combo_laser_mode.blockSignals(False)
            self.spin_power.blockSignals(False)
            self.spin_feed.blockSignals(False)
            self.spin_step.blockSignals(False)
            self.spin_overscan.blockSignals(False)
            self.spin_rotate.blockSignals(False)
            self.cb_snake.blockSignals(False)
            self.cb_invert.blockSignals(False)
            self.cb_flip_x.blockSignals(False)
            self.cb_flip_y.blockSignals(False)

    def save_current_settings(self):
        self.settings.setValue("laser_mode_idx", self.combo_laser_mode.currentIndex())
        self.settings.setValue("laser_power", self.spin_power.value())
        self.settings.setValue("feed_rate", self.spin_feed.value())
        self.settings.setValue("raster_step", self.spin_step.value())
        self.settings.setValue("overscan_dist", self.spin_overscan.value())
        self.settings.setValue("rotate_angle", self.spin_rotate.value())
        self.settings.setValue("cb_snake", "true" if self.cb_snake.isChecked() else "false")
        self.settings.setValue("cb_invert", "true" if self.cb_invert.isChecked() else "false")
        self.settings.setValue("cb_flip_x", "true" if self.cb_flip_x.isChecked() else "false")
        self.settings.setValue("cb_flip_y", "true" if self.cb_flip_y.isChecked() else "false")
        self.settings.sync()

    def closeEvent(self, event):
        self.save_current_settings()
        event.accept()

    def browse_file(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Открыть Gerber файл", "", "Gerber Files (*.gbr *.pho);;All Files (*)")
        if file_path:
            self.entry_path.setText(file_path)
            self.status_label.setText(f"Статус: Загрузка {os.path.basename(file_path)}...")
            self.status_label.setStyleSheet("color: blue;")
            self.btn_save.setDisabled(True)
            QtWidgets.QApplication.processEvents()
            self.load_gerber_geometry(file_path)

    def load_gerber_geometry(self, gerber_path):
        try:
            # Замечание 4: Явно указываем UTF-8 кодировку
            with open(gerber_path, 'r', encoding='utf-8', errors='ignore') as f:
                gerber_source = f.read()
            
            # Замечание 1: Проверяем дюймы (%MOIN%)
            self.gerber_is_inches = "%MOIN%" in gerber_source

            processor = GerberProcessor()
            parser = GerberParser(processor)
            tokens = tokenize_gerber(gerber_source)
            parser.parse(tokens)
            
            parsed_geoms = [g for g in processor.geometries if not g.is_empty]
            
            # Замечание 1: Масштабируем дюймы в мм
            if self.gerber_is_inches:
                self.current_gerber_geometry = [scale(g, xfact=25.4, yfact=25.4, origin=(0, 0)) for g in parsed_geoms]
            else:
                self.current_gerber_geometry = parsed_geoms

            if not self.current_gerber_geometry:
                raise ValueError("Файл не содержит графических векторов.")

            self.update_interactive_preview()
            self.view.fitInView(self.view.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        except Exception as e:
            self.current_gerber_geometry = None
            self.status_label.setText(f"Ошибка загрузки Gerber: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def get_transformed_elements(self):
        # Замечание 6: Защита от пустой геометрии
        if not self.current_gerber_geometry: 
            return []

        rotate_angle = self.spin_rotate.value()
        flip_x = self.cb_flip_x.isChecked()
        flip_y = self.cb_flip_y.isChecked()

        all_bounds = [g.bounds for g in self.current_gerber_geometry]
        raw_xmin = min([b[0] for b in all_bounds])
        raw_ymin = min([b[1] for b in all_bounds])
        raw_xmax = max([b[2] for b in all_bounds])
        raw_ymax = max([b[3] for b in all_bounds])
        geom_center = (raw_xmin + (raw_xmax - raw_xmin) / 2.0, raw_ymin + (raw_ymax - raw_ymin) / 2.0)

        transformed = []
        for geom in self.current_gerber_geometry:
            if flip_x or flip_y:
                fx = -1.0 if flip_x else 1.0
                fy = -1.0 if flip_y else 1.0
                geom = scale(geom, xfact=fx, yfact=fy, origin=geom_center)
            if rotate_angle != 0.0:
                geom = rotate(geom, rotate_angle, origin=geom_center)
            transformed.append(geom)
        return transformed

    def update_interactive_preview(self):
        if not self.current_gerber_geometry: 
            return

        self.save_current_settings()
        overscan = self.spin_overscan.value()
        invert_mode = self.cb_invert.isChecked()

        try:
            transformed_elements = self.get_transformed_elements()
            if not transformed_elements: return
            
            t_bounds = [g.bounds for g in transformed_elements]
            t_xmin = min([b[0] for b in t_bounds])
            t_ymin = min([b[1] for b in t_bounds])
            t_xmax = max([b[2] for b in t_bounds])
            t_ymax = max([b[3] for b in t_bounds])

            self.view.scene.clear()
            qt_path = QtGui.QPainterPath()

            def add_shapely_to_qt_path(g_item):
                if g_item.is_empty: return
                if g_item.geom_type == 'Polygon':
                    x_ext, y_total = g_item.exterior.xy
                    poly_path = QtGui.QPainterPath()
                    poly_path.moveTo(x_ext[0] - t_xmin + overscan, -(y_total[0] - t_ymin))
                    for x, y in zip(x_ext[1:], y_total[1:]):
                        poly_path.lineTo(x - t_xmin + overscan, -(y - t_ymin))
                    poly_path.closeSubpath()

                    for interior in g_item.interiors:
                        x_int, y_int = interior.xy
                        int_path = QtGui.QPainterPath()
                        int_path.moveTo(x_int[0] - t_xmin + overscan, -(y_int[0] - t_ymin))
                        for x, y in zip(x_int[1:], y_int[1:]):
                            int_path.lineTo(x - t_xmin + overscan, -(y - t_ymin))
                        int_path.closeSubpath()
                        poly_path = poly_path.subtracted(int_path)
                    qt_path.addPath(poly_path)
                elif g_item.geom_type in ['MultiPolygon', 'GeometryCollection']:
                    for sub_geom in g_item.geoms: add_shapely_to_qt_path(sub_geom)
                elif g_item.geom_type in ['LineString', 'LinearRing']:
                    x_l, y_l = g_item.xy
                    line_path = QtGui.QPainterPath()
                    line_path.moveTo(x_l[0] - t_xmin + overscan, -(y_l[0] - t_ymin))
                    for x, y in zip(x_l[1:], y_l[1:]):
                        line_path.lineTo(x - t_xmin + overscan, -(y - t_ymin))
                    qt_path.addPath(line_path)

            for geom in transformed_elements:
                add_shapely_to_qt_path(geom)

            w = t_xmax - t_xmin + (2 * overscan)
            h = t_ymax - t_ymin
            
            if w <= 0 or h <= 0: return

            self.view.scene.setSceneRect(-5, -h - 5, w + 10, h + 10)

            path_item = QtWidgets.QGraphicsPathItem(qt_path)
            # Включаем DeviceCoordinateCache процессора для плавной отрисовки зума без лагов
            path_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)

            if invert_mode:
                bg_rect = QtWidgets.QGraphicsRectItem(0, -h, w, h)
                bg_rect.setBrush(QtGui.QBrush(QtGui.QColor("#1565c0")))
                bg_rect.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                self.view.scene.addItem(bg_rect)

                path_item.setBrush(QtGui.QBrush(QtGui.QColor("#e8e8e8")))
                path_item.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                self.view.scene.addItem(path_item)
            else:
                path_item.setBrush(QtGui.QBrush(QtGui.QColor("#2e7d32")))
                path_item.setPen(QtGui.QPen(QtGui.QColor("#2e7d32"), 0.1))
                self.view.scene.addItem(path_item)

            home_marker = QtWidgets.QGraphicsEllipseItem(-0.6, -0.6, 1.2, 1.2)
            home_marker.setBrush(QtGui.QBrush(QtGui.QColor("#ff0000")))
            home_marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.2))
            self.view.scene.addItem(home_marker)

            self.status_label.setText(f"Статус: Геометрия готова. Размер: {w:.2f} x {h:.2f} мм")
            self.status_label.setStyleSheet("color: #2e7d32;")
        except Exception as e:
            self.status_label.setText(f"Ошибка визуализации: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def process_conversion(self):
        if not self.current_gerber_geometry: return
        self.save_current_settings()
        self.status_label.setText("Статус: Оптимизация слоев...")
        self.status_label.setStyleSheet("color: orange;")
        QtWidgets.QApplication.processEvents()

        power = self.spin_power.value()
        feedrate = self.spin_feed.value()
        step = self.spin_step.value()
        overscan = self.spin_overscan.value()
        
        snake_mode = self.cb_snake.isChecked()
        invert_mode = self.cb_invert.isChecked()
        selected_mode_txt = "M4" if self.combo_laser_mode.currentIndex() == 0 else "M3"

        try:
            transformed_elements = self.get_transformed_elements()
            if not transformed_elements: return
            
            t_bounds = [g.bounds for g in transformed_elements]
            t_xmin = min([b[0] for b in t_bounds])
            t_ymin = min([b[1] for b in t_bounds])
            t_xmax = max([b[2] for b in t_bounds])
            t_ymax = max([b[3] for b in t_bounds])

            xmin, ymin = 0.0, 0.0
            xmax = t_xmax - t_xmin + (2 * overscan)
            ymax = t_ymax - t_ymin

            # Замечание 3: Оптимизация — слияние через unary_union ДО цикла
            moved_geometries = []
            if invert_mode:
                bounding_box = Polygon([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])
                final_mask = bounding_box
                for geom in transformed_elements:
                    shifted_geom = translate(geom, xoff=(-t_xmin + overscan), yoff=-t_ymin)
                    final_mask = final_mask.difference(shifted_geom)
                moved_geometries.append(final_mask)
            else:
                merged_pads = unary_union([translate(geom, xoff=(-t_xmin + overscan), yoff=-t_ymin) for geom in transformed_elements])
                moved_geometries.append(merged_pads)

            gcode = []
            gcode.append("; Gerber -> LaserGRBL GCode (Native Fast Engine)")
            gcode.append(f"G21 ; Миллиметры\nG90 ; Абсолютные координаты\n{selected_mode_txt} S0 ; Инициализация лазера")
            gcode.append(f"G0 X{0.0000:.4f} Y{(ymin + (step / 2.0)):.4f} F{feedrate} ; Старт")

            # Замечание 2: Безопасный расчет количества строк растра
            lines_count = int(math.ceil((ymax - ymin) / step))
            if lines_count <= 0: lines_count = 1

            direction_right = True
            blue_laser_path = QtGui.QPainterPath()
            red_overscan_path = QtGui.QPainterPath()

            for line_idx in range(lines_count):
                # Замечание 2: Позиция строго по индексу без накопления float ошибок
                current_y = ymin + (line_idx * step) + (step / 2.0)
                if current_y > ymax: current_y = ymax

                # Защита от фризов интерфейса при мелком шаге (каждые 200 строк)
                if line_idx % 200 == 0:
                    self.status_label.setText(f"Расчет: строка {line_idx} из {lines_count}...")
                    QtWidgets.QApplication.processEvents()

                scan_line = LineString([(xmin - 1.0, current_y), (xmax + 1.0, current_y)])
                segments_coords = []

                for target_geom in moved_geometries:
                    laser_on_segments = scan_line.intersection(target_geom)
                    if not laser_on_segments.is_empty:
                        geoms_to_process = []
                        if laser_on_segments.geom_type in ['MultiLineString', 'GeometryCollection']:
                            geoms_to_process = list(laser_on_segments.geoms)
                        else:
                            geoms_to_process = [laser_on_segments]
                        
                        for g in geoms_to_process:
                            if g.geom_type in ['LineString', 'LinearRing']:
                                start_x = float(g.coords[0][0])
                                end_x = float(g.coords[-1][0])
                                segments_coords.append((start_x, end_x))
                            # Замечание 5: Обработка касаний на углах (Point)
                            elif g.geom_type == 'Point':
                                pt_x = float(g.x)
                                segments_coords.append((pt_x - 0.005, pt_x + 0.005))

                if segments_coords:
                    segments_coords.sort(key=lambda val: val[0])
                    merged = []
                    curr_start, curr_end = segments_coords[0]
                    for start, end in segments_coords[1:]:
                        if start <= curr_end: 
                            curr_end = max(curr_end, end)
                        else:
                            merged.append((curr_start, curr_end))
                            curr_start, curr_end = start, end
                    merged.append((curr_start, curr_end))
                    segments_coords = merged

                line_start_x = xmin
                line_end_x = xmax

                for s_x, e_x in segments_coords:
                    blue_laser_path.moveTo(s_x, -current_y)
                    blue_laser_path.lineTo(e_x, -current_y)

                if direction_right or not snake_mode:
                    segments_coords.sort(key=lambda val: val[0])
                    gcode.append(f"G0 X{line_start_x:.4f} Y{current_y:.4f}")
                    red_overscan_path.moveTo(line_start_x, -current_y)
                    
                    last_x = line_start_x
                    for start_x, end_x in segments_coords:
                        if start_x > last_x: 
                            gcode.append(f"G1 X{start_x:.4f} S0")
                            red_overscan_path.lineTo(start_x, -current_y)
                        gcode.append(f"G1 X{end_x:.4f} S{power}")
                        red_overscan_path.moveTo(end_x, -current_y)
                        last_x = end_x
                    if last_x < line_end_x: 
                        gcode.append(f"G1 X{line_end_x:.4f} S0")
                        red_overscan_path.lineTo(line_end_x, -current_y)
                else:
                    segments_coords.sort(key=lambda val: val[0], reverse=True)
                    gcode.append(f"G0 X{line_end_x:.4f} Y{current_y:.4f}")
                    red_overscan_path.moveTo(line_end_x, -current_y)
                    
                    last_x = line_end_x
                    for start_x, end_x in segments_coords:
                        if end_x < last_x: 
                            gcode.append(f"G1 X{end_x:.4f} S0")
                            red_overscan_path.lineTo(end_x, -current_y)
                        gcode.append(f"G1 X{start_x:.4f} S{power}")
                        red_overscan_path.moveTo(start_x, -current_y)
                        last_x = start_x
                    if last_x > line_start_x: 
                        gcode.append(f"G1 X{line_start_x:.4f} S0")
                        red_overscan_path.lineTo(line_start_x, -current_y)

                if snake_mode: 
                    direction_right = not direction_right

            gcode.append("M5\nG0 X0 Y0\nM2")
            self.generated_gcode = "\n".join(gcode)

            self.view.scene.clear()
            self.view.setBackgroundBrush(QtGui.QColor("#f0f0f0"))

            red_item = QtWidgets.QGraphicsPathItem(red_overscan_path)
            red_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
            red_item.setPen(QtGui.QPen(QtGui.QColor("#ff0000"), 0.03, QtCore.Qt.PenStyle.SolidLine))
            self.view.scene.addItem(red_item)

            blue_item = QtWidgets.QGraphicsPathItem(blue_laser_path)
            blue_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)
            blue_item.setPen(QtGui.QPen(QtGui.QColor("#0000ff"), step, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap))
            self.view.scene.addItem(blue_item)

            home_marker = QtWidgets.QGraphicsEllipseItem(-0.5, -0.5, 1, 1)
            home_marker.setBrush(QtGui.QBrush(QtGui.QColor("red")))
            self.view.scene.addItem(home_marker)

            self.btn_save.setDisabled(False)
            self.status_label.setText(f"Статус: Успешно! Траектория построена ({lines_count} строк).")
            self.status_label.setStyleSheet("color: green;")
        except Exception as e:
            self.status_label.setText(f"Ошибка вычислений: {str(e)}")
            self.status_label.setStyleSheet("color: red;")
            self.btn_save.setDisabled(True)

    def save_gcode_dialog(self):
        if self.generated_gcode is None: return
        gerber_path = self.entry_path.text().strip()
        default_output_name = os.path.splitext(gerber_path)[0] + ".gcode"
        output_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить лазерный G-Code", default_output_name, "G-Code Files (*.gcode *.nc);;All Files (*)")
        if output_path:
            try:
                with open(output_path, "w", encoding='utf-8') as f: 
                    f.write(self.generated_gcode)
                self.status_label.setText(f"Файл сохранен: {os.path.basename(output_path)}")
            except Exception as e:
                self.status_label.setText(f"Ошибка записи: {str(e)}")

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = LaserConverterApp()
    window.show()
    sys.exit(app.exec())
