#! python3  # noqa: E265

"""
The global-or-workspace scope that styles and layer groups share.

Rows are display strings, so the global scope needs a label the user can read;
`scope()` maps it back to the `None` the library wants. Three modules use it
(the two tabs and the main dialog's workspace-link cells), which is the point
at which docs/development/conventions.md says a shared helper stops living inside one tab.
"""

from qgis.PyQt.QtCore import QCoreApplication

# The value rows and combos carry; global_label() is what they show.
GLOBAL = "(global)"

# A detail cell not fetched yet: tabs list names at once and the summary
# columns of the visible page follow. Shown as it is. Here, not
# in dlg_main, because the tabs need it and dlg_main imports them.
PENDING = "…"


def scope(workspace_label):
    """Workspace name for the API, or None for the global scope."""
    return None if workspace_label in ("", GLOBAL) else workspace_label


def global_label():
    """The global scope as the user reads it, in their language."""
    return QCoreApplication.translate("Scope", "(global)")
