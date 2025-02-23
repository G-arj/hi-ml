#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
"""
Tests for the functions in health_azure.azure_util
"""
import json
import logging
import os
import sys
import time
from argparse import ArgumentParser, Namespace, ArgumentError
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from unittest import mock
from unittest.mock import MagicMock, patch
from uuid import uuid4

import conda_merge
import numpy as np
import param
import pytest
from _pytest.capture import CaptureFixture
from _pytest.logging import LogCaptureFixture
from azureml._vendor.azure_storage.blob import Blob
from azureml.core import Experiment, Run, ScriptRunConfig, Workspace
from azureml.core.authentication import ServicePrincipalAuthentication
from azureml.core.environment import CondaDependencies
from azureml.data.azure_storage_datastore import AzureBlobDatastore
from health_azure import paths

import health_azure.utils as util
from health_azure.himl import AML_IGNORE_FILE, append_to_amlignore
from testazure.test_himl import RunTarget, render_and_run_test_script
from testazure.utils_testazure import (DEFAULT_IGNORE_FOLDERS, DEFAULT_WORKSPACE, MockRun, change_working_directory,
                                       repository_root)


RUN_ID = uuid4().hex
RUN_NUMBER = 42
EXPERIMENT_NAME = "fancy-experiment"
AML_TESTS_EXPERIMENT = "test_experiment"


def oh_no() -> None:
    """
    Raise a simple exception. To be used as a side_effect for mocks.
    """
    raise ValueError("Throwing an exception")


@pytest.mark.fast
def test_find_file(tmp_path: Path) -> None:
    file_name = "some_file.json"
    file = tmp_path / file_name
    file.touch()
    python_root = tmp_path / "python_root"
    python_root.mkdir(exist_ok=False)
    start_path = python_root / "starting_directory"
    start_path.mkdir(exist_ok=False)
    where_are_we_now = Path.cwd()
    os.chdir(start_path)
    found_file = util.find_file_in_parent_to_pythonpath(file_name)
    assert found_file
    with mock.patch.dict(os.environ, {"PYTHONPATH": str(python_root)}):
        found_file = util.find_file_in_parent_to_pythonpath(file_name)
        assert not found_file
    os.chdir(where_are_we_now)


def test_is_running_in_azureml() -> None:
    """
    Test if the code correctly recognizes that it is executed in AzureML
    """
    # These tests would always run outside of AzureML, on local box or Azure build agents. Function should return
    # False in all those cases
    assert not util.is_running_in_azure_ml()
    assert not util.is_running_in_azure_ml(util.RUN_CONTEXT)
    # When in AzureML, the Run has a field "experiment"
    mock_workspace = "foo"
    with patch("health_azure.utils.RUN_CONTEXT") as mock_run_context:
        mock_run_context.experiment = MagicMock(workspace=mock_workspace)
        # We can't try that with the default argument because of Python's handling of mutable default arguments
        # (default argument value has been assigned already before mocking)
        assert util.is_running_in_azure_ml(util.RUN_CONTEXT)


@pytest.mark.fast
@patch("health_azure.utils.Workspace.from_config")
@patch("health_azure.utils.get_authentication")
@patch("health_azure.utils.Workspace")
def test_get_workspace(
        mock_workspace: mock.MagicMock,
        mock_get_authentication: mock.MagicMock,
        mock_from_config: mock.MagicMock,
        tmp_path: Path) -> None:
    # Test the case when running on AML
    with patch("health_azure.utils.RUN_CONTEXT") as mock_run_context:
        mock_run_context.experiment = MagicMock(workspace=mock_workspace)
        workspace = util.get_workspace(None, None)
        assert workspace == mock_workspace

    # Test the case when a workspace object is provided. The test always runs outside AzureML, and should return the
    # workspace object unchanged
    mock_workspace2 = "foo"
    workspace = util.get_workspace(mock_workspace2, None)  # type: ignore
    assert workspace == mock_workspace2

    # Test the case when a workspace config path is provided
    mock_get_authentication.return_value = "auth"
    _ = util.get_workspace(None, Path(__file__))
    mock_from_config.assert_called_once_with(path=__file__, auth="auth")

    # Work off a temporary directory: No config file is present
    with change_working_directory(tmp_path):
        with pytest.raises(ValueError) as ex:
            util.get_workspace(None, None)
        assert "No workspace config file given" in str(ex)

    # Workspace config file is set to a file that does not exist
    with pytest.raises(ValueError) as ex:
        util.get_workspace(None, workspace_config_path=tmp_path / "does_not_exist")
    assert "Workspace config file does not exist" in str(ex)

    # Workspace config file is set to a wrong type
    with pytest.raises(ValueError) as ex:
        util.get_workspace(None, workspace_config_path=1)  # type: ignore
    assert "Workspace config path is not a path" in str(ex)


@patch("health_azure.utils.Run")
def test_create_run_recovery_id(mock_run: MagicMock) -> None:
    """
    The recovery id created for a run
    """
    mock_run.id = RUN_ID
    mock_run.experiment.name = EXPERIMENT_NAME
    recovery_id = util.create_run_recovery_id(mock_run)
    assert recovery_id == EXPERIMENT_NAME + util.EXPERIMENT_RUN_SEPARATOR + RUN_ID


@patch("health_azure.utils.Workspace")
@patch("health_azure.utils.Experiment")
@patch("health_azure.utils.Run")
def test_fetch_run(mock_run: MagicMock, mock_experiment: MagicMock, mock_workspace: MagicMock) -> None:
    mock_run.id = RUN_ID
    mock_run.experiment = mock_experiment
    mock_experiment.name = EXPERIMENT_NAME
    recovery_id = EXPERIMENT_NAME + util.EXPERIMENT_RUN_SEPARATOR + RUN_ID
    mock_run.number = RUN_NUMBER
    with mock.patch("health_azure.utils.get_run", return_value=mock_run):
        run_to_recover = util.fetch_run(mock_workspace, recovery_id)
        assert run_to_recover.number == RUN_NUMBER
    mock_experiment.side_effect = oh_no
    with pytest.raises(Exception) as e:
        util.fetch_run(mock_workspace, recovery_id)
    assert str(e.value).startswith(f"Unable to retrieve run {RUN_ID}")


@patch("health_azure.utils.Run")
@patch("health_azure.utils.Experiment")
@patch("health_azure.utils.get_run")
def test_fetch_run_for_experiment(get_run: MagicMock, mock_experiment: MagicMock, mock_run: MagicMock) -> None:
    get_run.side_effect = oh_no
    mock_run.id = RUN_ID
    mock_experiment.get_runs = lambda: [mock_run, mock_run, mock_run]
    mock_experiment.name = EXPERIMENT_NAME
    with pytest.raises(Exception) as e:
        util.fetch_run_for_experiment(mock_experiment, RUN_ID)
    exp = f"Run {RUN_ID} not found for experiment: {EXPERIMENT_NAME}. Available runs are: {RUN_ID}, {RUN_ID}, {RUN_ID}"
    assert str(e.value) == exp


@patch("health_azure.utils.InteractiveLoginAuthentication")
def test_get_authentication(mock_interactive_authentication: MagicMock) -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        util.get_authentication()
        assert mock_interactive_authentication.called
    service_principal_id = "1"
    tenant_id = "2"
    service_principal_password = "3"
    with mock.patch.dict(
            os.environ,
            {
                util.ENV_SERVICE_PRINCIPAL_ID: service_principal_id,
                util.ENV_TENANT_ID: tenant_id,
                util.ENV_SERVICE_PRINCIPAL_PASSWORD: service_principal_password
            },
            clear=True):
        spa = util.get_authentication()
        assert isinstance(spa, ServicePrincipalAuthentication)
        assert spa._service_principal_id == service_principal_id
        assert spa._tenant_id == tenant_id
        assert spa._service_principal_password == service_principal_password


def test_get_secret_from_environment() -> None:
    env_variable_name = uuid4().hex.upper()
    env_variable_value = "42"
    with pytest.raises(ValueError) as e:
        util.get_secret_from_environment(env_variable_name)
    assert str(e.value) == f"There is no value stored for the secret named '{env_variable_name}'"
    assert util.get_secret_from_environment(env_variable_name, allow_missing=True) is None
    with mock.patch.dict(os.environ, {env_variable_name: env_variable_value}):
        assert util.get_secret_from_environment(env_variable_name) == env_variable_value


def test_to_azure_friendly_string() -> None:
    """
    Tests the to_azure_friendly_string function which should replace everything apart from a-zA-Z0-9_ with _, and
    replace multiple _ with a single _
    """
    bad_string = "full__0f_r*bb%sh"
    good_version = util.to_azure_friendly_string(bad_string)
    assert good_version == "full_0f_r_bb_sh"
    good_string = "Not_Full_0f_Rubbish"
    good_version = util.to_azure_friendly_string(good_string)
    assert good_version == good_string
    optional_string = None
    assert optional_string == util.to_azure_friendly_string(optional_string)


def test_split_recovery_id_fails() -> None:
    """
    Other tests test the main branch of split_recovery_id, but they do not test the exceptions
    """
    with pytest.raises(ValueError) as e:
        id = util.EXPERIMENT_RUN_SEPARATOR.join([str(i) for i in range(3)])
        util.split_recovery_id(id)
        assert str(e.value) == f"recovery_id must be in the format: 'experiment_name:run_id', but got: {id}"
    with pytest.raises(ValueError) as e:
        id = "foo_bar"
        util.split_recovery_id(id)
        assert str(e.value) == f"The recovery ID was not in the expected format: {id}"


