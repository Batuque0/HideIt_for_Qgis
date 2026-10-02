# -*- coding: utf-8 -*-
"""
Ocultar Puntos por Área
------------------------
Plugin de QGIS (3.x) que permite alternar (mostrar/ocultar) puntos de la
capa vectorial activa dibujando un rectángulo con el mouse: los puntos
visibles dentro del área se marcan para ocultar y los que ya estaban
ocultos se marcan para volver a mostrarse. Usa un filtro de capa
(subset string), sin borrar las entidades físicamente. Incluye además
una acción para restaurar toda la visibilidad de una sola vez.
"""

import os
import re

from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.PyQt.QtGui import QColor, QCursor, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox

PLUGIN_DIR = os.path.dirname(__file__)
ICON_SELECCIONAR = os.path.join(PLUGIN_DIR, "icons", "icon_seleccionar.png")
ICON_RESTAURAR = os.path.join(PLUGIN_DIR, "icons", "icon_restaurar.png")

from qgis.core import (
    QgsWkbTypes,
    QgsPointXY,
    QgsRectangle,
    QgsFeatureRequest,
    QgsCoordinateTransform,
    QgsProject,
    Qgis,
)
from qgis.gui import QgsMapTool, QgsRubberBand


class RectangleSelectMapTool(QgsMapTool):
    """Herramienta de mapa personalizada que dibuja un rectángulo de
    arrastre (rubber band) semitransparente y, al soltar el clic,
    entrega la extensión (QgsRectangle) resultante mediante un callback.
    """

    def __init__(self, canvas, on_rectangle_ready):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_rectangle_ready = on_rectangle_ready

        self.start_point = None
        self.is_dragging = False

        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0, 80))       # relleno semitransparente
        self.rubber_band.setStrokeColor(QColor(255, 0, 0, 200))
        self.rubber_band.setWidth(2)

        self.setCursor(QCursor(Qt.CrossCursor))

    # -- Eventos del mouse -------------------------------------------------

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.start_point = self.toMapCoordinates(event.pos())
        self.is_dragging = True
        self._reset_rubber_band()

    def canvasMoveEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        current_point = self.toMapCoordinates(event.pos())
        self._update_rubber_band(self.start_point, current_point)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.is_dragging:
            return
        self.is_dragging = False
        end_point = self.toMapCoordinates(event.pos())

        rect = QgsRectangle(self.start_point, end_point)
        rect.normalize()

        self._reset_rubber_band()
        self.start_point = None

        # Ignora clics/arrastres degenerados (sin área)
        if rect.width() == 0 and rect.height() == 0:
            return

        self.on_rectangle_ready(rect)

    def deactivate(self):
        self._reset_rubber_band()
        self.is_dragging = False
        self.start_point = None
        super().deactivate()

    # -- Utilidades ----------------------------------------------------------

    def _reset_rubber_band(self):
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def _update_rubber_band(self, start, end):
        points = [
            QgsPointXY(start.x(), start.y()),
            QgsPointXY(end.x(), start.y()),
            QgsPointXY(end.x(), end.y()),
            QgsPointXY(start.x(), end.y()),
        ]
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for point in points:
            self.rubber_band.addPoint(point, False)
        self.rubber_band.closePoints(True)
        self.rubber_band.show()


