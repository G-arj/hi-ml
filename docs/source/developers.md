# Notes for developers

## Development environment

We suggest using Visual Studio Code (VSCode), available for multiple platforms [here](https://code.visualstudio.com/).
On Windows system, we recommend using WSL, the Windows Subsystem for Linux, because some PyTorch features are not available on Windows.
Inside VSCode, please install the extensions that are recommended for this project - they are available in `.vscode/extensions.json` in the
repository root.

## Creating a Conda environment

To create a separate Conda environment with all packages that `hi-ml` requires for running and testing,
use the provided `environment.yml` file. You can create a Conda environment called `himl` from that via either

```shell script
conda env create --file environment.yml
```

or

```shell script
make env
```

Afterwards, please activate this environment via `conda activate himl`. Select this Python interpreter also inside VSCode,
by choosing "Python: Select Interpreter" from the command palette (Ctrl-Shift-P on VSCode for Windows)

## Installing `pyright`

We are using static typechecking for our code via `mypy` and `pyright`. The latter requires a separate installation
outside the Conda environment. For WSL, these are the required steps (see also
[here](https://docs.microsoft.com/en-us/windows/dev-environment/javascript/nodejs-on-wsl)):

```shell
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.38.0/install.sh | bash
```

Close your terminal and re-open it, then run:

```shell
nvm install node
npm install -g pyright
```

## Using specific versions of `hi-ml` in your Python environments

If you'd like to test specific changes to the `hi-ml` package in your code, you can use two different routes:

* You can clone the `hi-ml` repository on your machine, and use `hi-ml` in your Python environment via a local package
install:

```shell
pip install -e <your_git_folder>/hi-ml
```

* You can consume an early version of the package from `test.pypi.org` via `pip`:

```shell
pip install --extra-index-url https://test.pypi.org/simple/ hi-ml==0.1.0.post165
```

* If you are using Conda, you can add an additional parameter for `pip` into the Conda `environment.yml` file like this:

```yml
name: foo
dependencies:
  - pip=20.1.1
  - python=3.7.3
  - pip:
      - --extra-index-url https://test.pypi.org/simple/
      - hi-ml==0.1.0.post165
```

## Common things to do

The repository contains a makefile with definitions for common operations.

* `make check`: Run `flake8` and `mypy` on the repository.
* `make test`: Run `flake8` and `mypy` on the repository, then all tests via `pytest`
* `make pip`: Install all packages for running and testing in the current interpreter.
* `make conda`: Update the hi-ml Conda environment and activate it

## Building documentation

To build the sphinx documentation, you must have sphinx and related packages installed
(see `build_requirements.txt` in the repository root). Then run:

```shell
cd docs
make html
```

This will build all your documentation in `docs/build/html`.

## Setting up your AzureML workspace

* In the browser, navigate to the AzureML workspace that you want to use for running your tests.
* In the top right section, there will be a dropdown menu showing the name of your AzureML workspace. Expand that.
* In the panel, there is a link "Download config file". Click that.
* This will download a file `config.json`. Move that file to both of the folders `hi-ml/testhiml` and `hi-ml/testazure` 
  The file `config.json` is already present in `.gitignore`, and will hence not be checked in.

## Creating and Deleting Docker Environments in AzureML

* Passing a `docker_base_image` into `submit_to_azure_if_needed` causes a new image to be built and registered in your
workspace (see [docs](https://docs.microsoft.com/en-us/azure/machine-learning/how-to-use-environments) for more
information).
* To remove an environment use the [az ml environment delete](https://docs.microsoft.com/en-us/cli/azure/ml/environment?view=azure-cli-latest#az_ml_environment_delete)
function in the AzureML CLI (note that all the parameters need to be set, none are optional).

## Testing

For all of the tests to work locally you will need to cache your AzureML credentials. One simple way to do this is to
run the example in `src/health/azure/examples` (i.e. run `python elevate_this.py --message='Hello World' --azureml` or
`make example`) after editing `elevate_this.py` to reference your compute cluster.

When running the tests locally, they can either be run against the source directly, or the source built into a package.

* To run the tests against the source directly in the local `src` folder, ensure that there is no wheel in the `dist` folder (for example by running `make clean`). If a wheel is not detected, then the local `src` folder will be copied into the temporary test folder as part of the test process.

* To run the tests against the source as a package, build it with `make build`. This will build the local `src` folder into a new wheel in the `dist` folder. This wheel will be detected and passed to AzureML as a private package as part of the test process.

### Test discovery in VSCode

All tests in the repository should be picked up automatically by VSCode. In particular, this includes the tests in the `hi-ml-histopathology` folder, which
are not always necessary when working on the core `hi-ml` projects.
You can exclude a set of tests from test discovery by modifying `python.testing.pytestArgs` in the VSCode `.vscode/settings.json` file.

## Creating a New Release

To create a new package release, follow these steps:

* On the repository's github page, click on "Releases", then "Draft a new release"
* In the "Draft a new release" page, click "Choose a tag". In the text box, enter a (new) tag name that has
  the desired version number, plus a "v" prefix. For example, to create package version 0.12.17, create a
  tag `v0.12.17`. Then choose "+ Create new tag" below the text box.
* Enter a "Release title" that highlights the main feature(s) of this new package version.
* Click "Auto-generate  release notes" to pull in the titles of the Pull Requests since the last release.
* Before the auto-generated "What's changed" section, add a few sentences that summarize what's new.
* Click "Publish release"

## Troubleshooting

### Debugging a test in VSCode fails on Windows

* Symptom: Debugging just does not seem to do anything
* Check: Debug Console shows error `from _sqlite3 import *: ImportError: DLL load failed: The specified module could not be found.`
* Fix: [see here](https://stackoverflow.com/questions/54876404/unable-to-import-sqlite3-using-anaconda-python)
* Run `conda info --envs` to see where your Conda environment lives, then place `sqlite3.dll` into the `DLLs` folder inside of the environment