@pytest.mark.parametrize(["id", "expected1", "expected2"],
                         [("foo:bar", "foo", "bar"),
                          ("foo:bar_ab_cd", "foo", "bar_ab_cd"),
                          ("a_b_c_00_123", "a_b_c", "a_b_c_00_123"),
                          ("baz_00_123", "baz", "baz_00_123"),
                          ("foo_bar_abc_123_456", "foo_bar_abc", "foo_bar_abc_123_456"),
                          # This is the run ID of a hyperdrive parent run. It only has one numeric part at the end
                          ("foo_bar_123", "foo_bar", "foo_bar_123"),
                          # This is a hyperdrive child run
                          ("foo_bar_123_3", "foo_bar", "foo_bar_123_3"),
                          ])
def test_split_recovery_id(id: str, expected1: str, expected2: str) -> None:
    """
    Check that run recovery ids are correctly parsed into experiment and run id.
    """
    assert util.split_recovery_id(id) == (expected1, expected2)


def test_retrieve_unique_deps() -> None:
    deps_with_duplicates = ["package==1.0", "package==1.1", "git+https:www.github.com/something.git"]

    dedup_deps = util._retrieve_unique_deps(deps_with_duplicates)  # type: ignore
    assert dedup_deps == ["package==1.0", "git+https:www.github.com/something.git"]

    dedup_deps_keep_last = util._retrieve_unique_deps(deps_with_duplicates, keep_method="last")
    assert dedup_deps_keep_last == ["package==1.1", "git+https:www.github.com/something.git"]


def test_merge_conda(
        random_folder: Path,
        caplog: CaptureFixture,
) -> None:
    """
    Tests the logic for merging Conda environment files.
    """
    env1 = """
channels:
  - defaults
  - pytorch
dependencies:
  - conda1=1.0
  - conda2=2.0
  - conda_both=3.0
  - pip:
      - azureml-sdk==1.7.0
      - foo==1.0
"""
    env2 = """
channels:
  - defaults
dependencies:
  - conda1=1.1
  - conda_both=3.0
  - pip:
      - azureml-sdk==1.6.0
      - bar==2.0
"""
    # Spurious test failures on Linux build agents, saying that they can't write the file. Wait a bit.
    time.sleep(0.5)
    file1 = random_folder / "env1.yml"
    file1.write_text(env1)
    file2 = random_folder / "env2.yml"
    file2.write_text(env2)
    # Spurious test failures on Linux build agents, saying that they can't read the file. Wait a bit.
    time.sleep(0.5)
    files = [file1, file2]
    merged_file = random_folder / "merged.yml"
    util.merge_conda_files(files, merged_file)
    merged_file_text = merged_file.read_text()
    assert merged_file_text.splitlines() == """channels:
- defaults
- pytorch
dependencies:
- conda1=1.0
- conda2=2.0
- conda_both=3.0
- pip:
  - azureml-sdk==1.6.0
  - bar==2.0
  - foo==1.0
""".splitlines()
    conda_dep = CondaDependencies(merged_file)

    # We expect to see the union of channels.
    assert list(conda_dep.conda_channels) == ["defaults", "pytorch"]

    # Package version conflicts are not resolved, both versions are retained.
    assert list(conda_dep.conda_packages) == ["conda1=1.0", "conda2=2.0", "conda_both=3.0"]
    assert list(conda_dep.pip_packages) == ["azureml-sdk==1.6.0", "bar==2.0", "foo==1.0"]

    # Assert that extra pip requirements are added correctly
    pip_contents = """package1==0.0.1
package2==0.0.1
"""
    pip_file = random_folder / "req.txt"
    pip_file.write_text(pip_contents)
    util.merge_conda_files(files, merged_file, pip_files=[pip_file])
    merged_file_text = merged_file.read_text()
    assert merged_file_text.splitlines() == """channels:
- defaults
- pytorch
dependencies:
- conda1=1.0
- conda2=2.0
- conda_both=3.0
- pip:
  - azureml-sdk==1.6.0
  - bar==2.0
  - foo==1.0
  - package1==0.0.1
  - package2==0.0.1""".splitlines()

    # Are names merged correctly?
    assert "name:" not in merged_file_text
    env1 = "name: env1\n" + env1
    file1.write_text(env1)
    env2 = "name: env2\n" + env2
    file2.write_text(env2)
    util.merge_conda_files(files, merged_file)
    assert "name: env2" in merged_file.read_text()

    def raise_a_merge_error() -> None:
        raise conda_merge.MergeError("raising an exception")

    with mock.patch("health_azure.utils.conda_merge.merge_channels") as mock_merge_channels:
        mock_merge_channels.side_effect = lambda _: raise_a_merge_error()
        with pytest.raises(conda_merge.MergeError):
            util.merge_conda_files(files, merged_file)
    assert "Failed to merge channel priorities" in caplog.text  # type: ignore

    # If there are no channels do not produce any merge of them
    with mock.patch("health_azure.utils.conda_merge.merge_channels") as mock_merge_channels:
        mock_merge_channels.return_value = []
        util.merge_conda_files(files, merged_file)
        assert "channels:" not in merged_file.read_text()

    with mock.patch("health_azure.utils.conda_merge.merge_dependencies") as mock_merge_dependencies:
        mock_merge_dependencies.side_effect = lambda _: raise_a_merge_error()
        with pytest.raises(conda_merge.MergeError):
            util.merge_conda_files(files, merged_file)
    assert "Failed to merge dependencies" in caplog.text  # type: ignore

    # If there are no dependencies then something is wrong with the conda files or our parsing of them
    with mock.patch("health_azure.utils.conda_merge.merge_dependencies") as mock_merge_dependencies:
        mock_merge_dependencies.return_value = []
        with pytest.raises(ValueError):
            util.merge_conda_files(files, merged_file)


def test_merge_conda_pip_include(random_folder: Path) -> None:
    """
    Tests the logic to exclude PIP include statements from Conda environments.
    """
    env1 = """
channels:
  - default
dependencies:
  - conda_both=3.0
  - pip:
      - -r requirements.txt
      - foo==1.0
"""
    file1 = random_folder / "env1.yml"
    file1.write_text(env1)
    merged_file = random_folder / "merged.yml"
    util.merge_conda_files([file1], merged_file)
    merged_contents = merged_file.read_text()
    assert "-r requirements.txt" not in merged_contents

    file2 = random_folder / "requirements.txt"
    file2.write_text("package==1.0.0")
    merged_file2 = random_folder / "merged2.yml"
    util.merge_conda_files([file1], merged_file2, pip_files=[file2])
    merged_contents2 = merged_file2.read_text()
    assert merged_contents2 == """channels:
- default
dependencies:
- conda_both=3.0
- pip:
  - foo==1.0
  - package==1.0.0
"""


def test_merge_conda_pip_include2(random_folder: Path) -> None:
    """
    Tests the logic to exclude PIP include statements from Conda environments, on the root level environment file.
    """
    if paths.is_himl_used_from_git_repo():
        root_yaml = paths.git_repo_root_folder() / paths.ENVIRONMENT_YAML_FILE_NAME
        requirements = paths.git_repo_root_folder() / "hi-ml-azure" / "run_requirements.txt"
        merged_file2 = random_folder / "merged2.yml"
        util.merge_conda_files([root_yaml], merged_file2, pip_files=[requirements])


def assert_pip_length(yaml: Any, expected_length: int) -> None:
    """Checks if the pip dependencies section of a Conda YAML file has the expected number of entries
    """
    pip = util._get_pip_dependencies(yaml)
    assert pip is not None
    assert len(pip[1]) == expected_length


@pytest.mark.fast
def test_pip_include_1() -> None:
    """Test if Conda files that use PIP include are handled correctly. This uses the top-level environment.yml
    file in the repository.
    """
    if paths.is_himl_used_from_git_repo():
        root_yaml = paths.git_repo_root_folder() / paths.ENVIRONMENT_YAML_FILE_NAME
        assert root_yaml.is_file()
        original_yaml = conda_merge.read_file(root_yaml)
        # At the time of writing, the top-level environment file only had 4 include statements in the pip
        # section, they should all be filtered out.
        assert_pip_length(original_yaml, 4)
        uses_pip_include, modified_yaml = util.is_conda_file_with_pip_include(root_yaml)
        assert uses_pip_include
        pip = util._get_pip_dependencies(modified_yaml)
        # The pip section of the top-level yaml has nothing but include statements, so after filtering the
        # pip section is empty. In this case, no pip section shoudld be present at all.
        assert pip is None


@pytest.mark.fast
def test_pip_include_2(tmp_path: Path) -> None:
    """Test if Conda files that use PIP include are recognized.
    """
    # Environment file without a "-r" include statement
    conda_str = """name: simple-envpip
dependencies:
  - pip:
    - azureml-sdk==1.23.0
  - more_conda
"""
    tmp_conda = tmp_path / "env.yml"
    tmp_conda.write_text(conda_str)
    uses_pip_include, modified_yaml = util.is_conda_file_with_pip_include(tmp_conda)
    assert not uses_pip_include
    assert_pip_length(modified_yaml, 1)

    # Environment file that has a "-r" include statement
    conda_str = """name: simple-env
dependencies:
  - pip:
    - -r foo.txt
    - any_package
"""
    tmp_conda.write_text(conda_str)
    uses_pip_include, modified_yaml = util.is_conda_file_with_pip_include(tmp_conda)
    assert uses_pip_include
    assert util._get_pip_dependencies(modified_yaml) == (0, ["any_package"])


