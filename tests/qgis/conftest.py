"""pytest bootstrap for the QGIS suite.

Starts the QGIS application once. The package's `__init__` puts the bundled
wheels on sys.path, under pytest and unittest alike.
"""

from qgis.testing import start_app

start_app()
