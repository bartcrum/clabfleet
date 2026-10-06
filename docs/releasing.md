# Releasing

A release is a version tag. Pushing the tag `v<version>` runs
`.github/workflows/release.yml`, which builds the package, publishes it
to PyPI and makes the GitHub release with that version's section of
`CHANGELOG.md` as its notes.

## Once, before the first release

PyPI accepts the upload through *trusted publishing*: it trusts this
repository's release workflow, and no API token is stored anywhere.

1. On [pypi.org](https://pypi.org), log in and open **Publishing** in
   your account. Under **Add a new pending publisher** enter:

   | Field | Value |
   |---|---|
   | PyPI project name | `clabfleet` |
   | Owner | `bartcrum` |
   | Repository name | `clabfleet` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher reserves nothing: the name `clabfleet` is yours
   once the first upload succeeds.
2. On GitHub, under the repository's **Settings → Environments**, create
   an environment named `pypi`. Adding yourself as a required reviewer
   there makes every publish wait for your approval.

After the first release, replace the `git+https` install line in the
README with `pip install "clabfleet[gui]"`. Not before: until the package
is on PyPI, that command would install whatever someone else puts there
under the name.

## Each release

1. **Version.** Set `__version__` in `clabfleet/__init__.py`. It is the
   only place the version is written; `pyproject.toml` reads it.
2. **Changelog.** In `CHANGELOG.md`, give the version a section headed
   `## <version>` saying what changed, and remove a "Not released yet"
   line from it. The workflow refuses to publish without the section, or
   with that line still in it.
3. **Merge** that to `main` through a pull request as usual, and wait for
   the checks: the tests, `ruff`, and the job that builds the package and
   installs it.
4. **Tag** the merge commit and push the tag:

   ```bash
   git checkout main && git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```

5. **Watch** the `release` workflow. It checks that the tag matches the
   version, builds, publishes to PyPI and creates the GitHub release.

A published version cannot be replaced or reused on PyPI. If something is
wrong with it, release the next version; a broken one can be *yanked* on
PyPI so that installers skip it.

## Trying the build yourself

```bash
pip install build twine
python -m build
twine check --strict dist/*
```

The `package` job in `.github/workflows/tests.yml` does this on every
pull request and also installs the wheel away from the checkout, to see
that the GUI's files were packaged.