@pytest.mark.parametrize(["s", "expected"],
                         [
                             ("1s", 1),
                             ("0.5m", 30),
                             ("1.5h", 90 * 60),
                             ("1.0d", 24 * 3600),
                             ("", None),
                         ])  # NOQA
@pytest.mark.fast
def test_run_duration(s: str, expected: Optional[float]) -> None:
    actual = util.run_duration_string_to_seconds(s)
    assert actual == expected
    if expected:
        assert isinstance(actual, int)


@pytest.mark.fast
def test_run_duration_fails() -> None:
    with pytest.raises(Exception):
        util.run_duration_string_to_seconds("17b")


@pytest.mark.fast
def test_repository_root() -> None:
    root = repository_root()
    assert (root / "SECURITY.md").is_file()


def test_nonexisting_amlignore(random_folder: Path) -> None:
    """
    Test that we can create an .AMLignore file, and it gets deleted after use.
    """
    folder1 = "Added1"
    added_folders = [folder1]
    cwd = Path.cwd()
    amlignore = random_folder / AML_IGNORE_FILE
    assert not amlignore.is_file()
    os.chdir(random_folder)
    with append_to_amlignore(added_folders):
        new_contents = amlignore.read_text()
        for f in added_folders:
            assert f in new_contents
    assert not amlignore.is_file()
    os.chdir(cwd)


@patch("health_azure.utils.Workspace")
def test_create_python_environment(
        mock_workspace: mock.MagicMock,
        random_folder: Path,
) -> None:
    conda_str = """name: simple-env
dependencies:
  - pip=20.1.1
  - python=3.7.3
  - pip:
    - azureml-sdk==1.23.0
    - conda-merge==0.1.5
  - pip:
    - --index-url https://test.pypi.org/simple/
    - --extra-index-url https://pypi.org/simple
    - hi-ml-azure
"""
    conda_environment_file = random_folder / "environment.yml"
    conda_environment_file.write_text(conda_str)
    conda_dependencies = CondaDependencies(conda_dependencies_file_path=conda_environment_file)
    env = util.create_python_environment(conda_environment_file=conda_environment_file)
    assert list(env.python.conda_dependencies.conda_channels) == list(conda_dependencies.conda_channels)
    assert list(env.python.conda_dependencies.conda_packages) == list(conda_dependencies.conda_packages)
    assert list(env.python.conda_dependencies.pip_options) == list(conda_dependencies.pip_options)
    assert list(env.python.conda_dependencies.pip_packages) == list(conda_dependencies.pip_packages)
    assert "AZUREML_OUTPUT_UPLOAD_TIMEOUT_SEC" in env.environment_variables
    assert "AZUREML_RUN_KILL_SIGNAL_TIMEOUT_SEC" in env.environment_variables
    assert "RSLEX_DIRECT_VOLUME_MOUNT" in env.environment_variables
    assert "RSLEX_DIRECT_VOLUME_MOUNT_MAX_CACHE_SIZE" in env.environment_variables
    # Just check that the environment has a reasonable name. Detailed checks for uniqueness of the name follow below.
    assert env.name.startswith("HealthML")

    pip_extra_index_url = "https://where.great.packages.live/"
    docker_base_image = "viennaglobal.azurecr.io/azureml/azureml_a187a87cc7c31ac4d9f67496bc9c8239"
    env = util.create_python_environment(
        conda_environment_file=conda_environment_file,
        pip_extra_index_url=pip_extra_index_url,
        docker_base_image=docker_base_image,
        environment_variables={"HELLO": "world"})
    # Environment variables should be added to the default ones
    assert "HELLO" in env.environment_variables
    assert "RSLEX_DIRECT_VOLUME_MOUNT" in env.environment_variables
    assert env.docker.base_image == docker_base_image

    private_pip_wheel_url = "https://some.blob/private/wheel"
    with mock.patch("health_azure.utils.Environment") as mock_environment:
        mock_environment.add_private_pip_wheel.return_value = private_pip_wheel_url
        env = util.create_python_environment(
            conda_environment_file=conda_environment_file,
            workspace=mock_workspace,
            private_pip_wheel_path=Path(__file__))
    envs_pip_packages = list(env.python.conda_dependencies.pip_packages)
    assert "hi-ml-azure" in envs_pip_packages
    assert private_pip_wheel_url in envs_pip_packages


def test_create_environment_unique_name(random_folder: Path) -> None:
    """
    Test if the name of the conda environment changes with each of the components
    """
    conda_str1 = """name: simple-env
dependencies:
  - pip=20.1.1
  - python=3.7.3
"""
    conda_environment_file = random_folder / "environment.yml"
    conda_environment_file.write_text(conda_str1)
    env1 = util.create_python_environment(conda_environment_file=conda_environment_file)

    # Changing the contents of the conda file should create a new environment names
    conda_str2 = """name: simple-env
dependencies:
  - pip=20.1.1
"""
    assert conda_str1 != conda_str2
    conda_environment_file.write_text(conda_str2)
    env2 = util.create_python_environment(conda_environment_file=conda_environment_file)
    assert env1.name != env2.name

    # Using a different PIP index URL can lead to different package resolution, so this should change name too
    env3 = util.create_python_environment(conda_environment_file=conda_environment_file,
                                          pip_extra_index_url="foo")
    assert env3.name != env2.name

    # Environment variables
    env4 = util.create_python_environment(conda_environment_file=conda_environment_file,
                                          environment_variables={"foo": "bar"})
    assert env4.name != env2.name

    # Docker base image
    env5 = util.create_python_environment(conda_environment_file=conda_environment_file,
                                          docker_base_image="docker")
    assert env5.name != env2.name

    # PIP wheel
    with mock.patch("health_azure.utils.Environment") as mock_environment:
        mock_environment.add_private_pip_wheel.return_value = "private_pip_wheel_url"
        env6 = util.create_python_environment(
            conda_environment_file=conda_environment_file,
            workspace=DEFAULT_WORKSPACE.workspace,
            private_pip_wheel_path=Path(__file__))
        assert env6.name != env2.name

    all_names = [env1.name, env2.name, env3.name, env4.name, env5.name, env6.name]
    all_names_set = {*all_names}
    assert len(all_names) == len(all_names_set), "Environment names are not unique"


def test_create_environment_wheel_fails(random_folder: Path) -> None:
    """
    Test if all necessary checks are carried out when adding private wheels to an environment.
    """
    conda_str = """name: simple-env
dependencies:
  - pip=20.1.1
  - python=3.7.3
"""
    conda_environment_file = random_folder / "environment.yml"
    conda_environment_file.write_text(conda_str)
    # Wheel file does not exist at all:
    with pytest.raises(FileNotFoundError) as ex1:
        util.create_python_environment(conda_environment_file=conda_environment_file,
                                       private_pip_wheel_path=Path("does_not_exist"))
        assert "Cannot add private wheel" in str(ex1)
    # Wheel exists, but no workspace provided:
    with pytest.raises(ValueError) as ex2:
        util.create_python_environment(conda_environment_file=conda_environment_file,
                                       private_pip_wheel_path=Path(__file__))
        assert "AzureML workspace must be provided" in str(ex2)


class MockEnvironment:
    def __init__(self, name: str, version: str = "autosave") -> None:
        self.name = name
        self.version = version


@patch("health_azure.utils.Environment")
@patch("health_azure.utils.Workspace")
def test_register_environment(
        mock_workspace: mock.MagicMock,
        mock_environment: mock.MagicMock,
        caplog: LogCaptureFixture,
) -> None:
    def _mock_env_get(workspace: Workspace, name: str = "", version: Optional[str] = None) -> MockEnvironment:
        if version is None:
            raise Exception("not found")
        return MockEnvironment(name, version=version)

    env_name = "an environment"
    env_version = "environment version"
    mock_environment.get.return_value = mock_environment
    mock_environment.name = env_name
    mock_environment.version = env_version
    with caplog.at_level(logging.INFO):  # type: ignore
        _ = util.register_environment(mock_workspace, mock_environment)
        caplog_text: str = caplog.text  # for mypy
        assert f"Using existing Python environment '{env_name}' with version '{env_version}'" in caplog_text

        # test that log is correct when exception is triggered
        mock_environment.get.side_effect = oh_no
        _ = util.register_environment(mock_workspace, mock_environment)
        caplog_text = caplog.text  # for mypy
        assert f"environment '{env_name}' does not yet exist, creating and registering it with version" \
               f" '{env_version}'" in caplog_text

        # test that environment version equals ENVIRONMENT_VERSION when exception is triggered
        # rather than default value of "autosave"
        mock_environment.version = None
        with patch.object(mock_environment, "get", _mock_env_get):
            with patch.object(mock_environment, "register") as mock_register:
                mock_register.return_value = mock_environment
                env = util.register_environment(mock_workspace, mock_environment)
                assert env.version == util.ENVIRONMENT_VERSION


