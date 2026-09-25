import sys
import os
import math
import re
import traceback

from gerbyx import logger
from gerbyx.tokenizer import tokenize_gerber
from gerbyx.parser import GerberParser
from gerbyx.processor import GerberProcessor

from shapely.geometry import LineString, Polygon, MultiPolygon, Point
from shapely.affinity import rotate, scale, translate, affine_transform
from shapely.ops import unary_union

from PyQt6 import QtWidgets, QtCore, QtGui
import numpy as np

class CrosshairOverlay(QtWidgets.QWidget):
    """Кастомное прозрачное 'стекло' поверх экрана. Рисует прицел во весь экран и HUD-координаты."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.active = False
        # Переменные для хранения текущих координат под прицелом
        self.current_x = 0.0
        self.current_y = 0.0

    def set_coordinates(self, x, y):
        """Обновляет координаты и вызывает перерисовку табло"""
        self.current_x = x
        self.current_y = y
        self.update()

    def paintEvent(self, event):
        if not self.active:
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        # 1. РИСУЕМ АВИАЦИОННЫЙ ПРИЦЕЛ (HUD)
        pen = QtGui.QPen(QtGui.QColor(255, 23, 68, 90), 2.0) # Полупрозрачный красный
        painter.setPen(pen)

        cx = self.width() // 2
        cy = self.height() // 2

        painter.drawLine(0, cy, self.width(), cy)
        painter.drawLine(cx, 0, cx, self.height())

        painter.drawEllipse(QtCore.QPoint(cx, cy), 8, 8)
        painter.drawEllipse(QtCore.QPoint(cx, cy), 24, 24)
        painter.drawEllipse(QtCore.QPoint(cx, cy), 48, 48)

        # 2. РИСУЕМ ПЛАВАЮЩЕЕ ТАБЛО С КООРДИНАТАМИ (В ЛЕВОМ НИЖНЕМ УГЛУ)
        hud_w, hud_h = 180, 68
        hud_x = 20
        hud_y = self.height() - hud_h - 20

        # Мягкая темная подложка
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, 160)))
        painter.drawRoundedRect(QtCore.QRectF(hud_x, hud_y, hud_w, hud_h), 6.0, 6.0)

        # Пишем цифровые значения координат под прицелом
        painter.setPen(QtGui.QPen(QtGui.QColor("#00e676"))) # Ярко-зеленый люминофор
        font = painter.font()
        font.setFamily("Monospace")
        font.setPointSizeF(10.0)
        font.setBold(True)
        painter.setFont(font)

        # Текст для вывода
        hud_text = (
            f" ПРИЦЕЛ:\n"
            f" X: {self.current_x:>8.3f} мм\n"
            f" Y: {self.current_y:>8.3f} мм"
        )

        painter.drawText(
            QtCore.QRectF(hud_x + 12, hud_y + 6, hud_w - 24, hud_h - 12),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            hud_text
        )

        painter.end()

class LaserGraphicsView(QtWidgets.QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setCacheMode(QtWidgets.QGraphicsView.CacheModeFlag.CacheBackground)

        # Жесткое центрирование зума строго по центру экрана под перекрестием
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)

        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)

        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)

        # Инициализируем оверлей прицела и крепим его поверх вьюпорта
        self.overlay = CrosshairOverlay(self)

    def resizeEvent(self, event):
        """Растягиваем прозрачное стекло прицела вслед за изменением окна виджета"""
        super().resizeEvent(event)
        self.overlay.setGeometry(self.viewport().geometry())

    @property
    def calibration_mode(self):
        return self.overlay.active

    @calibration_mode.setter
    def calibration_mode(self, value):
        self.overlay.active = value
        self.overlay.update()

    def get_center_board_coordinates(self):
        """Вычисляет, какая точная координата Shapely-сцены сейчас находится строго по центру экрана"""
        view_center = self.viewport().rect().center()
        scene_pos = self.mapToScene(view_center)
        return scene_pos.x(), scene_pos.y()

    def wheelEvent(self, event: QtGui.QWheelEvent):
        zoom_factor = 1.25
        if event.angleDelta().y() > 0:
            self.scale(zoom_factor, zoom_factor)
        else:
            self.scale(1.0 / zoom_factor, 1.0 / zoom_factor)

    def scrollContentsBy(self, dx, dy):
        """Срабатывает при любом сдвиге сцены мышью или скроллбарами"""
        super().scrollContentsBy(dx, dy)

        if self.calibration_mode:
            main_win = self.window()
            if hasattr(main_win, 'current_gerber_geometry') and main_win.current_gerber_geometry:
                # Берем чистую метрическую координату центра экрана станка
                scene_x, scene_y = self.get_center_board_coordinates()

                # Отправляем абсолютные координаты осей на визир оверлея
                self.overlay.set_coordinates(scene_x, -scene_y)

    def drawBackground(self, painter: QtGui.QPainter, rect: QtCore.QRectF):
        """Бесконечная миллиметровая сетка с аппаратной защитой от зависаний процессора"""
        painter.fillRect(rect, QtGui.QColor("#e8e8e8"))

        left = int(math.floor(rect.left()))
        right = int(math.ceil(rect.right()))
        top = int(math.floor(rect.top()))
        bottom = int(math.ceil(rect.bottom()))

        pen_grid_1mm = QtGui.QPen(QtGui.QColor("#dcdcdc"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_grid_10mm = QtGui.QPen(QtGui.QColor("#b8b8b8"), 0, QtCore.Qt.PenStyle.SolidLine)
        pen_axes = QtGui.QPen(QtGui.QColor("#808080"), 0, QtCore.Qt.PenStyle.SolidLine)

        width_mm = right - left
        show_1mm = width_mm < 150

        # ЗАЩИТА: Если экран слишком сильно отдален, увеличиваем шаг прорисовки, чтобы ЧПУ не лагало
        step_grid = 1 if width_mm < 1000 else (10 if width_mm < 5000 else 50)

        # Рисуем вертикальные линии
        for x in range(left - 10, right + 10, step_grid):
            if x % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(QtCore.QPointF(x, top - 10), QtCore.QPointF(x, bottom + 10))
            elif show_1mm and x % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(QtCore.QPointF(x, top - 10), QtCore.QPointF(x, bottom + 10))

        # Рисуем горизонтальные линии
        for y in range(top - 10, bottom + 10, step_grid):
            if y % 10 == 0:
                painter.setPen(pen_grid_10mm)
                painter.drawLine(QtCore.QPointF(left - 10, y), QtCore.QPointF(right + 10, y))
            elif show_1mm and y % 1 == 0:
                painter.setPen(pen_grid_1mm)
                painter.drawLine(QtCore.QPointF(left - 10, y), QtCore.QPointF(right + 10, y))

        # Железные оси координат (0,0) станка ЧПУ
        painter.setPen(pen_axes)
        painter.drawLine(QtCore.QPointF(0, top - 10), QtCore.QPointF(0, bottom + 10))
        painter.drawLine(QtCore.QPointF(left - 10, 0), QtCore.QPointF(right + 10, 0))

        # Рисуем шкалу линеек
        font = painter.font()
        font.setPointSizeF(2.0)
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#555555"))

        # Подписи осей адаптируются под шаг сетки
        for x in range((left // 10) * 10, right + 10, 10 if step_grid == 1 else step_grid):
            if x != 0:
                painter.drawText(QtCore.QRectF(x - 5, 0.5, 10, 3), QtCore.Qt.AlignmentFlag.AlignCenter, str(x))

        for y in range((top // 10) * 10, bottom + 10, 10 if step_grid == 1 else step_grid):
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

        # Переменные для ручного авиационного базирования (массивы на 4 точки)
        self.manual_file_pts = [None, None, None, None]
        self.manual_mach_pts = [None, None, None, None]
        self.manual_markers = [None, None, None, None]

        self.use_calibration = False
        self.matrix_coeffs = None

        self.init_ui()
        self.load_saved_settings()

    def init_ui(self):
        self.setWindowTitle("LaserGRBL Raster Converter & Native Visualizer")
        self.setMinimumWidth(1150)
        self.setMinimumHeight(760)

        self.layout_horizontal = QtWidgets.QHBoxLayout()
        self.setLayout(self.layout_horizontal)

        # Левая панель управления
        self.left_panel = QtWidgets.QWidget()
        self.left_layout = QtWidgets.QVBoxLayout()
        self.left_panel.setLayout(self.left_layout)
        self.left_panel.setFixedWidth(430)
        self.layout_horizontal.addWidget(self.left_panel)

        # БЛОК 1: Исходный файл Gerber
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

        # БЛОК 2: Параметры лазера и станка
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
        self.spin_overscan.setSingleStep(0.5)
        self.param_grid.addWidget(self.spin_overscan, 4, 1)

        self.param_grid.addWidget(QtWidgets.QLabel("Точный поворот стола (град):"), 5, 0)
        self.spin_rotate = QtWidgets.QDoubleSpinBox()
        self.spin_rotate.setDecimals(3)
        self.spin_rotate.setRange(-360.000, 360.000)
        self.spin_rotate.setSingleStep(0.01)
        self.spin_rotate.setValue(0.000)
        self.param_grid.addWidget(self.spin_rotate, 5, 1)
        self.left_layout.addWidget(self.param_group)

        # БЛОК 3: Режимы работы и зеркалирование
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
        # БЛОК 4: Оптическое смещение (офсет) камеры — ВЫНЕСЕН НАВЕРХ
        self.camera_group = QtWidgets.QGroupBox("Оптическое смещение (офсет) камеры")
        camera_main_layout = QtWidgets.QVBoxLayout()
        self.camera_group.setLayout(camera_main_layout)

        self.cb_use_camera_offset = QtWidgets.QCheckBox("Включить компенсацию смещения камеры")
        self.cb_use_camera_offset.setStyleSheet("font-weight: bold; color: #43a047;")
        self.cb_use_camera_offset.stateChanged.connect(self.toggle_camera_fields_visibility)
        camera_main_layout.addWidget(self.cb_use_camera_offset)

        # Контейнер для полей ввода (чтобы скрывать/показывать группу целиком)
        self.camera_fields_widget = QtWidgets.QWidget()
        camera_grid = QtWidgets.QGridLayout(self.camera_fields_widget)
        camera_grid.setContentsMargins(0, 5, 0, 0)

        camera_grid.addWidget(QtWidgets.QLabel("Сдвиг по X (Камера -> Лазер):"), 0, 0)
        self.spin_cam_offset_x = QtWidgets.QDoubleSpinBox()
        self.spin_cam_offset_x.setDecimals(3)
        self.spin_cam_offset_x.setRange(-5000.000, 5000.000)
        self.spin_cam_offset_x.setSingleStep(0.1)
        self.spin_cam_offset_x.setValue(0.000)
        self.spin_cam_offset_x.valueChanged.connect(self.update_interactive_preview)
        camera_grid.addWidget(self.spin_cam_offset_x, 0, 1)

        camera_grid.addWidget(QtWidgets.QLabel("Сдвиг по Y (Камера -> Лазер):"), 1, 0)
        self.spin_cam_offset_y = QtWidgets.QDoubleSpinBox()
        self.spin_cam_offset_y.setDecimals(3)
        self.spin_cam_offset_y.setRange(-5000.000, 5000.000)
        self.spin_cam_offset_y.setSingleStep(0.1)
        self.spin_cam_offset_y.setValue(0.000)
        self.spin_cam_offset_y.valueChanged.connect(self.update_interactive_preview)
        camera_grid.addWidget(self.spin_cam_offset_y, 1, 1)

        camera_main_layout.addWidget(self.camera_fields_widget)
        self.camera_fields_widget.setVisible(False)  # Скрыто по умолчанию
        self.left_layout.addWidget(self.camera_group)

        # БЛОК 5: Включение ручной разметки платы по точкам (ниже камеры)
        self.cb_enable_calib = QtWidgets.QCheckBox("Включить ручную разметку платы по точкам")
        self.cb_enable_calib.setStyleSheet("font-weight: bold; color: #0288d1; margin-top: 5px;")
        self.cb_enable_calib.stateChanged.connect(self.toggle_manual_calibration)
        self.left_layout.addWidget(self.cb_enable_calib)

        self.calib_group = QtWidgets.QGroupBox("Базирование по центральному прицелу")
        self.calib_layout = QtWidgets.QVBoxLayout()
        self.calib_group.setLayout(self.calib_layout)

        self.cb_use_pt4 = QtWidgets.QCheckBox("Использовать 4-ю точку для коррекции деформаций")
        self.cb_use_pt4.stateChanged.connect(self.toggle_pt4_active)
        self.calib_layout.addWidget(self.cb_use_pt4)

        self.cb_show_markers = QtWidgets.QCheckBox("Показывать зафиксированные точки на плате")
        self.cb_show_markers.setChecked(True)
        self.cb_show_markers.stateChanged.connect(self.toggle_markers_visibility)
        self.calib_layout.addWidget(self.cb_show_markers)

        # Кнопки фиксации точек
        self.btn_pt1 = QtWidgets.QPushButton("Зафиксировать Точку 1")
        self.btn_pt2 = QtWidgets.QPushButton("Зафиксировать Точку 2")
        self.btn_pt3 = QtWidgets.QPushButton("Зафиксировать Точку 3")
        self.btn_pt4 = QtWidgets.QPushButton("Зафиксировать Точку 4")
        self.btn_pt4.setDisabled(True)

        self.btn_pt1.clicked.connect(lambda: self.capture_point_in_crosshair(0))
        self.btn_pt2.clicked.connect(lambda: self.capture_point_in_crosshair(1))
        self.btn_pt3.clicked.connect(lambda: self.capture_point_in_crosshair(2))
        self.btn_pt4.clicked.connect(lambda: self.capture_point_in_crosshair(3))

        self.calib_layout.addWidget(self.btn_pt1)
        self.calib_layout.addWidget(self.btn_pt2)
        self.calib_layout.addWidget(self.btn_pt3)
        self.calib_layout.addWidget(self.btn_pt4)

        self.left_layout.addWidget(self.calib_group)
        self.calib_group.setVisible(False)

        # Подключение сигналов автосохранения
        self.combo_laser_mode.currentIndexChanged.connect(self.save_current_settings)
        self.spin_power.valueChanged.connect(self.save_current_settings)
        self.spin_feed.valueChanged.connect(self.save_current_settings)
        self.spin_step.valueChanged.connect(self.save_current_settings)
        self.cb_use_camera_offset.stateChanged.connect(self.save_current_settings)
        self.spin_cam_offset_x.valueChanged.connect(self.save_current_settings)
        self.spin_cam_offset_y.valueChanged.connect(self.save_current_settings)

        self.spin_rotate.valueChanged.connect(self.update_interactive_preview)
        self.spin_overscan.valueChanged.connect(self.update_interactive_preview)
        self.cb_snake.stateChanged.connect(self.update_interactive_preview)
        self.cb_invert.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_x.stateChanged.connect(self.update_interactive_preview)
        self.cb_flip_y.stateChanged.connect(self.update_interactive_preview)

        # Статус-бар и пусковые кнопки
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

        # Правый графический холст визуализации векторов
        self.plot_group = QtWidgets.QGroupBox("Экран интерактивной визуализации векторов")
        self.plot_layout = QtWidgets.QVBoxLayout()
        self.plot_group.setLayout(self.plot_layout)

        self.view = LaserGraphicsView()
        self.plot_layout.addWidget(self.view)
        self.layout_horizontal.addWidget(self.plot_group)

    def toggle_camera_fields_visibility(self, state):
        """Показывает или скрывает поля ввода офсета камеры и обновляет превью"""
        is_active = (state == 2)
        if hasattr(self, 'camera_fields_widget'):
            self.camera_fields_widget.setVisible(is_active)
        self.update_interactive_preview()

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
        if hasattr(self, 'cb_use_camera_offset'): self.cb_use_camera_offset.blockSignals(True)
        if hasattr(self, 'spin_cam_offset_x'): self.spin_cam_offset_x.blockSignals(True)
        if hasattr(self, 'spin_cam_offset_y'): self.spin_cam_offset_y.blockSignals(True)

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

            if hasattr(self, 'cb_use_camera_offset'):
                self.cb_use_camera_offset.setChecked(self.settings.value("use_camera_offset", "false") == "true")
            if hasattr(self, 'spin_cam_offset_x'):
                self.spin_cam_offset_x.setValue(float(self.settings.value("cam_offset_x", 0.000)))
            if hasattr(self, 'spin_cam_offset_y'):
                self.spin_cam_offset_y.setValue(float(self.settings.value("cam_offset_y", 0.000)))

            if hasattr(self, 'cb_use_camera_offset'):
                state = 2 if self.cb_use_camera_offset.isChecked() else 0
                self.toggle_camera_fields_visibility(state)

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
            if hasattr(self, 'cb_use_camera_offset'): self.cb_use_camera_offset.blockSignals(False)
            if hasattr(self, 'spin_cam_offset_x'): self.spin_cam_offset_x.blockSignals(False)
            if hasattr(self, 'spin_cam_offset_y'): self.spin_cam_offset_y.blockSignals(False)

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
        if hasattr(self, 'cb_use_camera_offset'):
            self.settings.setValue("use_camera_offset", "true" if self.cb_use_camera_offset.isChecked() else "false")
        if hasattr(self, 'spin_cam_offset_x'):
            self.settings.setValue("cam_offset_x", self.spin_cam_offset_x.value())
        if hasattr(self, 'spin_cam_offset_y'):
            self.settings.setValue("cam_offset_y", self.spin_cam_offset_y.value())
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
            with open(gerber_path, 'r', encoding='utf-8', errors='ignore') as f:
                gerber_source = f.read()
            
            self.gerber_is_inches = "%MOIN%" in gerber_source

            processor = GerberProcessor()
            parser = GerberParser(processor)
            tokens = tokenize_gerber(gerber_source)
            parser.parse(tokens)

            parsed_geoms = [g for g in processor.geometries if not g.is_empty]

            if self.gerber_is_inches:
                self.current_gerber_geometry = [scale(g, xfact=25.4, yfact=25.4, origin=(0, 0)) for g in parsed_geoms]
            else:
                self.current_gerber_geometry = parsed_geoms

            if not self.current_gerber_geometry:
                raise ValueError("Файл не содержит графических векторов.")

            self.update_interactive_preview()
            self.view.fitInView(self.view.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        except Exception as e:
            print("\n" + "="*40 + " Ошибка загрузки Gerber " + "="*40)
            traceback.print_exc()
            print("="*105 + "\n")
            self.current_gerber_geometry = None
            self.status_label.setText(f"Ошибка загрузки Gerber: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def toggle_manual_calibration(self, state):
        """Включение/выключение режима центрального прицела и панели кнопок"""
        is_active = (state == 2)
        if hasattr(self, 'calib_group'):
            self.calib_group.setVisible(is_active)

        if hasattr(self, 'view'):
            self.view.calibration_mode = is_active
            if is_active:
                self.view.scrollContentsBy(0, 0)
            self.view.viewport().update()

        if not is_active:
            self.use_calibration = False
            for marker in self.manual_markers:
                if marker:
                    try: self.view.scene.removeItem(marker)
                    except: pass
            self.manual_file_pts = [None, None, None, None]
            self.manual_mach_pts = [None, None, None, None]
            self.manual_markers = [None, None, None, None]

            if hasattr(self, 'btn_pt1'): self.btn_pt1.setText("Зафиксировать Точку 1")
            if hasattr(self, 'btn_pt2'): self.btn_pt2.setText("Зафиксировать Точку 2")
            if hasattr(self, 'btn_pt3'): self.btn_pt3.setText("Зафиксировать Точку 3")
            if hasattr(self, 'btn_pt4'): self.btn_pt4.setText("Зафиксировать Точку 4")

            for btn in [self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4]:
                if hasattr(btn, 'setStyleSheet'): btn.setStyleSheet("")

            if self.current_gerber_geometry:
                self.update_interactive_preview()

    def toggle_pt4_active(self, state):
        """Включение/выключение использования опциональной 4-й точки"""
        is_active = (state == 2)
        self.btn_pt4.setEnabled(is_active)

        if not is_active:
            if self.manual_markers[3]:
                try: self.view.scene.removeItem(self.manual_markers[3])
                except: pass
            self.manual_file_pts[3] = None
            self.manual_mach_pts[3] = None
            self.manual_markers[3] = None
            self.btn_pt4.setText("Зафиксировать Точку 4")
            self.btn_pt4.setStyleSheet("")

            # Проверяем готовность первых 3 точек перед пересчетом (теперь без вылетов)
            if self.manual_file_pts and self.manual_mach_pts:
                if all(pt is not None for pt in self.manual_file_pts[:3]) and all(pt is not None for pt in self.manual_mach_pts[:3]):
                    self.calculate_manual_affine_matrix()

    def toggle_markers_visibility(self, state):
        """Динамически скрывает или показывает цветные точки-отметки на сцене"""
        is_visible = (state == 2)
        for marker in self.manual_markers:
            if marker:
                marker.setVisible(is_visible)
    def capture_point_in_crosshair(self, point_idx):
        """Мгновенно фиксирует координаты файла, находящиеся строго под центральным прицелом"""
        if not self.current_gerber_geometry:
            QtWidgets.QMessageBox.warning(self, "Внимание", "Сначала загрузите Gerber файл!")
            return

        buttons = [self.btn_pt1, self.btn_pt2, self.btn_pt3, self.btn_pt4]
        scene_x, scene_y = self.view.get_center_board_coordinates()

        transformed_elements = self.get_transformed_elements_raw()
        if not transformed_elements: return
        t_bounds = [g.bounds for g in transformed_elements]
        t_xmin = min([b[0] for b in t_bounds])

        # Вычисляем координату с учетом аппаратного положения экрана
        exact_file_x = scene_x + t_xmin
        exact_file_y = -scene_y

        if hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked():
            exact_file_x -= self.spin_cam_offset_x.value()
            exact_file_y -= self.spin_cam_offset_y.value()

        self.manual_file_pts[point_idx] = (exact_file_x, exact_file_y)
        if self.manual_markers[point_idx]:
            try: self.view.scene.removeItem(self.manual_markers[point_idx])
            except: pass

        colors = ["#ff1744", "#2979ff", "#00e676", "#e040fb"]
        marker = QtWidgets.QGraphicsEllipseItem(scene_x - 0.4, scene_y - 0.4, 0.8, 0.8)
        marker.setBrush(QtGui.QBrush(QtGui.QColor(colors[point_idx])))
        marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.15))

        if hasattr(self, 'cb_show_markers'):
            marker.setVisible(self.cb_show_markers.isChecked())

        self.view.scene.addItem(marker)
        self.manual_markers[point_idx] = marker
        self.status_label.setText(f"Статус: Точка {point_idx + 1} зафиксирована в прицеле.")
        self.status_label.setStyleSheet("color: #0288d1;")

        # Кастомный цифровой диалог ввода со стрелочками (без буфера обмена)
        try:
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(f"Координаты станка для Точки {point_idx + 1}")
            dialog.setMinimumWidth(340)
            dialog_layout = QtWidgets.QVBoxLayout(dialog)

            info_text = (
                f"Вы навели прицел на репер платы:\n"
                f"X: {exact_file_x:.3f}, Y: {exact_file_y:.3f}\n\n"
                f"Задайте точные координаты станка ЧПУ:"
            )
            dialog_layout.addWidget(QtWidgets.QLabel(info_text))

            grid = QtWidgets.QGridLayout()
            dialog_layout.addLayout(grid)

            grid.addWidget(QtWidgets.QLabel("Координата X станка (мм):"), 0, 0)
            spin_x = QtWidgets.QDoubleSpinBox()
            spin_x.setDecimals(3)
            spin_x.setRange(-9999.000, 9999.000)
            spin_x.setSingleStep(0.1)
            grid.addWidget(spin_x, 0, 1)

            grid.addWidget(QtWidgets.QLabel("Координата Y станка (мм):"), 1, 0)
            spin_y = QtWidgets.QDoubleSpinBox()
            spin_y.setDecimals(3)
            spin_y.setRange(-9999.000, 9999.000)
            spin_y.setSingleStep(0.1)
            grid.addWidget(spin_y, 1, 1)

            spin_x.setValue(exact_file_x)
            spin_y.setValue(exact_file_y)

            button_box = QtWidgets.QDialogButtonBox(
                QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog
            )
            button_box.accepted.connect(dialog.accept)
            button_box.rejected.connect(dialog.reject)
            dialog_layout.addWidget(button_box)

            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                mach_x = spin_x.value()
                mach_y = spin_y.value()

                self.manual_mach_pts[point_idx] = (mach_x, mach_y)
                buttons[point_idx].setText(f"Т{point_idx + 1}: Файл({exact_file_x:.1f}, {exact_file_y:.1f}) -> Ст({mach_x:.1f}, {mach_y:.1f})")
                buttons[point_idx].setStyleSheet("background-color: #c8e6c9; font-weight: bold;")

                p1_3_ready = all(pt is not None for pt in self.manual_file_pts[:3]) and all(pt is not None for pt in self.manual_mach_pts[:3])
                p4_enabled = self.cb_use_pt4.isChecked()
                p4_ready = self.manual_file_pts is not None and self.manual_mach_pts is not None if p4_enabled else True

                if p1_3_ready and p4_ready:
                    self.calculate_manual_affine_matrix()
            else:
                if self.manual_markers[point_idx]:
                    try: self.view.scene.removeItem(self.manual_markers[point_idx])
                    except: pass
                self.manual_file_pts[point_idx] = None
                self.manual_markers[point_idx] = None
                buttons[point_idx].setText(f"Зафиксировать Точку {point_idx + 1}")
                buttons[point_idx].setStyleSheet("")
        except Exception as e:
            print("\n" + "="*40 + " Сбой работы окна ввода " + "="*40)
            traceback.print_exc()
            print("="*105 + "\n")
            QtWidgets.QMessageBox.critical(self, "Ошибка", f"Сбой работы окна ввода: {str(e)}. Сброшено.")

    def calculate_manual_affine_matrix(self):
        """Расчет аффинной матрицы, автоматически адаптирующийся под 3 или 4 точки"""

        try:
            valid_indices = [i for i in range(4) if self.manual_file_pts[i] is not None and self.manual_mach_pts[i] is not None]
            if len(valid_indices) < 3: return

            # Извлекаем чистые float-координаты из исходного Gerber-файла
            x_f = [float(self.manual_file_pts[i][0]) for i in valid_indices]
            y_f = [float(self.manual_file_pts[i][1]) for i in valid_indices]

            # Читаем постоянные физические смещения камеры из полей ввода
            cam_x = self.spin_cam_offset_x.value() if hasattr(self, 'spin_cam_offset_x') else 0.0
            cam_y = self.spin_cam_offset_y.value() if hasattr(self, 'spin_cam_offset_y') else 0.0

            # ВАЖНО: Пересчитываем координаты станка из положения "под камерой"
            # в положение "под лазерным лучом" прямо перед расчетом матрицы!
            X_m = [float(self.manual_mach_pts[i][0]) + cam_x for i in valid_indices]
            Y_m = [float(self.manual_mach_pts[i][1]) + cam_y for i in valid_indices]

            A = np.zeros((len(valid_indices), 3))
            for idx in range(len(valid_indices)):
                A[idx] = [x_f[idx], y_f[idx], 1]

            res_x = np.linalg.lstsq(A, X_m, rcond=None)
            res_y = np.linalg.lstsq(A, Y_m, rcond=None)

            a, b, x_off = res_x[0]
            d, e, y_off = res_y[0]

            self.matrix_coeffs = (float(a), float(b), float(d), float(e), float(x_off), float(y_off))
            self.use_calibration = True

            pts_count = len(valid_indices)
            self.status_label.setText(f"Статус: Базирование выполнено успешно по {pts_count} точкам!")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")

            for marker in self.manual_markers:
                if marker:
                    try: self.view.scene.removeItem(marker)
                    except: pass
            self.manual_markers = [None, None, None, None]

            self.update_interactive_preview()
        except Exception as e:
            print("\n" + "="*40 + " ОШИБКА АФФИННОЙ МАТРИЦЫ " + "="*40)
            traceback.print_exc()
            print("="*105 + "\n")
            QtWidgets.QMessageBox.critical(self, "Ошибка расчета", f"Не удалось рассчитать коэффициенты трансформации: {str(e)}")
            self.use_calibration = False

    def get_transformed_elements_raw(self):
        """Возвращает базовую геометрию платы с учетом ползунков и ЖИВОГО сдвига камеры"""
        if not self.current_gerber_geometry: return []
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

            if hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked():
                cam_x = self.spin_cam_offset_x.value()
                cam_y = self.spin_cam_offset_y.value()
                geom = translate(geom, xoff=cam_x, yoff=cam_y)

            transformed.append(geom)
        return transformed
    def get_transformed_elements(self):
        """Основной метод: трансформирует геометрию под ручные ползунки или точную матрицу пятачков"""
        if not self.current_gerber_geometry: 
            return []

        has_calib_btn = hasattr(self, 'cb_enable_calib') and self.cb_enable_calib.isChecked()

        if has_calib_btn and hasattr(self, 'use_calibration') and self.use_calibration:
            transformed = []
            for geom in self.current_gerber_geometry:
                t_geom = affine_transform(geom, self.matrix_coeffs)
                transformed.append(t_geom)
            return transformed

        return self.get_transformed_elements_raw()

    def update_interactive_preview(self):
        if not self.current_gerber_geometry: return
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

            # Получаем чистые числовые значения смещения камеры для расширения поля
            cam_x = self.spin_cam_offset_x.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0
            cam_y = self.spin_cam_offset_y.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0

            def add_shapely_to_qt_path(g_item):
                if g_item.is_empty: return
                if g_item.geom_type == 'Polygon':
                    x_ext, y_ext = g_item.exterior.xy
                    poly_path = QtGui.QPainterPath()
                    poly_path.moveTo(x_ext[0] - t_xmin + overscan + cam_x, -(y_ext[0] - t_ymin + cam_y))
                    for x, y in zip(x_ext[1:], y_ext[1:]):
                        poly_path.lineTo(x - t_xmin + overscan + cam_x, -(y - t_ymin + cam_y))
                    poly_path.closeSubpath()

                    for interior in g_item.interiors:
                        x_int, y_int = interior.xy
                        int_path = QtGui.QPainterPath()
                        int_path.moveTo(x_int[0] - t_xmin + overscan + cam_x, -(y_int[0] - t_ymin + cam_y))
                        for x, y in zip(x_int[1:], y_int[1:]):
                            int_path.lineTo(x - t_xmin + overscan + cam_x, -(y - t_ymin + cam_y))
                        int_path.closeSubpath()
                        poly_path = poly_path.subtracted(int_path)
                    qt_path.addPath(poly_path)

                elif g_item.geom_type in ['MultiPolygon', 'GeometryCollection']:
                    for sub_geom in g_item.geoms:
                        add_shapely_to_qt_path(sub_geom)

                elif g_item.geom_type in ['LineString', 'LinearRing']:
                    x_l, y_l = g_item.xy
                    line_path = QtGui.QPainterPath()
                    line_path.moveTo(x_l[0] - t_xmin + overscan + cam_x, -(y_l[0] - t_ymin + cam_y))
                    for x, y in zip(x_l[1:], y_l[1:]):
                        line_path.lineTo(x - t_xmin + overscan + cam_x, -(y - t_ymin + cam_y))
                    qt_path.addPath(line_path)

            for geom in transformed_elements:
                add_shapely_to_qt_path(geom)

            w = t_xmax - t_xmin + (2 * overscan)
            h = t_ymax - t_ymin
            if w <= 0 or h <= 0: return
            scene_left = min(-50.0, -50.0 + cam_x)
            scene_right = max(w + 50.0, w + 50.0 + cam_x)
            scene_top = min(-h - 50.0, -h - 50.0 - cam_y)
            scene_bottom = max(50.0, 50.0 - cam_y)

            # Задаем новые свободные границы для бесконечного пролистывания и зума
            self.view.scene.setSceneRect(
                scene_left,
                scene_top,
                scene_right - scene_left,
                scene_bottom - scene_top
            )

            path_item = QtWidgets.QGraphicsPathItem(qt_path)
            path_item.setCacheMode(QtWidgets.QGraphicsItem.CacheMode.DeviceCoordinateCache)

            if invert_mode:
                bg_rect = QtWidgets.QGraphicsRectItem(cam_x, -(h + cam_y), w, h)
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

            # --- ЧИСТЫЙ ВЫВОД МАРКЕРОВ БЕЗ ИСКУССТВЕННЫХ СДВИГОВ ---
            if hasattr(self, 'cb_enable_calib') and self.cb_enable_calib.isChecked():
                colors = ["#ff1744", "#2979ff", "#00e676", "#e040fb"]
                for idx, pt in enumerate(self.manual_file_pts):
                    if pt is not None:
                        file_x, file_y = pt

                        if hasattr(self, 'use_calibration') and self.use_calibration and self.matrix_coeffs:
                            # РЕЖИМ 1 (КАЛИБРОВАННЫЙ): Матрица выдает идеальные координаты станка ЧПУ
                            a, b, d, e, x_off, y_off = self.matrix_coeffs
                            sc_x = a * file_x + b * file_y + x_off
                            sc_y = d * file_x + e * file_y + y_off

                            # Приводим к экранной сетке GraphicsView (просто центрируем и инвертируем Y)
                            sc_x = sc_x - t_xmin
                            sc_y = -sc_y
                        else:
                            # РЕЖИМ 2 (СЫРОЙ): До ввода 3-й точки
                            sc_x = file_x - t_xmin
                            sc_y = -file_y

                        m_item = QtWidgets.QGraphicsEllipseItem(sc_x - 0.4, sc_y - 0.4, 0.8, 0.8)
                        m_item.setBrush(QtGui.QBrush(QtGui.QColor(colors[idx])))
                        m_item.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 0.15))

                        if hasattr(self, 'cb_show_markers'):
                            m_item.setVisible(self.cb_show_markers.isChecked())

                        self.view.scene.addItem(m_item)
                        self.manual_markers[idx] = m_item

            self.status_label.setText((
                f"Статус: Геометрия готова.\n"
                f"Размер из файла: {(t_xmax - t_xmin):.2f} x {h:.2f} мм\n"
                f"Размер гравировки: {(w + cam_x):.2f} x {(h+cam_y):.2f} мм"
            ))
            self.status_label.setStyleSheet("color: #2e7d32;")
        except Exception as e:
            print("\n" + "="*40 + " ОШИБКА ВИЗУАЛИЗАЦИИ ИЛС " + "="*40)
            traceback.print_exc()
            print("="*105 + "\n")
            self.status_label.setText(f"Ошибка визуализации: {str(e)}")
            self.status_label.setStyleSheet("color: red;")

    def process_conversion(self):
        if not self.current_gerber_geometry: return
        self.save_current_settings()

        self.view.calibration_mode = False
        self.view.overlay.update()

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

            raw_xmin = float(min([b[0] for b in t_bounds]))
            raw_ymin = float(min([b[1] for b in t_bounds]))
            raw_xmax = float(max([b[2] for b in t_bounds]))
            raw_ymax = float(max([b[3] for b in t_bounds]))

            # Локальная система координат от нуля станка
            xmin, ymin = 0.0, 0.0
            xmax = float(raw_xmax - raw_xmin + (2 * overscan))
            ymax = float(raw_ymax - raw_ymin)

            # Читаем физический сдвиг камеры из полей ввода для правильного смещения в G-коде
            cam_off_x = self.spin_cam_offset_x.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0
            cam_off_y = self.spin_cam_offset_y.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0

            moved_geometries = []
            if invert_mode:
                bounding_box = Polygon([(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)])
                final_mask = bounding_box
                for geom in transformed_elements:
                    # ИСПРАВЛЕНО: Сдвигаем каждый элемент на чистое float-число
                    shifted_geom = translate(geom, xoff=(-raw_xmin + overscan), yoff=-raw_ymin)
                    final_mask = final_mask.difference(shifted_geom)
                moved_geometries.append(final_mask)
            else:
                merged_pads = unary_union([translate(geom, xoff=(-raw_xmin + overscan), yoff=-raw_ymin) for geom in transformed_elements])
                moved_geometries.append(merged_pads)

            # Читаем физический сдвиг камеры из полей ввода для правильного смещения в G-коде
            cam_off_x = self.spin_cam_offset_x.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0
            cam_off_y = self.spin_cam_offset_y.value() if (hasattr(self, 'cb_use_camera_offset') and self.cb_use_camera_offset.isChecked()) else 0.0

            gcode = []
            gcode.append("; Gerber -> LaserGRBL GCode (Symmetric Half-Overscan Real-Space Engine)")
            gcode.append(f"G21 ;\nG90 ;")

            # --- БЛОК ТЕСТОВОГО ОБХОДА КОНТУРА ПЛАТЫ ---
            preview_power = 10 if power > 100 else 2
            # Указка обводит синий контур маски платы (от xmin до xmax с учетом камеры)
            gcode.append(f"G0 X{(xmin + cam_off_x):.4f} Y{(ymin + cam_off_y):.4f} F{feedrate}")
            gcode.append(f"M3 S{preview_power} ;")
            gcode.append(f"G1 X{(xmax + cam_off_x):.4f} Y{(ymin + cam_off_y):.4f} F{feedrate}")
            gcode.append(f"G1 X{(xmax + cam_off_x):.4f} Y{(ymax + cam_off_y):.4f}")
            gcode.append(f"G1 X{(xmin + cam_off_x):.4f} Y{(ymax + cam_off_y):.4f}")
            gcode.append(f"G1 X{(xmin + cam_off_x):.4f} Y{(ymin + cam_off_y):.4f}")
            gcode.append("M5\nG4 P0.5\nM0 ;")

            gcode.append(f"{selected_mode_txt} S0\nG1 F{feedrate}")

            # УЧИТЫВАЕМ КАМЕРУ: Сдвигаем начальную точку выхода лазера
            gcode.append(f"G0 X{(0.0000 + cam_off_x):.4f} Y{(ymin + (step / 2.0) + cam_off_y):.4f} F{feedrate}")

            lines_count = int(math.ceil((ymax - ymin) / step))
            if lines_count <= 0: lines_count = 1

            direction_right = True
            blue_laser_path = QtGui.QPainterPath()
            red_overscan_path = QtGui.QPainterPath()

            for line_idx in range(lines_count):
                current_y = ymin + (line_idx * step) + (step / 2.0)
                if current_y > ymax: current_y = ymax

                if line_idx % 200 == 0:
                    self.status_label.setText(f"Расчет: строка {line_idx} из {lines_count}...")
                    QtWidgets.QApplication.processEvents()

                scan_line = LineString([(xmin - 1.0, current_y), (xmax + 1.0, current_y)])
                segments_coords = []

                for target_geom in moved_geometries:
                    laser_on_segments = scan_line.intersection(target_geom)
                    if not laser_on_segments.is_empty:
                        geoms_to_process = list(laser_on_segments.geoms) if laser_on_segments.geom_type in ['MultiLineString', 'GeometryCollection'] else [laser_on_segments]
                        for g in geoms_to_process:
                            if g.geom_type in ['LineString', 'LinearRing']:
                                start_x = float(g.coords[0][0])
                                end_x = float(g.coords[-1][0])
                                segments_coords.append((start_x, end_x))
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
                    # Прибавляем офсеты камеры, чтобы синие линии на экране сместились вслед за G-кодом
                    blue_laser_path.moveTo(s_x + cam_off_x, -(current_y + cam_off_y))
                    blue_laser_path.lineTo(e_x + cam_off_x, -(current_y + cam_off_y))

                # УЧИТЫВАЕМ КАМЕРУ: Прибавляем cam_off_x к X и cam_off_y к Y во всех командах перемещения станка
                if direction_right or not snake_mode:
                    segments_coords.sort(key=lambda val: val)
                    gcode.append(f"G0 X{(line_start_x + cam_off_x):.4f} Y{(current_y + cam_off_y):.4f}")
                    red_overscan_path.moveTo(line_start_x + cam_off_x, -(current_y + cam_off_y))

                    last_x = line_start_x
                    for start_x, end_x in segments_coords:
                        if start_x > last_x: 
                            gcode.append(f"G1 X{(start_x + cam_off_x):.4f} S0")
                            red_overscan_path.lineTo(start_x + cam_off_x, -(current_y + cam_off_y))
                        gcode.append(f"G1 X{(end_x + cam_off_x):.4f} S{power}")
                        red_overscan_path.moveTo(end_x + cam_off_x, -(current_y + cam_off_y))
                        last_x = end_x
                    if last_x < line_end_x: 
                        gcode.append(f"G1 X{(line_end_x + cam_off_x):.4f} S0")
                        red_overscan_path.lineTo(line_end_x + cam_off_x, -(current_y + cam_off_y))
                else:
                    segments_coords.sort(key=lambda val: val, reverse=True)
                    gcode.append(f"G0 X{(line_end_x + cam_off_x):.4f} Y{(current_y + cam_off_y):.4f}")
                    red_overscan_path.moveTo(line_end_x + cam_off_x, -(current_y + cam_off_y))

                    last_x = line_end_x
                    for start_x, end_x in segments_coords:
                        if end_x < last_x: 
                            gcode.append(f"G1 X{(end_x + cam_off_x):.4f} S0")
                            red_overscan_path.lineTo(end_x + cam_off_x, -(current_y + cam_off_y))
                        gcode.append(f"G1 X{(start_x + cam_off_x):.4f} S{power}")
                        red_overscan_path.moveTo(start_x + cam_off_x, -(current_y + cam_off_y))
                        last_x = start_x
                    if last_x > line_start_x: 
                        gcode.append(f"G1 X{(line_start_x + cam_off_x):.4f} S0")
                        red_overscan_path.lineTo(line_start_x + cam_off_x, -(current_y + cam_off_y))

                if snake_mode: 
                    direction_right = not direction_right

            # Финальный возврат в ноль станка с учетом смещения камеры
            gcode.append(f"M5\nG0 X{cam_off_x:.4f} Y{cam_off_y:.4f}\nM2")
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
            print("\n" + "="*40 + " Ошибка вычислений " + "="*40)
            traceback.print_exc()
            print("="*115 + "\n")
            self.status_label.setText(f"Ошибка вычислений: {str(e)} (Подробности в консоли)")
            self.status_label.setStyleSheet("color: red;")
            self.btn_save.setDisabled(True)

    def save_gcode_dialog(self):
        if self.generated_gcode is None: return
        gerber_path = self.entry_path.text().strip()
        # ИСПРАВЛЕНО: Безопасное разделение расширения файла кортежа через [0]
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
