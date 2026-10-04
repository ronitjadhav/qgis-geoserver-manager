# Packaging and release

Packaging is [qgis-plugin-ci](https://github.com/opengisch/qgis-plugin-ci/),
which builds the zip with `git archive` and reads `CHANGELOG.md` for the
release notes. There is no build step of the plugin itself: the
`geoservercloud` and `xmltodict` wheels in `geoserver_manager/extras/` are
committed, and the translations are compiled by CI.

```bash
python -m pip install -U -r requirements/packaging.txt

# a zip numbered with the latest version of CHANGELOG.md
qgis-plugin-ci package latest
```

CI packages each pull request and each push to main as the version in
`metadata.txt` followed by the commit count, such as `0.1.0.270`. The site
publishes main's zip with its `plugins.xml`, so QGIS offers each new build as
an update to a tester who added that repository. A tag is packaged as its own
version.

CI rejects plugin archives of 1 MB or larger, on a tag too, before anything is
published. If the archive grows unexpectedly, check whether the bundled wheel
was replaced with the upstream one. The shipped copy
has the 15 MB of acceptance-test fixtures stripped out, which takes it from
16 MB to 49 KB. Strip the new wheel the same way on every version bump, and
keep `GSC_REQUIRED` in `toolbelt/dependencies.py` equal to what is shipped.
A test asserts those two agree.

## Release a version

One released version is one git tag, and the continuous deployment does the
rest. For a tag `X.Y.Z`, which must be SemVer:

1. Move the *Unreleased* entries of `CHANGELOG.md` under a new
   `## X.Y.Z - YYYY-MM-DD` heading. This text becomes the release notes and
   the plugin repository's description, so it is worth reading once as a
   stranger would.
2. Set `version=X.Y.Z` in `geoserver_manager/metadata.txt`, and drop
   `experimental=True` when the release is no longer experimental.
3. Tag and push:

    ```sh
    git tag -a X.Y.Z -m "X.Y.Z"
    git push origin X.Y.Z
    ```

4. The tag triggers *Package and release*, which builds the zip, creates the
   GitHub release and publishes to the
   [QGIS plugin repository](https://plugins.qgis.org/) using the
   `OSGEO_USER` and `OSGEO_PASSWORD` secrets.

The first upload decides the plugin's permanent identifier there, which is the
package folder name, `geoserver_manager`. It cannot be changed afterwards; a
different folder name would be a different plugin. Once the plugin exists on
that repository, set its numeric id as `official_repository_id` in
`docs/conf.py`, so the deployment snippet on the installation page is right.

If a tag went out wrong, remove it and try again:

```sh
git tag -d X.Y.Z
git push origin :refs/tags/X.Y.Z
```