def test_set_environment_variables_for_multi_node(
        caplog: CaptureFixture,
        capsys: CaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):  # type: ignore
        util.set_environment_variables_for_multi_node()
        assert "No settings for the MPI central node found" in caplog.text  # type: ignore
        assert "Assuming that this is a single node training job" in caplog.text  # type: ignore

    with mock.patch.dict(
            os.environ,
            {
                util.ENV_AZ_BATCHAI_MPI_MASTER_NODE: "here",
                util.ENV_MASTER_PORT: "there",
                util.ENV_OMPI_COMM_WORLD_RANK: "everywhere",
                util.ENV_MASTER_ADDR: "else",
            },
            clear=True):
        util.set_environment_variables_for_multi_node()
    out, _ = capsys.readouterr()
    assert "Distributed training: MASTER_ADDR = here, MASTER_PORT = there, NODE_RANK = everywhere" in out

    with mock.patch.dict(
            os.environ,
            {
                util.ENV_MASTER_IP: "here",
                util.ENV_NODE_RANK: "everywhere",
                util.ENV_MASTER_ADDR: "else",
            },
            clear=True):
        util.set_environment_variables_for_multi_node()
    out, _ = capsys.readouterr()
    assert "Distributed training: MASTER_ADDR = here, MASTER_PORT = 6105, NODE_RANK = everywhere" in out


@pytest.mark.fast
@patch("health_azure.utils.fetch_run")
@patch("azureml.core.Workspace")
def test_get_most_recent_run(mock_workspace: MagicMock, mock_fetch_run: MagicMock, tmp_path: Path) -> None:
    mock_run_id = "run_abc_123"
    mock_run = MockRun(mock_run_id)
    mock_workspace.get_run.return_value = mock_run
    mock_fetch_run.return_value = mock_run

    latest_path = tmp_path / "most_recent_run.txt"
    latest_path.write_text(mock_run_id)

    run = util.get_most_recent_run(latest_path, mock_workspace)
    assert run.id == mock_run_id


def _get_experiment_runs(tags: Dict[str, str]) -> List[MockRun]:
    mock_run_no_tags = MockRun()
    mock_run_tags = MockRun(tags={"completed": "True"})
    all_runs = [mock_run_no_tags for _ in range(5)] + [mock_run_tags for _ in range(5)]
    return [r for r in all_runs if r.tags == tags] if tags else all_runs


@pytest.mark.fast
@pytest.mark.parametrize("num_runs, tags, expected_num_returned", [
    (1, {"completed": "True"}, 1),
    (3, {}, 3),
    (2, {"Completed: False"}, 0)
])
def test_get_latest_aml_run_from_experiment(num_runs: int, tags: Dict[str, str], expected_num_returned: int) -> None:
    mock_experiment_name = "MockExperiment"

    with mock.patch("health_azure.utils.Experiment") as mock_experiment:
        with mock.patch("health_azure.utils.Workspace",
                        experiments={mock_experiment_name: mock_experiment}
                        ) as mock_workspace:
            mock_experiment.get_runs.return_value = _get_experiment_runs(tags)
            aml_runs = util.get_latest_aml_runs_from_experiment(mock_experiment_name, num_runs=num_runs,
                                                                tags=tags, aml_workspace=mock_workspace)
            assert len(aml_runs) == expected_num_returned


def test_get_latest_aml_run_from_experiment_remote(tmp_path: Path) -> None:
    """
    Test that a remote run with particular tags can be correctly retrieved, ignoring any more recent
    experiments which do not have the correct tags. Note: this test will instantiate 2 new Runs in the
    workspace described in your config.json file, under an experiment defined by AML_TESTS_EXPERIMENT
    """
    ws = DEFAULT_WORKSPACE.workspace
    assert True

    experiment = Experiment(ws, AML_TESTS_EXPERIMENT)
    config = ScriptRunConfig(
        source_directory=".",
        command=["cd ."],  # command that does nothing
        compute_target="local"
    )
    # Create first run and tag
    with append_to_amlignore(
            amlignore=Path("") / AML_IGNORE_FILE,
            lines_to_append=DEFAULT_IGNORE_FOLDERS):
        first_run = experiment.submit(config)
    tags = {"experiment_type": "great_experiment"}
    first_run.set_tags(tags)
    first_run.wait_for_completion()

    # Create second run and ensure no tags
    with append_to_amlignore(
            amlignore=Path("") / AML_IGNORE_FILE,
            lines_to_append=DEFAULT_IGNORE_FOLDERS):
        second_run = experiment.submit(config)
    if any(second_run.get_tags()):
        second_run.remove_tags(tags)

    # Retrieve latest run with given tags (expect first_run to be returned)
    retrieved_runs = util.get_latest_aml_runs_from_experiment(AML_TESTS_EXPERIMENT, tags=tags, aml_workspace=ws)
    assert len(retrieved_runs) == 1
    assert retrieved_runs[0].id == first_run.id
    assert retrieved_runs[0].get_tags() == tags


@pytest.mark.fast
@patch("health_azure.utils.Workspace")
@pytest.mark.parametrize("mock_run_id", ["run_abc_123", "experiment1:run_bcd_456"])
def test_get_aml_run_from_run_id(mock_workspace: MagicMock, mock_run_id: str) -> None:
    def _mock_get_run(run_id: str) -> MockRun:
        if len(mock_run_id.split(util.EXPERIMENT_RUN_SEPARATOR)) > 1:
            return MockRun(mock_run_id.split(util.EXPERIMENT_RUN_SEPARATOR)[1])
        return MockRun(mock_run_id)

    mock_workspace.get_run = _mock_get_run

    aml_run = util.get_aml_run_from_run_id(mock_run_id, aml_workspace=mock_workspace)
    if len(mock_run_id.split(util.EXPERIMENT_RUN_SEPARATOR)) > 1:
        mock_run_id = mock_run_id.split(util.EXPERIMENT_RUN_SEPARATOR)[1]

    assert aml_run.id == mock_run_id


def _get_file_names(pref: str = "") -> List[str]:
    file_names = ["somepath.txt", "abc/someotherpath.txt", "abc/def/anotherpath.txt"]
    if len(pref) > 0:
        return [u for u in file_names if u.startswith(pref)]
    else:
        return file_names


def test_get_run_file_names() -> None:
    with patch("azureml.core.Run") as mock_run:
        expected_file_names = _get_file_names()
        mock_run.get_file_names.return_value = expected_file_names
        # check that we get the expected run paths if no filter is applied
        run_paths = util.get_run_file_names(mock_run)  # type: ignore
        assert len(run_paths) == len(expected_file_names)
        assert sorted(run_paths) == sorted(expected_file_names)

        # Now check we get the expected run paths if a filter is applied
        prefix = "abc"
        run_paths = util.get_run_file_names(mock_run, prefix=prefix)
        assert all([f.startswith(prefix) for f in run_paths])


def _mock_download_file(filename: str, output_file_path: Optional[Path] = None,
                        _validate_checksum: bool = False) -> None:
    """
    Creates an empty file at the given output_file_path
    """
    output_file_path = Path('test_output') if output_file_path is None else output_file_path
    output_file_path = Path(output_file_path) if not isinstance(output_file_path, Path) else output_file_path  # mypy
    output_file_path.parent.mkdir(exist_ok=True, parents=True)
    output_file_path.touch(exist_ok=True)


@pytest.mark.parametrize("dummy_env_vars", [{}, {util.ENV_LOCAL_RANK: "1"}])
@pytest.mark.parametrize("prefix", ["", "abc"])
def test_download_run_files(tmp_path: Path, dummy_env_vars: Dict[Optional[str], Optional[str]], prefix: str) -> None:
    # Assert that 'downloaded' paths don't exist to begin with
    dummy_paths = [Path(x) for x in _get_file_names(pref=prefix)]
    expected_paths = [tmp_path / dummy_path for dummy_path in dummy_paths]
    # Ensure that paths don't already exist
    [p.unlink() for p in expected_paths if p.exists()]  # type: ignore
    assert not any([p.exists() for p in expected_paths])

    mock_run = MockRun(run_id="id123")
    with mock.patch.dict(os.environ, dummy_env_vars):
        with patch("health_azure.utils.get_run_file_names") as mock_get_run_paths:
            mock_get_run_paths.return_value = dummy_paths  # type: ignore
            mock_run.download_file = MagicMock()  # type: ignore
            mock_run.download_file.side_effect = _mock_download_file

            util._download_files_from_run(mock_run, output_dir=tmp_path)
            # First test the case where is_local_rank_zero returns True
            if not any(dummy_env_vars):
                # Check that our mocked _download_file_from_run has been called once for each file
                assert sum([p.exists() for p in expected_paths]) == len(expected_paths)
            # Now test the case where is_local_rank_zero returns False - in this case nothing should be created
            else:
                assert not any([p.exists() for p in expected_paths])


@patch("health_azure.utils.get_workspace")
@patch("health_azure.utils.get_aml_run_from_run_id")
@patch("health_azure.utils._download_files_from_run")
def test_download_files_from_run_id(mock_download_run_files: MagicMock,
                                    mock_get_aml_run_from_run_id: MagicMock,
                                    mock_workspace: MagicMock) -> None:
    mock_run = {"id": "run123"}
    mock_get_aml_run_from_run_id.return_value = mock_run
    util.download_files_from_run_id("run123", Path(__file__))
    mock_download_run_files.assert_called_with(mock_run, Path(__file__), prefix="", validate_checksum=False)