class OcultarPuntosPorArea:
    """Clase principal del plugin: registra las acciones, la barra de
    herramientas y contiene la lógica de ocultamiento/restauración."""

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()

        self.toolbar = None
        self.action_select = None
        self.action_restore = None

        self.map_tool = None
        self.previous_map_tool = None

        # Estado de la sesión de selección activa: copia de trabajo de
        # los IDs actualmente ocultos, que se va alternando (toggle) a
        # medida que se dibujan rectángulos, y se vuelca al filtro
        # recién al presionar "Listo".
        self.session_hidden_ids = set()

    # ------------------------------------------------------------------
    # Ciclo de vida del plugin
    # ------------------------------------------------------------------

    def initGui(self):
        """Crea la barra de herramientas y las acciones del plugin."""
        self.toolbar = self.iface.addToolBar("Ocultar Puntos")
        self.toolbar.setObjectName("OcultarPuntosToolbar")
        # Solo ícono, sin texto debajo/al lado (igual que el resto de
        # las barras de herramientas nativas de QGIS).
        self.toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)

        self.action_select = QAction(
            QIcon(ICON_SELECCIONAR), "Seleccionar para Ocultar", self.iface.mainWindow()
        )
        self.action_select.setCheckable(True)
        self.action_select.setToolTip(
            "Seleccionar para Ocultar / Mostrar\n"
            "Dibujá un rectángulo: las entidades visibles (puntos, líneas "
            "o polígonos) se ocultan y las ya ocultas se vuelven a mostrar."
        )
        self.action_select.triggered.connect(self.activate_select_tool)

        self.action_restore = QAction(
            QIcon(ICON_RESTAURAR), "Mostrar / Restaurar Todo", self.iface.mainWindow()
        )
        self.action_restore.setToolTip(
            "Mostrar / Restaurar Todo\n"
            "Elimina el filtro de la capa activa y muestra todas las entidades."
        )
        self.action_restore.triggered.connect(self.restore_all)

        self.toolbar.addAction(self.action_select)
        self.toolbar.addAction(self.action_restore)

        # Si el usuario cambia de herramienta de mapa manualmente,
        # destildamos el botón de selección.
        self.canvas.mapToolSet.connect(self._on_map_tool_changed)

    def unload(self):
        """Limpieza al desinstalar/desactivar el plugin."""
        try:
            self.canvas.mapToolSet.disconnect(self._on_map_tool_changed)
        except TypeError:
            pass

        if self.map_tool is not None:
            self.map_tool.deactivate()
            self.map_tool = None

        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None

        self.action_select = None
        self.action_restore = None

    # ------------------------------------------------------------------
    # Selección / ocultamiento
    # ------------------------------------------------------------------

    def activate_select_tool(self, checked):
        layer = self.iface.activeLayer()

        if not checked:
            # El usuario destildó el botón manualmente: desactivar.
            self._deactivate_select_tool(restore_previous=True)
            return

        if layer is None or layer.type() != layer.VectorLayer:
            self._warn("Seleccioná una capa vectorial activa antes de continuar.")
            self.action_select.setChecked(False)
            return

        if layer.geometryType() == QgsWkbTypes.NullGeometry:
            self._warn("La capa activa no tiene geometría (es una tabla).")
            self.action_select.setChecked(False)
            return

        self.session_hidden_ids = self._extract_ids_from_subset_string(layer.subsetString())
        self.previous_map_tool = self.canvas.mapTool()

        self.map_tool = RectangleSelectMapTool(self.canvas, self._on_rectangle_drawn)
        self.canvas.setMapTool(self.map_tool)

        self.iface.messageBar().pushMessage(
            "Ocultar Puntos por Área",
            "Dibujá un rectángulo: las entidades visibles se marcan para "
            "ocultar y las ya ocultas se marcan para volver a mostrar.",
            level=Qgis.Info,
            duration=5,
        )

    def _on_rectangle_drawn(self, rect):
        layer = self.iface.activeLayer()
        if layer is None:
            return

        layer_rect = self._transform_rect_to_layer_crs(rect, layer)
        ids_in_rect = self._get_ids_in_rect_ignoring_filter(layer, layer_rect)

        if not ids_in_rect:
            self._info("No se encontraron entidades dentro del área dibujada.")
            return  # la herramienta sigue activa para reintentar

        # Toggle: lo que ya está oculto pasa a "mostrar", lo que está
        # visible pasa a "ocultar".
        ids_to_hide = [i for i in ids_in_rect if i not in self.session_hidden_ids]
        ids_to_show = [i for i in ids_in_rect if i in self.session_hidden_ids]

        respuesta = self._ask_add_done_cancel(len(ids_to_hide), len(ids_to_show))

        if respuesta == "cancelar":
            # Descarta la selección del último rectángulo, sin cambiar nada.
            return

        for fid in ids_to_hide:
            self.session_hidden_ids.add(fid)
        for fid in ids_to_show:
            self.session_hidden_ids.discard(fid)

        if respuesta == "anadir":
            self.iface.messageBar().pushMessage(
                "Ocultar Puntos por Área",
                f"{len(self.session_hidden_ids)} entidad(es) marcada(s) como "
                f"ocultas hasta el momento. Seguí dibujando o presioná "
                f"'Listo' desde el próximo cuadro.",
                level=Qgis.Info,
                duration=4,
            )
            return  # sigue en modo dibujo

        if respuesta == "listo":
            self._apply_session_filter(layer, self.session_hidden_ids)
            self._deactivate_select_tool(restore_previous=True)

    def _get_ids_in_rect_ignoring_filter(self, layer, layer_rect):
        """Busca las entidades que intersectan el rectángulo ignorando
        temporalmente el filtro (subset string) de la capa, para poder
        detectar tanto entidades visibles como ya ocultas. Funciona
        igual para puntos, líneas o polígonos: el intersect exacto
        (ExactIntersect) evalúa la geometría real contra el rectángulo,
        no solo su caja delimitadora."""
        original_subset = layer.subsetString()
        if original_subset:
            layer.setSubsetString("")
        try:
            request = QgsFeatureRequest()
            request.setFilterRect(layer_rect)
            request.setFlags(QgsFeatureRequest.ExactIntersect)
            return [f.id() for f in layer.getFeatures(request)]
        finally:
            if original_subset:
                layer.setSubsetString(original_subset)

    def _ask_add_done_cancel(self, count_to_hide, count_to_show):
        """Muestra el QMessageBox con los tres botones explícitos y
        devuelve 'anadir', 'listo' o 'cancelar'."""
        box = QMessageBox(self.iface.mainWindow())
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Ocultar Puntos por Área")

        partes = []
        if count_to_hide:
            partes.append(f"{count_to_hide} entidad(es) para OCULTAR")
        if count_to_show:
            partes.append(f"{count_to_show} entidad(es) YA OCULTAS para volver a MOSTRAR")
        resumen = " y ".join(partes)

        box.setText(
            f"En el área seleccionada hay {resumen}.\n\n"
            "• Añadir: aplica este cambio y permite seguir dibujando más áreas.\n"
            "• Listo: aplica este cambio y actualiza la capa.\n"
            "• Cancelar: descarta esta selección sin cambiar nada."
        )

        btn_add = box.addButton("Añadir", QMessageBox.ActionRole)
        btn_done = box.addButton("Listo", QMessageBox.AcceptRole)
        btn_cancel = box.addButton("Cancelar", QMessageBox.RejectRole)
        box.setDefaultButton(btn_done)

        box.exec_()
        clicked = box.clickedButton()

        if clicked == btn_add:
            return "anadir"
        if clicked == btn_done:
            return "listo"
        return "cancelar"

    @staticmethod
    def _id_field_token(layer):
        """Devuelve el pseudo-campo de ID adecuado según el proveedor de
        la capa. Las capas basadas en OGR/GDAL (KML, Shapefile, GeoJSON,
        GeoPackage, etc.) usan su propio dialecto SQL y reconocen 'FID',
        no la sintaxis de expresiones de QGIS ('$id')."""
        provider_name = layer.dataProvider().name().lower()
        if provider_name == "ogr":
            return "FID"
        return "$id"

    def _apply_session_filter(self, layer, hidden_ids):
        """Aplica como filtro final el conjunto completo de IDs que deben
        quedar ocultos tras la sesión de selección (ya incluye tanto lo
        que ya estaba oculto como los cambios hechos por toggle)."""
        if not hidden_ids:
            layer.setSubsetString("")
        else:
            ids_str = ", ".join(str(fid) for fid in sorted(hidden_ids))
            token = self._id_field_token(layer)
            success = layer.setSubsetString(f"{token} NOT IN ({ids_str})")

            if not success:
                # El proveedor no siguió la convención esperada: se
                # reintenta con el token alternativo antes de rendirse.
                alt_token = "FID" if token == "$id" else "$id"
                success = layer.setSubsetString(f"{alt_token} NOT IN ({ids_str})")

            if not success:
                self._warn(
                    "No se pudo aplicar el filtro: el proveedor de esta "
                    "capa no admite este tipo de filtro."
                )
                return

        layer.triggerRepaint()
        self.canvas.refresh()

        self.iface.messageBar().pushMessage(
            "Ocultar Puntos por Área",
            f"La capa '{layer.name()}' quedó con {len(hidden_ids)} entidad(es) "
            f"oculta(s) en total.",
            level=Qgis.Success,
            duration=4,
        )

    @staticmethod
    def _extract_ids_from_subset_string(subset_string):
        """Extrae los IDs numéricos de un filtro previo del tipo
        'FID NOT IN (1, 2, 3)' o '$id NOT IN (1, 2, 3)'. Devuelve un
        set vacío si no coincide con ninguno de los dos formatos."""
        if not subset_string:
            return set()

        match = re.search(
            r"(?:\$id|FID)\s+NOT\s+IN\s*\(([^)]*)\)", subset_string, re.IGNORECASE
        )
        if not match:
            return set()

        ids = set()
        for token in match.group(1).split(","):
            token = token.strip()
            if token:
                try:
                    ids.add(int(token))
                except ValueError:
                    continue
        return ids

    def _transform_rect_to_layer_crs(self, rect, layer):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()

        if canvas_crs == layer_crs:
            return rect

        transform = QgsCoordinateTransform(
            canvas_crs, layer_crs, QgsProject.instance()
        )
        try:
            return transform.transformBoundingBox(rect)
        except Exception:
            # Si la transformación falla (p. ej. capa sin CRS válido),
            # se usa el rectángulo original como respaldo.
            return rect

    def _deactivate_select_tool(self, restore_previous=False):
        self.session_hidden_ids = set()

        if self.map_tool is not None:
            self.map_tool.deactivate()
            self.map_tool = None

        if self.action_select is not None:
            self.action_select.blockSignals(True)
            self.action_select.setChecked(False)
            self.action_select.blockSignals(False)

        if restore_previous and self.previous_map_tool is not None:
            self.canvas.setMapTool(self.previous_map_tool)
        self.previous_map_tool = None

    def _on_map_tool_changed(self, new_tool, old_tool):
        # Si el usuario activó otra herramienta distinta de la nuestra
        # mientras estábamos seleccionando, destildamos el botón.
        if old_tool is self.map_tool and new_tool is not self.map_tool:
            self.map_tool = None
            if self.action_select is not None:
                self.action_select.blockSignals(True)
                self.action_select.setChecked(False)
                self.action_select.blockSignals(False)

    # ------------------------------------------------------------------
    # Restaurar
    # ------------------------------------------------------------------

    def restore_all(self):
        layer = self.iface.activeLayer()

        if layer is None or layer.type() != layer.VectorLayer:
            self._warn("Seleccioná una capa vectorial activa para restaurar.")
            return

        layer.setSubsetString("")
        layer.triggerRepaint()
        self.canvas.refresh()

        self.iface.messageBar().pushMessage(
            "Ocultar Puntos por Área",
            f"Se restauraron todas las entidades de la capa '{layer.name()}'.",
            level=Qgis.Success,
            duration=4,
        )

    # ------------------------------------------------------------------
    # Helpers de UI
    # ------------------------------------------------------------------

    def _warn(self, text):
        self.iface.messageBar().pushMessage(
            "Ocultar Puntos por Área", text, level=Qgis.Warning, duration=5
        )

    def _info(self, text):
        self.iface.messageBar().pushMessage(
            "Ocultar Puntos por Área", text, level=Qgis.Info, duration=4
        )
