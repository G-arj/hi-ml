#  -------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  -------------------------------------------------------------------------------------------

from pathlib import Path
from typing import Dict, Sequence

import dateutil.parser
import numpy as np
import pandas as pd
from azureml.core import Experiment, Run, Workspace

from health_azure.utils import (aggregate_hyperdrive_metrics, download_file_if_necessary, get_aml_run_from_run_id,
                                get_tags_from_hyperdrive_run)


def collect_crossval_outputs(parent_run_id: str, download_dir: Path, aml_workspace: Workspace,
                             crossval_arg_name: str = "cross_validation_split_index",
                             output_filename: str = "test_output.csv",
                             overwrite: bool = False) -> Dict[int, pd.DataFrame]:
    """Fetch output CSV files from cross-validation runs as dataframes.

    Will only download the CSV files if they do not already exist locally.

    :param parent_run_id: Azure ML run ID for the parent Hyperdrive run.
    :param download_dir: Base directory where to download the CSV files. A new sub-directory will
        be created for each child run (e.g. `<download_dir>/<crossval index>/*.csv`).
    :param aml_workspace: Azure ML workspace in which the runs were executed.
    :param crossval_arg_name: Name of the Hyperdrive argument used for indexing the child runs.
    :param output_filename: Filename of the output CSVs to download.
    :param overwrite: Whether to force the download even if each file already exists locally.
    :return: A dictionary of dataframes with the sorted cross-validation indices as keys.
    """
    parent_run = get_aml_run_from_run_id(parent_run_id, aml_workspace)

    all_outputs_dfs = {}
    for child_run in parent_run.get_children():
        child_run_index = get_tags_from_hyperdrive_run(child_run, crossval_arg_name)
        if child_run_index is None:
            raise ValueError(f"Child run expected to have the tag '{crossval_arg_name}'")
        child_dir = download_dir / str(child_run_index)
        try:
            remote_filename = "outputs/" + output_filename
            child_csv = download_file_if_necessary(child_run, remote_filename, child_dir / output_filename,
                                                   overwrite=overwrite)
            all_outputs_dfs[child_run_index] = pd.read_csv(child_csv)
        except Exception as e:
            print(f"Failed to download {output_filename} for run {child_run.id}: {e}")
    return dict(sorted(all_outputs_dfs.items()))


def collect_crossval_metrics(parent_run_id: str, download_dir: Path, aml_workspace: Workspace,
                             crossval_arg_name: str = "cross_validation_split_index",
                             overwrite: bool = False) -> pd.DataFrame:
    """Fetch metrics logged to Azure ML from cross-validation runs as a dataframe.

    Will only download the metrics if they do not already exist locally, as this can take several
    seconds for each child run.

    :param parent_run_id: Azure ML run ID for the parent Hyperdrive run.
    :param download_dir: Directory where to save the downloaded metrics as `aml_metrics.json`.
    :param aml_workspace: Azure ML workspace in which the runs were executed.
    :param crossval_arg_name: Name of the Hyperdrive argument used for indexing the child runs.
    :param overwrite: Whether to force the download even if metrics are already saved locally.
    :return: A dataframe in the format returned by :py:func:`~health_azure.aggregate_hyperdrive_metrics()`.
    """
    metrics_json = download_dir / "aml_metrics.json"
    if not overwrite and metrics_json.is_file():
        print(f"AML metrics file already exists at {metrics_json}")
        metrics_df = pd.read_json(metrics_json)
    else:
        metrics_df = aggregate_hyperdrive_metrics(run_id=parent_run_id,
                                                  child_run_arg_name=crossval_arg_name,
                                                  aml_workspace=aml_workspace)
        metrics_json.parent.mkdir(parents=True, exist_ok=True)
        print(f"Writing AML metrics file to {metrics_json}")
        metrics_df.to_json(metrics_json)
    return metrics_df.sort_index(axis='columns')