@pytest.mark.parametrize("dummy_env_vars, expect_file_downloaded", [({}, True), ({util.ENV_LOCAL_RANK: "1"}, False)])
@patch("azureml.core.Run", MockRun)
def test_download_file_from_run(tmp_path: Path, dummy_env_vars: Dict[str, str], expect_file_downloaded: bool) -> None:
    dummy_filename = "filetodownload.txt"
    expected_file_path = tmp_path / dummy_filename

    # mock the method 'download_file' on the AML Run class and assert it gets called with the expected params
    mock_run = MockRun(run_id="id123")
    mock_run.download_file = MagicMock(return_value=None)  # type: ignore
    mock_run.download_file.side_effect = _mock_download_file

    with mock.patch.dict(os.environ, dummy_env_vars):
        _ = util._download_file_from_run(mock_run, dummy_filename, expected_file_path)

        if expect_file_downloaded:
            mock_run.download_file.assert_called_with(dummy_filename, output_file_path=str(expected_file_path),
                                                      _validate_checksum=False)
            assert expected_file_path.exists()
        else:
            assert not expected_file_path.exists()


def test_download_file_from_run_remote(tmp_path: Path) -> None:
    # This test will create a Run in your workspace (using only local compute)
    ws = DEFAULT_WORKSPACE.workspace
    experiment = Experiment(ws, AML_TESTS_EXPERIMENT)
    config = ScriptRunConfig(
        source_directory=".",
        command=["cd ."],  # command that does nothing
        compute_target="local"
    )
    with append_to_amlignore(
            amlignore=Path("") / AML_IGNORE_FILE,
            lines_to_append=DEFAULT_IGNORE_FOLDERS):
        run = experiment.submit(config)

    file_to_upload = tmp_path / "dummy_file.txt"
    file_contents = "Hello world"
    file_to_upload.write_text(file_contents)

    # This should store the file in outputs
    run.upload_file("dummy_file", str(file_to_upload))

    output_file_path = tmp_path / "downloaded_file.txt"
    assert not output_file_path.exists()

    start_time = time.perf_counter()
    _ = util._download_file_from_run(run, "dummy_file", output_file_path)
    end_time = time.perf_counter()
    time_dont_validate_checksum = end_time - start_time

    assert output_file_path.exists()
    assert output_file_path.read_text() == file_contents

    # Now delete the file and try again with _validate_checksum == True
    output_file_path.unlink()
    assert not output_file_path.exists()
    start_time = time.perf_counter()
    _ = util._download_file_from_run(run, "dummy_file", output_file_path, validate_checksum=True)
    end_time = time.perf_counter()
    time_validate_checksum = end_time - start_time

    assert output_file_path.exists()
    assert output_file_path.read_text() == file_contents

    logging.info(f"Time to download file without checksum: {time_dont_validate_checksum} vs time with"
                 f"validation {time_validate_checksum}.")


def test_download_run_file_during_run(tmp_path: Path) -> None:
    """
    Test if we can download files from a run, when executing inside AzureML. This should not require any additional
    information about the workspace to use, but pick up the current workspace.
    """
    # Create a run that contains a simple txt file
    experiment_name = "himl-tests"
    run_to_download_from = util.create_aml_run_object(experiment_name=experiment_name,
                                                      workspace=DEFAULT_WORKSPACE.workspace)
    file_contents = "Hello World!"
    file_name = "hello.txt"
    full_file_path = tmp_path / file_name
    full_file_path.write_text(file_contents)
    run_to_download_from.upload_file(file_name, str(full_file_path))
    run_to_download_from.complete()
    run_id = run_to_download_from.id

    # Test if we can retrieve the run directly from the workspace. This tests for a bug in an earlier version
    # of the code where run IDs as those created from runs outside AML were not recognized
    run_2 = util.get_aml_run_from_run_id(run_id, aml_workspace=DEFAULT_WORKSPACE.workspace)
    assert run_2.id == run_id

    # Now create an AzureML run with a simple script that uses that file. The script will download the file,
    # where the download is should pick up the workspace from the current AML run.
    script_body = ""
    script_body += f"run_id = '{run_id}'\n"
    script_body += f"    file_name = '{file_name}'\n"
    script_body += f"    file_contents = '{file_contents}'\n"
    script_body += """
    output_path = Path("outputs")
    output_path.mkdir(exist_ok=True)

    download_files_from_run_id(run_id, output_path, prefix=file_name)
    full_file_path = output_path / file_name
    actual_contents = full_file_path.read_text().strip()
    print(f"{actual_contents}")
    assert actual_contents == file_contents
"""
    extra_options = {
        "imports": """
import sys
from pathlib import Path
from azureml.core import Run
from health_azure.utils import download_files_from_run_id""",
        "body": script_body
    }
    # Run the script locally first, then in the cloud. In local runs, the workspace should be picked up from the
    # config.json file, in AzureML runs it should be read off the run context.
    render_and_run_test_script(tmp_path, RunTarget.LOCAL, extra_options, extra_args=[], expected_pass=True)
    print("Local run finished")
    render_and_run_test_script(tmp_path / "foo", RunTarget.AZUREML, extra_options, extra_args=[], expected_pass=True)


def test_is_global_rank_zero() -> None:
    with mock.patch.dict(os.environ, {util.ENV_NODE_RANK: "0", util.ENV_GLOBAL_RANK: "0", util.ENV_LOCAL_RANK: "0"}):
        assert not util.is_global_rank_zero()

    with mock.patch.dict(os.environ, {util.ENV_GLOBAL_RANK: "0", util.ENV_LOCAL_RANK: "0"}):
        assert not util.is_global_rank_zero()

    with mock.patch.dict(os.environ, {util.ENV_NODE_RANK: "0"}):
        assert util.is_global_rank_zero()


def test_is_local_rank_zero() -> None:
    # mock the environment variables
    with mock.patch.dict(os.environ, {}):
        assert util.is_local_rank_zero()

    with mock.patch.dict(os.environ, {util.ENV_GLOBAL_RANK: "1", util.ENV_LOCAL_RANK: "1"}):
        assert not util.is_local_rank_zero()


@pytest.mark.parametrize("dummy_recovery_id", [
    "expt:run_abc_1234",
    "['expt:abc_432','expt2:def_111']",
    "run_ghi_1234",
    "['run_jkl_1234','run_mno_7654']"
])
def test_get_run_source(dummy_recovery_id: str,
                        ) -> None:
    arguments = ["", "--run", dummy_recovery_id]
    with patch.object(sys, "argv", arguments):

        script_config = util.AmlRunScriptConfig()
        script_config = util.parse_args_and_update_config(script_config, arguments)

        if isinstance(script_config.run, List):
            assert isinstance(script_config.run[0], str)
        else:
            assert isinstance(script_config.run, str)


@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize("show_progress", [True, False])
def test_download_from_datastore(tmp_path: Path, overwrite: bool, show_progress: bool) -> None:
    """
    Test that download_from_datastore successfully downloads file from Blob Storage.
    Note that this will temporarily upload a file to the default datastore of the default workspace -
    (determined by either a config.json file, or by specifying workspace settings in the environment variables).
    After the test has completed, the blob will be deleted.
    """
    ws = DEFAULT_WORKSPACE.workspace
    default_datastore: AzureBlobDatastore = ws.get_default_datastore()
    dummy_file_content = "Hello world"
    local_data_path = tmp_path / "local_data"
    local_data_path.mkdir()
    test_data_path_remote = "test_data/abc"

    # Create dummy data files and upload to datastore (checking they are uploaded)
    dummy_filenames = []
    num_dummy_files = 2
    for i in range(num_dummy_files):
        dummy_filename = f"dummy_data_{i}.txt"
        dummy_filenames.append(dummy_filename)
        data_to_upload_path = local_data_path / dummy_filename
        data_to_upload_path.write_text(dummy_file_content)
    default_datastore.upload(str(local_data_path), test_data_path_remote, overwrite=False)
    existing_blobs = list(default_datastore.blob_service.list_blobs(prefix=test_data_path_remote,
                                                                    container_name=default_datastore.container_name))
    assert len(existing_blobs) == num_dummy_files

    # Check that the file doesn't currently exist at download location
    downloaded_data_path = tmp_path / "downloads"
    assert not downloaded_data_path.exists()

    # Now attempt to download
    util.download_from_datastore(default_datastore.name, test_data_path_remote, downloaded_data_path,
                                 aml_workspace=ws, overwrite=overwrite, show_progress=show_progress)
    expected_local_download_dir = downloaded_data_path / test_data_path_remote
    assert expected_local_download_dir.exists()
    expected_download_paths = [expected_local_download_dir / dummy_filename for dummy_filename in dummy_filenames]
    assert all([p.exists() for p in expected_download_paths])

    # Delete the file from Blob Storage
    container = default_datastore.container_name
    existing_blobs = list(default_datastore.blob_service.list_blobs(prefix=test_data_path_remote,
                                                                    container_name=container))
    for existing_blob in existing_blobs:
        default_datastore.blob_service.delete_blob(container_name=container, blob_name=existing_blob.name)


