# Packaging and release

Packaging is [qgis-plugin-ci](https://github.com/opengisch/qgis-plugin-ci/).
It builds the zip with `git archive` and reads `CHANGELOG.md` for the release
notes. The plugin itself has no build step: the `geoservercloud` and
`xmltodict` wheels in `geoserver_manager/extras/` are committed, and CI
compiles the translations.

```bash
python -m pip install -U -r requirements/packaging.txt

# a zip numbered with the latest version of CHANGELOG.md
qgis-plugin-ci package latest
```

CI packages each pull request and each push to main. The version is the one
in `metadata.txt` followed by the commit count, such as `1.0.0.270`. The site
publishes main's zip with its `plugins.xml`, so QGIS offers each new build as
an update to a tester who added that repository. A tag is packaged as its own
version.

CI rejects a plugin archive of 1 MB or more, on a tag too, before anything is
published. If the archive grows unexpectedly, check whether the bundled wheel
was replaced with the upstream one. The shipped copy has the 15 MB of
acceptance-test fixtures stripped out, which takes it from 16 MB to 58 KB.
Strip a new wheel the same way on every version bump, and keep
`GSC_REQUIRED` in `toolbelt/dependencies.py` equal to what is shipped. A test
asserts that those two agree.

## Release a version

One released version is one git tag, and the continuous deployment does the
rest. The *Unreleased* section of `CHANGELOG.md` decides the number:

| *Unreleased* contains | Next version |
|---|---|
| Only fixes, documentation, translations, dependency updates | patch, `1.0.x` |
| Something a user can newly do, or a new QGIS or GeoServer version supported | minor, `1.x.0` |
| Something a user can no longer do: a dropped QGIS or Python version, a removed feature, settings that do not carry over | major, `x.0.0` |

A minor with a risk in it, such as a new major version of the library or of
GeoServer, goes out as a pre-release first. A tag such as `1.1.0-beta1`
publishes as experimental. The final tag follows when nobody reports a
problem. Each changelog bullet starts with its kind in bold: **Fixed.**,
**Added.**, **Changed.** or **Removed.**

For a tag `X.Y.Z`, which must be SemVer:

1. Move the *Unreleased* entries of `CHANGELOG.md` under a new
   `## X.Y.Z - YYYY-MM-DD` heading. This text becomes the release notes and
   the description on the plugin repository, so read it once as a stranger
   would.
   Put no `###` heading in an entry. qgis-plugin-ci ends the entry at the
   first one, and drops the text after it.
2. Set `version=X.Y.Z` in `geoserver_manager/metadata.txt`. Keep
   `experimental=False`. A pre-release tag, such as `1.1.0-beta1`, makes
   qgis-plugin-ci publish that version as experimental.
3. Tag and push:

    ```sh
    git tag -a X.Y.Z -m "X.Y.Z"
    git push origin X.Y.Z
    ```

4. The tag triggers *Package and release*. Its release job waits for a
   maintainer's approval: open the run in the *Actions* tab and approve the
   `plugins-qgis-org` deployment. The job then builds the zip and creates the
   GitHub release. It publishes to the
   [QGIS plugin repository](https://plugins.qgis.org/) with the `OSGEO_USER`
   and `OSGEO_PASSWORD` secrets of that environment. The repository scans each
   version and holds it until a staff member approves it, usually within one
   working day.

Only maintainers can push a tag. The `plugins-qgis-org` environment only
accepts version tags, so no branch and no pull request can read its secrets.

The first upload decides the plugin's permanent identifier there: the package
folder name, `geoserver_manager`. It cannot change afterwards; a different
folder name would be a different plugin. Once the plugin exists on that
repository, set its numeric id as `official_repository_id` in `docs/conf.py`.
Then the deployment snippet on the installation page is right.

If a tag went out wrong before the upload, remove it and try again:

```sh
git tag -d X.Y.Z
git push origin :refs/tags/X.Y.Z
```

Once the plugin repository has the upload, it refuses that version number.
Release the next patch version instead.