def get_crossval_metrics_table(metrics_df: pd.DataFrame, metrics_list: Sequence[str]) -> pd.DataFrame:
    """Format raw cross-validation metrics into a table with a summary "Mean ± Std" column.

    Note that this function only supports scalar metrics. To format metrics that are logged
    throughout training, you should call :py:func:`get_best_epoch_metrics()` first.

    :param metrics_df: Metrics dataframe, as returned by :py:func:`collect_crossval_metrics()` and
        :py:func:`~health_azure.aggregate_hyperdrive_metrics()`.
    :param metrics_list: The list of metrics to include in the table.
    :return: A dataframe with the values of the selected metrics formatted as strings, including a
        header and a summary column.
    """
    header = ["Metric"] + [f"Split {k}" for k in metrics_df.columns] + ["Mean ± Std"]
    metrics_rows = []
    for metric in metrics_list:
        values: pd.Series = metrics_df.loc[metric]
        mean = values.mean()
        std = values.std()
        row = [metric] + [f"{v:.3f}" for v in values] + [f"{mean:.3f} ± {std:.3f}"]
        metrics_rows.append(row)
    table = pd.DataFrame(metrics_rows, columns=header).set_index(header[0])
    return table


def get_best_epochs(metrics_df: pd.DataFrame, primary_metric: str, maximise: bool = True) -> Dict[int, int]:
    """Determine the best epoch for each cross-validation run based on a given metric.

    The returned epoch indices are relative to the logging frequency of the chosen metric, i.e.
    should not be mixed between pipeline stages that log metrics at different epoch intervals.

    :param metrics_df: Metrics dataframe, as returned by :py:func:`collect_crossval_metrics()` and
        :py:func:`~health_azure.aggregate_hyperdrive_metrics()`.
    :param primary_metric: Name of the reference metric to optimise.
    :param maximise: Whether the given metric should be maximised (minimised if `False`).
    :return: Dictionary mapping each cross-validation index to its best epoch.
    """
    best_fn = np.argmax if maximise else np.argmin
    best_epochs = metrics_df.loc[primary_metric].apply(best_fn)
    return best_epochs.to_dict()


def get_best_epoch_metrics(metrics_df: pd.DataFrame, metrics_list: Sequence[str],
                           best_epochs: Dict[int, int]) -> pd.DataFrame:
    """Extract the values of the selected cross-validation metrics at the given best epochs.

    The `best_epoch` indices are relative to the logging frequency of the chosen primary metric,
    i.e. the metrics in `metrics_list` must have been logged at the same epoch intervals.

    :param metrics_df: Metrics dataframe, as returned by :py:func:`collect_crossval_metrics()` and
        :py:func:`~health_azure.aggregate_hyperdrive_metrics()`.
    :param metrics_list: Names of the metrics to index by the best epoch indices provided. Their
        values in `metrics_df` should be lists.
    :param best_epochs: Dictionary of cross-validation indices to best epochs, as returned by
        :py:func:`get_best_epochs()`.
    :return: Dataframe with the same columns as `metrics_df` and rows specified by `metrics_list`,
        containing only scalar values.
    """
    best_metrics = [metrics_df.loc[metrics_list, k].apply(lambda values: values[epoch])
                    for k, epoch in best_epochs.items()]
    best_metrics_df = pd.DataFrame(best_metrics).T
    return best_metrics_df


def get_formatted_run_info(parent_run: Run) -> str:
    """Format Azure ML cross-validation run information as HTML.

    Includes details of the parent and child runs, as well as submission information.

    :param parent_run: Parent Hyperdrive Azure ML run object.
    :return: Formatted HTML string.
    """
    def format_experiment(experiment: Experiment) -> str:
        return f"<a href={experiment.get_portal_url()}>{experiment.name}</a>"

    def format_run(run: Run) -> str:
        return f"<a href={run.get_portal_url()}>{run.display_name}</a> ({run.id}, {run.get_status()})"

    def format_submission_info(run: Run) -> str:
        details = run.get_details()
        start_time = dateutil.parser.parse(details['startTimeUtc'])
        return f"Started on {start_time.strftime('%d %b %Y %H:%M %Z')} by {details['submittedBy']}"

    html = f"<p>Experiment: {format_experiment(parent_run.experiment)}"
    html += f"\n<br>Parent run: {format_run(parent_run)}"

    html += "\n<ul>"
    for k, child_run in enumerate(sorted(parent_run.get_children(), key=lambda r: r.id)):
        html += f"\n<li>Child {k}: {format_run(child_run)}</li>"
    html += "\n</ul>"

    html += f"\n<p>{format_submission_info(parent_run)}"
    html += f"\n<p>Command-line arguments: <code>{parent_run.get_tags()['commandline_args']}</code>"
    return html