@pytest.mark.parametrize("overwrite", [True, False])
@pytest.mark.parametrize("show_progress", [True, False])
def test_upload_to_datastore(tmp_path: Path, overwrite: bool, show_progress: bool) -> None:
    """
    Test that upload_to_datastore successfully uploads a file to Blob Storage.
    Note that this will temporarily upload a file to the default datastore of the default workspace -
    (determined by either a config.json file, or by specifying workspace settings in the environment variables).
    After the test has completed, the blob will be deleted.
    """
    ws = DEFAULT_WORKSPACE.workspace
    default_datastore: AzureBlobDatastore = ws.get_default_datastore()
    container = default_datastore.container_name
    dummy_file_content = "Hello world"

    remote_data_dir = "test_data"
    dummy_file_name = Path("abc/uploaded_file.txt")
    expected_remote_path = Path(remote_data_dir) / dummy_file_name.name

    # check that the file doesnt already exist in Blob Storage
    existing_blobs = list(default_datastore.blob_service.list_blobs(prefix=str(expected_remote_path.as_posix()),
                                                                    container_name=container))
    assert len(existing_blobs) == 0

    # Create a dummy data file and upload to datastore
    data_to_upload_path = tmp_path / dummy_file_name
    data_to_upload_path.parent.mkdir(exist_ok=True, parents=True)
    data_to_upload_path.write_text(dummy_file_content)

    util.upload_to_datastore(default_datastore.name, data_to_upload_path.parent, Path(remote_data_dir),
                             aml_workspace=ws, overwrite=overwrite, show_progress=show_progress)
    existing_blobs = list(default_datastore.blob_service.list_blobs(prefix=str(expected_remote_path.as_posix()),
                                                                    container_name=container))
    assert len(existing_blobs) == 1

    # delete the blob from Blob Storage
    existing_blob: Blob = existing_blobs[0]
    default_datastore.blob_service.delete_blob(container_name=container, blob_name=existing_blob.name)


@pytest.mark.parametrize("arguments, run_id", [
    (["", "--run", "run_abc_123"], "run_abc_123"),
    (["", "--run", "run_abc_123,run_def_456"], ["run_abc_123", "run_def_456"]),
    (["", "--run", "expt_name:run_abc_123"], "expt_name:run_abc_123"),
])
def test_script_config_run_src(arguments: List[str], run_id: Union[str, List[str]]) -> None:
    with patch.object(sys, "argv", arguments):
        script_config = util.AmlRunScriptConfig()
        script_config = util.parse_args_and_update_config(script_config, arguments)

        if isinstance(run_id, list):
            for script_config_run, expected_run_id in zip(script_config.run, run_id):
                assert script_config_run == expected_run_id
        else:
            if len(run_id.split(util.EXPERIMENT_RUN_SEPARATOR)) > 1:
                assert script_config.run == [run_id.split(util.EXPERIMENT_RUN_SEPARATOR)[1]]
            else:
                assert script_config.run == [run_id]


@patch("health_azure.utils.download_files_from_run_id")
@patch("health_azure.utils.get_workspace")
def test_checkpoint_download(mock_get_workspace: MagicMock, mock_download_files: MagicMock) -> None:
    mock_workspace = MagicMock()
    mock_get_workspace.return_value = mock_workspace
    dummy_run_id = "run_def_456"
    prefix = "path/to/file"
    output_file_dir = Path("my_ouputs")
    util.download_checkpoints_from_run_id(dummy_run_id, prefix, output_file_dir, aml_workspace=mock_workspace)
    mock_download_files.assert_called_once_with(dummy_run_id, output_file_dir, prefix=prefix,
                                                workspace=mock_workspace, validate_checksum=True)


@pytest.mark.slow
def test_checkpoint_download_remote(tmp_path: Path) -> None:
    """
    Creates a large dummy file (around 250 MB) and ensures we can upload it to a Run and subsequently download
    with no issues, thus replicating the behaviour of downloading a large checkpoint file.
    """
    num_dummy_files = 1
    prefix = "outputs/checkpoints/"

    ws = DEFAULT_WORKSPACE.workspace
    experiment = Experiment(ws, AML_TESTS_EXPERIMENT)
    config = ScriptRunConfig(
        source_directory=".",
        command=["cd ."],  # command that does nothing
        compute_target="local"
    )
    with append_to_amlignore(
            amlignore=Path("") / AML_IGNORE_FILE,
            lines_to_append=DEFAULT_IGNORE_FOLDERS):
        run = experiment.submit(config)

    file_contents = "Hello world"
    file_name = ""  # for pyright
    for i in range(num_dummy_files):
        file_name = f"dummy_checkpoint_{i}.txt"
        large_file_path = tmp_path / file_name
        with open(str(large_file_path), "wb") as f_path:
            f_path.seek((1024 * 1024 * 240) - 1)
            f_path.write(bytearray(file_contents, encoding="UTF-8"))

        file_size = large_file_path.stat().st_size
        logging.info(f"File {i} size: {file_size}")

        local_path = str(large_file_path)
        run.upload_file(prefix + file_name, local_path)

    # Check the local dir is empty to begin with
    output_file_dir = tmp_path
    assert not (output_file_dir / prefix).exists()

    whole_file_path = prefix + file_name
    start_time = time.perf_counter()
    util.download_checkpoints_from_run_id(run.id, whole_file_path, output_file_dir, aml_workspace=ws)
    end_time = time.perf_counter()
    time_taken = end_time - start_time
    logging.info(f"Time taken to download file: {time_taken}")

    download_file_path = output_file_dir / prefix / "dummy_checkpoint_0.txt"
    assert (output_file_dir / prefix).is_dir()
    assert len(list((output_file_dir / prefix).iterdir())) == num_dummy_files
    found_file_contents = ""  # for pyright
    with open(str(download_file_path), "rb") as f_path:
        for line in f_path:
            chunk = line.strip(b'\x00')
            if chunk:
                found_file_contents = chunk.decode("utf-8")
                break

    assert found_file_contents == file_contents

    # Delete the file downloaded file and check that download_checkpoints also works on a single checkpoint file
    download_file_path.unlink()
    assert not download_file_path.exists()

    util.download_checkpoints_from_run_id(run.id, whole_file_path, output_file_dir, aml_workspace=ws)
    assert download_file_path.exists()
    with open(str(download_file_path), "rb") as f_path:
        for line in f_path:
            chunk = line.strip(b'\x00')
            if chunk:
                found_file_contents = chunk.decode("utf-8")
                break
    assert found_file_contents == file_contents


@pytest.mark.parametrize(("available", "initialized", "expected_barrier_called"),
                         [(False, True, False),
                          (True, False, False),
                          (False, False, False),
                          (True, True, True)])
@pytest.mark.fast
def test_torch_barrier(available: bool,
                       initialized: bool,
                       expected_barrier_called: bool) -> None:
    distributed = mock.MagicMock()
    distributed.is_available.return_value = available
    distributed.is_initialized.return_value = initialized
    distributed.barrier = mock.MagicMock()
    with mock.patch.dict("sys.modules", {"torch": mock.MagicMock(distributed=distributed)}):
        util.torch_barrier()
        if expected_barrier_called:
            distributed.barrier.assert_called_once()
        else:
            assert distributed.barrier.call_count == 0


class ParamEnum(Enum):
    EnumValue1 = "1",
    EnumValue2 = "2"


class IllegalCustomTypeNoFromString(param.Parameter):
    def _validate(self, val: Any) -> None:
        super()._validate(val)


class IllegalCustomTypeNoValidate(util.CustomTypeParam):
    def from_string(self, x: str) -> Any:
        return x


class DummyConfig(param.Parameterized):
    string_param = param.String()
    int_param = param.Integer()

    def validate(self) -> None:
        assert isinstance(self.string_param, str)
        assert isinstance(self.int_param, int)


@pytest.fixture(scope="module")
def dummy_model_config() -> DummyConfig:
    string_param = "dummy"
    int_param = 1
    return DummyConfig(param1=string_param, param2=int_param)


def test_add_and_validate(dummy_model_config: DummyConfig) -> None:
    new_string_param = "new_dummy"
    new_int_param = 2
    new_args = {"string_param": new_string_param, "int_param": new_int_param}
    util.set_fields_and_validate(dummy_model_config, new_args)

    assert dummy_model_config.string_param == new_string_param
    assert dummy_model_config.int_param == new_int_param


def test_create_argparse(dummy_model_config: DummyConfig) -> None:
    with patch("health_azure.utils._add_overrideable_config_args_to_parser") as mock_add_args:
        parser = util.create_argparser(dummy_model_config)
        mock_add_args.assert_called_once()
        assert isinstance(parser, ArgumentParser)


def test_add_args(dummy_model_config: DummyConfig) -> None:
    parser = ArgumentParser()
    # assert that calling parse_args on a default ArgumentParser returns an empty Namespace
    args = parser.parse_args([])
    assert args == Namespace()
    # now call _add_overrideable_config_args_to_parser and assert that calling parse_args on the result
    # of that is a non-empty Namepsace
    with patch("health_azure.utils.get_overridable_parameters") as mock_get_overridable_parameters:
        mock_get_overridable_parameters.return_value = {"string_param": param.String(default="Hello")}
        parser = util._add_overrideable_config_args_to_parser(dummy_model_config, parser)
        assert isinstance(parser, ArgumentParser)
        args = parser.parse_args([])
        assert args != Namespace()
        assert args.string_param == "Hello"


def test_parse_args(dummy_model_config: DummyConfig) -> None:
    new_string_arg = "dummy_string"
    new_args = ["--string_param", new_string_arg]
    parser = ArgumentParser()
    parser.add_argument("--string_param", type=str, default=None)
    parser_result = util.parse_arguments(parser, args=new_args)
    assert parser_result.args.get("string_param") == new_string_arg


