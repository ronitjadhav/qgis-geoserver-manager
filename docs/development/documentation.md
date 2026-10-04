# Documentation

This site is [Sphinx](https://www.sphinx-doc.org/) with the
[Furo](https://pradyunsg.me/furo/) theme, written in Markdown through the
[MyST parser](https://myst-parser.readthedocs.io/). The pages live in `docs/`,
the configuration in `docs/conf.py`.

The [architecture](architecture.md), [invariants](invariants.md) and
[conventions](conventions.md) pages are the contributor's map of the code.
Read them before you change anything. There is no API reference generated
from docstrings: the plugin cannot be imported without QGIS, and the
documentation job does not install QGIS. Most pages are written by hand. The
changelog and the code of conduct are included from the repository root. The
[icon style guide](icon-style-guide.md) is maintained by hand. Icon previews
are optional local build output and are not part of the site.

## Build it

```bash
python -m pip install -U -r requirements/documentation.txt

sphinx-build -b html -d docs/_build/cache -j auto -q docs docs/_build/html
```

Open `docs/_build/html/index.html`. While you write, let the site rebuild on
save:

```bash
sphinx-autobuild -b html docs/ docs/_build/html
```

Then open <http://127.0.0.1:8000>.

A push to `main` builds the site and deploys it to GitHub Pages. The same job
publishes `plugins.xml`, the feed that makes every commit installable from
inside QGIS.

The build must stay silent: `sphinx-build -b html -q docs docs/_build/html`
prints nothing when the site is healthy. A warning means something is broken,
such as a link to a page that moved or a page no listed page points at. CI
builds with `-W`, so a warning fails the documentation job.

## Keep it current

**Documentation is part of the change, not a follow-up.** A pull request that
changes what the user sees also updates the pages that describe it. What to
update:

| What changed | What to update |
| :----------- | :------------- |
| A tab, a button, a form or a message | the matching section of the [usage guide](../usage/guide.md), and `CHANGELOG.md` under *Unreleased* |
| The dialog's layout, a form or a tab | the guide and its screenshots: run `scripts/capture_screenshot.py` against the [sandbox](environment.md). The command is in its docstring; `--only <name>` redoes one screenshot |
| A new resource type or tab | a section in the guide with its screenshots (add the tab and its main form to the capture script), and a row in the feature tables of `README.md` and `docs/index.md` |
| How the plugin is installed or configured | [installation](../usage/installation.md) and the configuration section of `README.md` |
| A development step, a tool or a command | the page here that teaches it, and the [conventions](conventions.md) page if a contributor would get it wrong |
| An interface icon or where it is used | register it in `resources/icons/catalog.json`, follow the [style guide](icon-style-guide.md) and run `python scripts/build_icon_catalog.py --check` |
| The logo or any brand asset | change `resources/images/geoserver_manager.svg`. The exported files are rendered from it; do not edit them by hand |
| A dependency or a workflow path filter | `.github/dependabot.yml` explains which workflow must see each requirements file. Keep that mapping true |

Two conventions the whole repository follows, this site included: no em
dashes, and 2 short sentences in place of one long one.
