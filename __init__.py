# -*- coding: utf-8 -*-
"""
Ocultar Puntos por Área
------------------------
Punto de entrada del plugin. QGIS llama a classFactory() al cargar el
complemento, pasándole la instancia de QgisInterface (iface).
"""


def classFactory(iface):
    """Carga la clase principal del plugin.

    :param iface: Interfaz de QGIS que se le pasará a la clase creada.
    :type iface: QgsInterface
    """
    from .main import OcultarPuntosPorArea
    return OcultarPuntosPorArea(iface)