class ParamClass(param.Parameterized):
    name: str = param.String(None, doc="Name")
    seed: int = param.Integer(42, doc="Seed")
    flag: bool = param.Boolean(False, doc="Flag")
    not_flag: bool = param.Boolean(True, doc="Not Flag")
    number: float = param.Number(3.14)
    integers: List[int] = param.List(None, class_=int)
    optional_int: Optional[int] = param.Integer(None, doc="Optional int")
    optional_float: Optional[float] = param.Number(None, doc="Optional float")
    floats: List[float] = param.List(None, class_=float)
    tuple1: Tuple[int, float] = param.NumericTuple((1, 2.3), length=2, doc="Tuple")
    int_tuple: Tuple[int, int, int] = util.IntTuple((1, 1, 1), length=3, doc="Integer Tuple")
    enum: ParamEnum = param.ClassSelector(default=ParamEnum.EnumValue1, class_=ParamEnum, instantiate=False)
    readonly: str = param.String("Nope", readonly=True)
    _non_override: str = param.String("Nope")
    constant: str = param.String("Nope", constant=True)
    other_args = util.ListOrDictParam(None, doc="List or dictionary of other args")

    def validate(self) -> None:
        pass


class ClassFrom(param.Parameterized):
    foo = param.String("foo")
    bar = param.Integer(1)
    baz = param.String("baz")
    _private = param.String("private")
    constant = param.String("constant", constant=True)


class ClassTo(param.Parameterized):
    foo = param.String("foo2")
    bar = param.Integer(2)
    _private = param.String("private2")
    constant = param.String("constant2", constant=True)


class NotParameterized:
    foo = 1


@pytest.fixture(scope="module")
def parameterized_config_and_parser() -> Tuple[ParamClass, ArgumentParser]:
    parameterized_config = ParamClass()
    parser = util.create_argparser(parameterized_config)
    return parameterized_config, parser


@pytest.mark.fast
def test_get_overridable_parameter(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser]) -> None:
    """
    Test to check overridable parameters are correctly identified.
    """
    parameterized_config = parameterized_config_and_parser[0]
    param_dict = util.get_overridable_parameters(parameterized_config)
    assert "name" in param_dict
    assert "flag" in param_dict
    assert "not_flag" in param_dict
    assert "seed" in param_dict
    assert "number" in param_dict
    assert "integers" in param_dict
    assert "optional_int" in param_dict
    assert "optional_float" in param_dict
    assert "tuple1" in param_dict
    assert "int_tuple" in param_dict
    assert "enum" in param_dict
    assert "other_args" in param_dict

    assert "readonly" not in param_dict
    assert "_non_override" not in param_dict
    assert "constant" not in param_dict


@pytest.mark.fast
def test_parser_defaults(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser]) -> None:
    """
    Check that default values are created as expected, and that the non-overridable parameters
    are omitted.
    """
    parameterized_config = parameterized_config_and_parser[0]
    defaults = vars(util.create_argparser(parameterized_config).parse_args([]))
    assert defaults["seed"] == 42
    assert defaults["tuple1"] == (1, 2.3)
    assert defaults["int_tuple"] == (1, 1, 1)
    assert defaults["enum"] == ParamEnum.EnumValue1
    assert not defaults["flag"]
    assert defaults["not_flag"]
    assert "readonly" not in defaults
    assert "constant" not in defaults
    assert "_non_override" not in defaults
    # We can't test if all invalid cases are handled because argparse call sys.exit
    # upon errors.


def check_parsing_succeeds(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser],
                           arg: List[str],
                           expected_key: str,
                           expected_value: Any) -> None:
    parameterized_config, parser = parameterized_config_and_parser
    parser_result = util.parse_arguments(parser, args=arg)
    assert parser_result.args.get(expected_key) == expected_value


def check_parsing_fails(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser],
                        arg: List[str]) -> None:
    parameterized_config, parser = parameterized_config_and_parser
    with pytest.raises(Exception):
        util.parse_arguments(parser, args=arg, fail_on_unknown_args=True)


@pytest.mark.fast
@pytest.mark.parametrize("args, expected_key, expected_value, expected_pass", [
    (["--name=foo"], "name", "foo", True),
    (["--seed", "42"], "seed", 42, True),
    (["--seed", ""], "seed", 42, True),
    (["--number", "2.17"], "number", 2.17, True),
    (["--number", ""], "number", 3.14, True),
    (["--integers", "1,2,3"], "integers", [1, 2, 3], True),
    (["--optional_int", ""], "optional_int", None, True),
    (["--optional_int", "2"], "optional_int", 2, True),
    (["--optional_float", ""], "optional_float", None, True),
    (["--optional_float", "3.14"], "optional_float", 3.14, True),
    (["--tuple1", "1,2"], "tuple1", (1, 2.0), True),
    (["--int_tuple", "1,2,3"], "int_tuple", (1, 2, 3), True),
    (["--enum=2"], "enum", ParamEnum.EnumValue2, True),
    (["--floats=1,2,3.14"], "floats", [1., 2., 3.14], True),
    (["--integers=1,2,3"], "integers", [1, 2, 3], True),
    (["--flag"], "flag", True, True),
    (["--no-flag"], None, None, False),
    (["--not_flag"], None, None, False),
    (["--no-not_flag"], "not_flag", False, True),
    (["--not_flag=false", "--no-not_flag"], None, None, False),
    (["--flag=Falsf"], None, None, False),
    (["--flag=Truf"], None, None, False),
    (["--other_args={'learning_rate': 0.5}"], "other_args", {'learning_rate': 0.5}, True),
    (["--other_args=['foo']"], "other_args", ["foo"], True),
    (["--other_args={'learning':3"], None, None, False),
    (["--other_args=['foo','bar'"], None, None, False)
])
def test_create_parser(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser],
                       args: List[str],
                       expected_key: str,
                       expected_value: Any,
                       expected_pass: bool) -> None:
    """
    Check that parse_args works as expected, with both non default and default values.
    """
    if expected_pass:
        check_parsing_succeeds(parameterized_config_and_parser, args, expected_key, expected_value)
    else:
        check_parsing_fails(parameterized_config_and_parser, args)


@pytest.mark.fast
@pytest.mark.parametrize("flag, expected_value", [
    ('on', True), ('t', True), ('true', True), ('y', True), ('yes', True), ('1', True),
    ('off', False), ('f', False), ('false', False), ('n', False), ('no', False), ('0', False)
])
def test_parsing_bools(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser],
                       flag: str,
                       expected_value: bool) -> None:
    """
    Check all the ways of passing in True and False, with and without the first letter capitialized
    """
    check_parsing_succeeds(parameterized_config_and_parser,
                           [f"--flag={flag}"],
                           "flag",
                           expected_value)
    check_parsing_succeeds(parameterized_config_and_parser,
                           [f"--flag={flag.capitalize()}"],
                           "flag",
                           expected_value)
    check_parsing_succeeds(parameterized_config_and_parser,
                           [f"--not_flag={flag}"],
                           "not_flag",
                           expected_value)
    check_parsing_succeeds(parameterized_config_and_parser,
                           [f"--not_flag={flag.capitalize()}"],
                           "not_flag",
                           expected_value)


@pytest.mark.fast
def test_apply_overrides(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser]) -> None:
    """
    Test that overrides are applied correctly, ond only to overridable parameters
    """
    parameterized_config = parameterized_config_and_parser[0]
    with patch("health_azure.utils.report_on_overrides") as mock_report_on_overrides:
        overrides = {"name": "newName", "int_tuple": (0, 1, 2)}
        actual_overrides = util.apply_overrides(parameterized_config, overrides)
        assert actual_overrides == overrides
        assert all([x == i and isinstance(x, int) for i, x in enumerate(parameterized_config.int_tuple)])
        assert parameterized_config.name == "newName"

        # Attempt to change seed and constant, but the latter should be ignored.
        change_seed = {"seed": 123}
        old_constant = parameterized_config.constant
        extra_overrides = {**change_seed, "constant": "Nothing"}  # type: ignore
        changes2 = util.apply_overrides(parameterized_config, overrides_to_apply=extra_overrides)  # type: ignore
        assert changes2 == change_seed
        assert parameterized_config.seed == 123
        assert parameterized_config.constant == old_constant

        # Check the call count of mock_validate and check it doesn't increase if should_validate is set to False
        # and that setting this flag doesn't affect on the outputs
        # mock_validate_call_count = mock_validate.call_count
        actual_overrides = util.apply_overrides(parameterized_config,
                                                overrides_to_apply=overrides,
                                                should_validate=False)
        assert actual_overrides == overrides
        # assert mock_validate.call_count == mock_validate_call_count

        # Check that report_on_overrides has not yet been called, but is called if keys_to_ignore is not None
        # and that setting this flag doesn't affect on the outputs
        assert mock_report_on_overrides.call_count == 0
        actual_overrides = util.apply_overrides(parameterized_config,
                                                overrides_to_apply=overrides,
                                                keys_to_ignore={"name"})
        assert actual_overrides == overrides
        assert mock_report_on_overrides.call_count == 1


