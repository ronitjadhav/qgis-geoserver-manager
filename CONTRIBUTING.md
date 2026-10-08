# Contributing Guidelines

Contributions are welcome through issues and pull requests. Start with the
[development setup](https://geoserver-manager.ronit.io/development/environment.html).

## Git hooks

Install the [pre-commit](https://pre-commit.com/) hooks before your first commit:

```sh
pre-commit install
```

`.pre-commit-config.yaml` lists the hooks.

## Code style

Follow [PEP-8](https://www.python.org/dev/peps/pep-0008/) and the existing
code style. The pre-commit hooks run the tools:

- [ruff](https://docs.astral.sh/ruff/) lints and formats first.
- [black](https://black.readthedocs.io/) formats the code.
- [isort](https://pycqa.github.io/isort/) sorts the imports.
- [flake8](https://flake8.pycqa.org/en/latest/) checks for errors and style violations.
- Docstrings follow the [Sphinx style](https://sphinx-rtd-tutorial.readthedocs.io/en/latest/docstrings.html#the-sphinx-docstring-format).

Two habits the whole repository keeps: no em dashes, and two short sentences
rather than one long sentence joined by a dash.

## Before changing code

Three pages of the documentation record what the code cannot tell you. Read
them first, in this order:
[architecture](https://geoserver-manager.ronit.io/development/architecture.html),
[invariants](https://geoserver-manager.ronit.io/development/invariants.html),
[conventions](https://geoserver-manager.ronit.io/development/conventions.html).
A change that breaks an invariant is a bug even when every test passes.

## Tests

Every fix comes with a test that fails without it. Run the new test against the
old code once to prove that it does. See
[testing](https://geoserver-manager.ronit.io/development/testing.html).

## Documentation

Documentation is part of the change, not a follow-up. A pull request that
changes what the user sees also updates the pages that describe it. That means
the [usage guide](https://geoserver-manager.ronit.io/usage/guide.html)
for anything in the dialog, and `CHANGELOG.md` under *Unreleased* for anything
worth telling a user. The
[documentation page](https://geoserver-manager.ronit.io/development/documentation.html)
lists what to touch for each kind of change. It also says how to rebuild the
screenshots, which a script generates.
