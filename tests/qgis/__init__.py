"""The bundled wheels go on sys.path here, so pytest and unittest both import them."""

import sys

from geoserver_manager.toolbelt.dependencies import BUNDLED_WHLS

for _whl in BUNDLED_WHLS:
    if str(_whl) not in sys.path:
        sys.path.insert(0, str(_whl))