def test_report_on_overrides(parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser],
                             caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    parameterized_config = parameterized_config_and_parser[0]
    old_logs = caplog.messages
    assert len(old_logs) == 0
    # the following overrides are expected to cause logged warnings because
    # a) parameter 'constant' is constant
    # b) parameter 'readonly' is readonly
    # b) parameter 'idontexist' is undefined (not the name of a parameter of ParamClass)
    overrides = {"constant": "dif_value", "readonly": "new_value", "idontexist": (0, 1, 2)}
    keys_to_ignore: Set = set()
    util.report_on_overrides(parameterized_config, overrides, keys_to_ignore)
    # Expect one warning message per failed override
    new_logs = caplog.messages
    expected_warnings = len(overrides.keys())
    assert len(new_logs) == expected_warnings, f"Expected {expected_warnings} warnings but found: {caplog.records}"


@pytest.mark.fast
@pytest.mark.parametrize("value_idx_0", [1.0, 1])
@pytest.mark.parametrize("value_idx_1", [2.0, 2])
@pytest.mark.parametrize("value_idx_2", [3.0, 3])
def test_int_tuple_validation(value_idx_0: Any, value_idx_1: Any, value_idx_2: Any,
                              parameterized_config_and_parser: Tuple[ParamClass, ArgumentParser]) -> None:
    """
    Test integer tuple parameter is validated correctly.
    """
    parameterized_config = parameterized_config_and_parser[0]
    val = (value_idx_0, value_idx_1, value_idx_2)
    if not all([isinstance(x, int) for x in val]):
        with pytest.raises(ValueError):
            parameterized_config.int_tuple = (value_idx_0, value_idx_1, value_idx_2)
    else:
        parameterized_config.int_tuple = (value_idx_0, value_idx_1, value_idx_2)


@pytest.mark.fast
def test_create_from_matching_params() -> None:
    """
    Test if Parameterized objects can be cloned by looking at matching fields.
    """
    class_from = ClassFrom()
    class_to = util.create_from_matching_params(class_from, cls_=ClassTo)
    assert isinstance(class_to, ClassTo)
    assert class_to.foo == "foo"
    assert class_to.bar == 1
    # Constant fields should not be touched
    assert class_to.constant == "constant2"
    # Private fields must be copied over.
    assert class_to._private == "private"
    # Baz is only present in the "from" object, and should not be copied to the new object
    assert not hasattr(class_to, "baz")

    with pytest.raises(ValueError) as ex:
        util.create_from_matching_params(class_from, NotParameterized)
    assert "subclass of param.Parameterized" in str(ex)
    assert "NotParameterized" in str(ex)


def test_parse_illegal_params() -> None:
    with pytest.raises(TypeError) as e:
        ParamClass(readonly="abc")
    assert "cannot be modified" in str(e.value)


def test_config_add_and_validate() -> None:
    config = ParamClass()
    assert config.name.startswith("ParamClass")
    util.set_fields_and_validate(config, {"name": "foo"})
    assert config.name == "foo"

    assert hasattr(config, "new_property") is False
    util.set_fields_and_validate(config, {"new_property": "bar"})
    assert hasattr(config, "new_property") is True
    assert config.new_property == "bar"


class IllegalParamClassNoString(param.Parameterized):
    custom_type_no_from_string = IllegalCustomTypeNoFromString(
        None, doc="This should fail since from_string method is missing"
    )


def test_cant_parse_param_type() -> None:
    """
    Assert that a TypeError is raised when trying to add a custom type with no from_string method as an argument
    """
    config = IllegalParamClassNoString()

    with pytest.raises(TypeError) as e:
        util.create_argparser(config)
        assert "is not supported" in str(e.value)


# Another custom type (from docs/source/conmmandline_tools.md)
class EvenNumberParam(util.CustomTypeParam):
    """ Our custom type param for even numbers """

    def _validate(self, val: Any) -> None:
        if (not self.allow_None) and val is None:
            raise ValueError("Value must not be None")
        if val % 2 != 0:
            raise ValueError(f"{val} is not an even number")
        super()._validate(val)  # type: ignore

    def from_string(self, x: str) -> int:
        return int(x)


class MyScriptConfig(param.Parameterized):
    simple_string: str = param.String(default="")
    even_number: int = EvenNumberParam(2, doc="your choice of even number", allow_None=False)


def test_parse_args_and_apply_overrides() -> None:
    config = MyScriptConfig()
    assert config.even_number == 2
    assert config.simple_string == ""

    new_even_number = config.even_number * 2
    new_string = config.simple_string + "something_new"
    config_w_results = util.parse_args_and_update_config(config, ["--even_number", str(new_even_number),
                                                                  "--simple_string", new_string])
    assert config_w_results.even_number == new_even_number
    assert config_w_results.simple_string == new_string

    # parsing args with unaccepted values should cause an exception to be raised
    odd_number = new_even_number + 1
    with pytest.raises(ValueError) as e:
        util.parse_args_and_update_config(config, args=["--even_number", f"{odd_number}"])
        assert "not an even number" in str(e.value)

    none_number = "None"
    with pytest.raises(ArgumentError):
        util.parse_args_and_update_config(config, args=["--even_number", f"{none_number}"])

    # Mock from_string to check test _validate
    mock_from_string_none = lambda a, b: None  # type: ignore
    with patch.object(EvenNumberParam, "from_string", new=mock_from_string_none):
        # Check that _validate fails with None value
        with pytest.raises(ValueError) as e:
            util.parse_args_and_update_config(config, ["--even_number", f"{none_number}"])
            assert "must not be None" in str(e.value)


class MockChildRun:
    def __init__(self, run_id: str, cross_val_index: int):
        self.run_id = run_id
        self.tags = {"hyperparameters": json.dumps({"child_run_index": cross_val_index})}

    def get_metrics(self) -> Dict[str, Union[float, List[Union[int, float]]]]:
        num_epochs = 5
        return {
            "epoch": list(range(num_epochs)),
            "train/loss": [np.random.rand() for _ in range(num_epochs)],
            "train/auroc": [np.random.rand() for _ in range(num_epochs)],
            "val/loss": [np.random.rand() for _ in range(num_epochs)],
            "val/recall": [np.random.rand() for _ in range(num_epochs)],
            "test/f1score": np.random.rand(),
            "test/accuracy": np.random.rand()
        }


class MockHyperDriveRun:
    def __init__(self, num_children: int) -> None:
        self.num_children = num_children

    def get_children(self) -> List[MockChildRun]:
        return [MockChildRun(f"run_abc_{i}456", i) for i in range(self.num_children)]


def test_download_files_from_hyperdrive_children(tmp_path: Path) -> None:
    def _mock_get_tags(run: Any, arg_name: Any) -> Dict[str, str]:
        return run.id

    def _mock_download_file(child_run_id: str, local_folder_child_run: Path, prefix: Optional[str] = None) -> None:
        prefix = prefix or ""  # for pyright
        expected_path = local_folder_child_run / prefix
        expected_path.touch()

    num_child_runs = 2
    hyperparam_name = "crossval_index"
    remote_file_path = "dummy_file.csv"
    local_download_folder = tmp_path / "downloaded_hyperdrive"
    local_download_folder.mkdir(exist_ok=False)
    assert len(list(local_download_folder.iterdir())) == 0

    mock_run = MagicMock()
    mock_run_1, mock_run_2 = MagicMock(id=1), MagicMock(id=2)

    with patch("health_azure.utils.download_files_from_run_id", new=_mock_download_file):
        with patch("health_azure.utils.get_tags_from_hyperdrive_run", new=_mock_get_tags):
            mock_run.get_children.return_value = [mock_run_1, mock_run_2]
            util.download_files_from_hyperdrive_children(mock_run, remote_file_path, local_download_folder,
                                                         hyperparam_name=hyperparam_name)

    assert len(list(local_download_folder.iterdir())) == num_child_runs
    assert (local_download_folder / str(mock_run_1.id)).is_dir()
    assert (local_download_folder / str(mock_run_1.id) / remote_file_path).exists()


@patch("health_azure.utils.isinstance", return_value=True)
def test_aggregate_hyperdrive_metrics(_: MagicMock) -> None:
    ws = DEFAULT_WORKSPACE.workspace
    num_crossval_splits = 2
    with patch("health_azure.utils.get_aml_run_from_run_id") as mock_get_run:
        mock_get_run.return_value = MockHyperDriveRun(num_crossval_splits)
        df = util.aggregate_hyperdrive_metrics("run_id_123", "child_run_index", aml_workspace=ws)
        num_rows, num_cols = df.shape
        assert num_rows == 7  # The number of metrics specified in MockChildRun.get_metrics
        assert num_cols == num_crossval_splits
        epochs = df.loc["epoch"]
        assert isinstance(epochs[0], list)
        test_accuracies = df.loc["test/accuracy"]
        assert isinstance(test_accuracies[0], float)


def test_create_run() -> None:
    """
    Test if we can create an AML run object here in the test suite, write logs and read them back in.
    """
    run_name = "foo"
    experiment_name = "himl-tests"
    run: Optional[Run] = None
    try:
        run = util.create_aml_run_object(experiment_name=experiment_name, run_name=run_name,
                                         workspace=DEFAULT_WORKSPACE.workspace)
        assert run is not None
        assert run.name == run_name
        assert run.experiment.name == experiment_name
        metric_name = "mymetric"
        metric_value = 1.234
        run.log(metric_name, metric_value)
        run.flush()
        metrics = run.get_metrics(name=metric_name)
        assert metrics[metric_name] == metric_value
    finally:
        if run is not None:
            run.complete()
