import collections
import dataclasses
import json
import os
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import v3io
import v3io.dataplane

import mlrun
import mlrun.api.schemas
import mlrun.data_types.infer
import mlrun.run
import mlrun.utils
import mlrun.utils.model_monitoring
import mlrun.utils.v3io_clients
from mlrun.utils import logger

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f%z"


class DriftStatus(Enum):
    """
    Enum for the drift status values.
    """

    NO_DRIFT = "NO_DRIFT"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    POSSIBLE_DRIFT = "POSSIBLE_DRIFT"


# A type for representing a drift result, a tuple of the status and the drift mean:
DriftResultType = Tuple[DriftStatus, float]


@dataclasses.dataclass
class TotalVarianceDistance:
    """
    Provides a symmetric drift distance between two periods t and u
    Z - vector of random variables
    Pt - Probability distribution over time span t

    :args distrib_t: array of distribution t (usually the latest dataset distribution)
    :args distrib_u: array of distribution u (usually the sample dataset distribution)
    """

    distrib_t: np.ndarray
    distrib_u: np.ndarray

    NAME: ClassVar[str] = "tvd"

    def compute(self) -> float:
        """
        Calculate Total Variance distance.

        :returns:  Total Variance Distance.
        """
        return np.sum(np.abs(self.distrib_t - self.distrib_u)) / 2


@dataclasses.dataclass
class HellingerDistance:
    """
    Hellinger distance is an f divergence measure, similar to the Kullback-Leibler (KL) divergence.
    It used to quantify the difference between two probability distributions.
    However, unlike KL Divergence the Hellinger divergence is symmetric and bounded over a probability space.
    The output range of Hellinger distance is [0,1]. The closer to 0, the more similar the two distributions.

    :args distrib_t: array of distribution t (usually the latest dataset distribution)
    :args distrib_u: array of distribution u (usually the sample dataset distribution)
    """

    distrib_t: np.ndarray
    distrib_u: np.ndarray

    NAME: ClassVar[str] = "hellinger"

    def compute(self) -> float:
        """
        Calculate Hellinger Distance

        :returns: Hellinger Distance
        """
        return np.sqrt(
            0.5 * ((np.sqrt(self.distrib_u) - np.sqrt(self.distrib_t)) ** 2).sum()
        )


@dataclasses.dataclass
class KullbackLeiblerDivergence:
    """
    KL Divergence (or relative entropy) is a measure of how one probability distribution differs from another.
    It is an asymmetric measure (thus it's not a metric) and it doesn't satisfy the triangle inequality.
    KL Divergence of 0, indicates two identical distributions.

    :args distrib_t: array of distribution t (usually the latest dataset distribution)
    :args distrib_u: array of distribution u (usually the sample dataset distribution)
    """

    distrib_t: np.ndarray
    distrib_u: np.ndarray

    NAME: ClassVar[str] = "kld"

    def compute(self, capping: float = None, kld_scaling: float = 1e-4) -> float:
        """
        :param capping:      A bounded value for the KL Divergence. For infinite distance, the result is replaced with
                             the capping value which indicates a huge differences between the distributions.
        :param kld_scaling:  Will be used to replace 0 values for executing the logarithmic operation.

        :returns: KL Divergence
        """
        t_u = np.sum(
            np.where(
                self.distrib_t != 0,
                (self.distrib_t)
                * np.log(
                    self.distrib_t
                    / np.where(self.distrib_u != 0, self.distrib_u, kld_scaling)
                ),
                0,
            )
        )
        u_t = np.sum(
            np.where(
                self.distrib_u != 0,
                (self.distrib_u)
                * np.log(
                    self.distrib_u
                    / np.where(self.distrib_t != 0, self.distrib_t, kld_scaling)
                ),
                0,
            )
        )
        result = t_u + u_t
        if capping:
            return capping if result == float("inf") else result
        return result


class VirtualDrift:
    """
    Virtual Drift object is used for handling the drift calculations.
    It contains the metrics objects and the related methods for the detection of potential drift.
    """

    def __init__(
        self,
        prediction_col: Optional[str] = None,
        label_col: Optional[str] = None,
        feature_weights: Optional[List[float]] = None,
        inf_capping: Optional[float] = 10,
    ):
        """
        Initialize a Virtual Drift object.

        :param prediction_col:  The name of the dataframe column which represents the predictions of the model. If
                                provided, it will be used for calculating drift over the predictions. The name of the
                                dataframe column which represents the labels of the model. If provided, it will be used
                                for calculating drift over the labels.
        :param feature_weights: Weights that can be applied to the features and to be considered during the drift
                                analysis.
        :param inf_capping:     A bounded value for the results of the statistical metric. For example, when calculating
                                KL divergence and getting infinite distance between the two distributions, the result
                                will be replaced with the capping value.
        """
        self.prediction_col = prediction_col
        self.label_col = label_col
        self.feature_weights = feature_weights
        self.capping = inf_capping

        # initialize objects of the current metrics
        self.metrics = {
            TotalVarianceDistance.NAME: TotalVarianceDistance,
            HellingerDistance.NAME: HellingerDistance,
            KullbackLeiblerDivergence.NAME: KullbackLeiblerDivergence,
        }

    @staticmethod
    def dict_to_histogram(histogram_dict: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
        """
        Convert histogram dictionary to pandas DataFrame with feature histograms as columns

        :param histogram_dict: Histogram dictionary

        :returns: Histogram dataframe
        """

        # create a dictionary with feature histograms as values
        histograms = {}
        for feature, stats in histogram_dict.items():
            histograms[feature] = stats["hist"][0]

        # convert the dictionary to pandas DataFrame
        histograms = pd.DataFrame(histograms)

        # normalize to probability distribution of each feature
        histograms = histograms / histograms.sum()

        return histograms

    def compute_metrics_over_df(
        self,
        base_histogram: Dict[str, Dict[str, Any]],
        latest_histogram: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """
        Calculate metrics values for each feature.

        For example:
        {tvd: {feature_1: 0.001, feature_2: 0.2: ,...}}

        :param base_histogram:   histogram dataframe that represents the distribution of the features from the original
                                 training set.
        :param latest_histogram: Histogram dataframe that represents the distribution of the features from the latest
                                 input batch.

        :returns: A dictionary in which for each metric (key) we assign the values for each feature.
        """

        # compute the different metrics for each feature distribution and store the results in dictionary
        drift_measures = {}
        for metric_name, metric in self.metrics.items():
            drift_measures[metric_name] = {
                feature: metric(
                    base_histogram.loc[:, feature], latest_histogram.loc[:, feature]
                ).compute()
                for feature in base_histogram
            }

        return drift_measures

    def compute_drift_from_histograms(
        self,
        feature_stats: Dict[str, Dict[str, Any]],
        current_stats: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """
        Compare the distributions of both the original features data and the latest input data
        :param feature_stats: Histogram dictionary of the original feature dataset that was used in the model training.
        :param current_stats: Histogram dictionary of the recent input data

        :returns: A dictionary that includes the drift results for each feature.

        """

        # convert histogram dictionaries to DataFrame of the histograms
        # with feature histogram as cols
        base_histogram = self.dict_to_histogram(feature_stats)
        latest_histogram = self.dict_to_histogram(current_stats)

        # verify all the features exist between datasets
        base_features = set(base_histogram.columns)
        latest_features = set(latest_histogram.columns)
        features_common = list(base_features.intersection(latest_features))
        feature_difference = list(base_features ^ latest_features)
        if not features_common:
            raise ValueError(
                f"No common features found: {base_features} <> {latest_features}"
            )

        # drop columns of non-exist features
        base_histogram = base_histogram.drop(
            feature_difference, axis=1, errors="ignore"
        )
        latest_histogram = latest_histogram.drop(
            feature_difference, axis=1, errors="ignore"
        )

        # compute the statistical metrics per feature
        features_drift_measures = self.compute_metrics_over_df(
            base_histogram.loc[:, features_common],
            latest_histogram.loc[:, features_common],
        )

        # compute total value for each metric
        for metric_name in self.metrics.keys():
            feature_values = list(features_drift_measures[metric_name].values())
            features_drift_measures[metric_name]["total_sum"] = np.sum(feature_values)
            features_drift_measures[metric_name]["total_mean"] = np.mean(feature_values)

            # add weighted mean by given feature weights if provided
            if self.feature_weights:
                features_drift_measures[metric_name]["total_weighted_mean"] = np.dot(
                    feature_values, self.feature_weights
                )

        # define drift result dictionary with values as a dictionary
        drift_result = collections.defaultdict(dict)

        # fill drift result dictionary with the statistical metrics results per feature
        # and the total sum and mean of each metric
        for feature in features_common:
            for metric, values in features_drift_measures.items():
                drift_result[feature][metric] = values[feature]
                sum = features_drift_measures[metric]["total_sum"]
                mean = features_drift_measures[metric]["total_mean"]
                drift_result[f"{metric}_sum"] = sum
                drift_result[f"{metric}_mean"] = mean
                if self.feature_weights:
                    metric_measure = features_drift_measures[metric]
                    weighted_mean = metric_measure["total_weighted_mean"]
                    drift_result[f"{metric}_weighted_mean"] = weighted_mean

        # compute the drift metric over the labels
        if self.label_col:
            label_drift_measures = self.compute_metrics_over_df(
                base_histogram.loc[:, self.label_col],
                latest_histogram.loc[:, self.label_col],
            )
            for metric, values in label_drift_measures.items():
                drift_result[self.label_col][metric] = values[metric]

        # compute the drift metric over the predictions
        if self.prediction_col:
            prediction_drift_measures = self.compute_metrics_over_df(
                base_histogram.loc[:, self.prediction_col],
                latest_histogram.loc[:, self.prediction_col],
            )
            for metric, values in prediction_drift_measures.items():
                drift_result[self.prediction_col][metric] = values[metric]

        return drift_result

    @staticmethod
    def check_for_drift_per_feature(
        metrics_results_dictionary: Dict[str, Union[float, dict]],
        possible_drift_threshold: float = 0.5,
        drift_detected_threshold: float = 0.7,
    ) -> Dict[str, DriftResultType]:
        """
        Check for drift based on the defined decision rule and the calculated results of the statistical metrics per
        feature.

        :param metrics_results_dictionary: Dictionary of statistical metrics results per feature and the total means of
                                           all features.
        :param possible_drift_threshold:   Threshold for the calculated result to be in a possible drift status.
                                           Defaulted to 0.5.
        :param drift_detected_threshold:   Threshold for the calculated result to be in a drift detected status.
                                           Defaulted to 0.7.

        :returns: A dictionary of all the features and their drift status and results tuples, tuple of:
                  [0] = Drift status enum based on the thresholds given.
                  [1] = The drift result (float) based on the mean of the Total Variance Distance and the Hellinger
                        distance.
        """
        # Initialize the drift results dictionary:
        drift_results = {}

        # Calculate the result per feature:
        for feature, results in metrics_results_dictionary.items():
            # A feature result must be a dictionary, otherwise it's the total mean (float):
            if not isinstance(results, dict):
                continue
            # Calculate the feature's drift mean:
            tvd = results[TotalVarianceDistance.NAME]
            hellinger = results[HellingerDistance.NAME]
            metrics_results_dictionary = (tvd + hellinger) / 2
            # Decision rule for drift detection:
            drift_status = VirtualDrift._get_drift_status(
                drift_result=metrics_results_dictionary,
                possible_drift_threshold=possible_drift_threshold,
                drift_detected_threshold=drift_detected_threshold,
            )
            # Collect the drift result:
            drift_results[feature] = (drift_status, metrics_results_dictionary)

        return drift_results

    @staticmethod
    def check_for_drift(
        metrics_results_dictionary: Dict[str, Union[float, dict]],
        possible_drift_threshold: float = 0.5,
        drift_detected_threshold: float = 0.7,
    ) -> DriftResultType:
        """
        Check for drift based on the defined decision rule and the calculated results of the statistical metrics by the
        mean of all features.

        :param metrics_results_dictionary: Dictionary of statistical metrics results per feature and the total means of
                                           all features.
        :param possible_drift_threshold:   Threshold for the calculated result to be in a possible drift status.
                                           Defaulted to 0.5.
        :param drift_detected_threshold:   Threshold for the calculated result to be in a drift detected status.
                                           Defaulted to 0.7.

        :returns: A tuple of:
                  [0] = Drift status enum based on the thresholds given.
                  [1] = The drift result (float) based on the mean of the Total Variance Distance and the Hellinger
                        distance.
        """
        # Calculate the mean drift result:
        tvd_mean = metrics_results_dictionary[f"{TotalVarianceDistance.NAME}_mean"]
        hellinger_mean = metrics_results_dictionary.get(
            f"{HellingerDistance.NAME}_mean"
        )
        drift_result = 0.0
        if tvd_mean and hellinger_mean:
            drift_result = (tvd_mean + hellinger_mean) / 2

        # Decision rule for drift detection:
        drift_status = VirtualDrift._get_drift_status(
            drift_result=drift_result,
            possible_drift_threshold=possible_drift_threshold,
            drift_detected_threshold=drift_detected_threshold,
        )

        return drift_status, drift_result

    @staticmethod
    def _get_drift_status(
        drift_result: float,
        possible_drift_threshold: float,
        drift_detected_threshold: float,
    ) -> DriftStatus:
        """
        Get the drift status according to the result and thresholds given.

        :param drift_result:             The drift result.
        :param possible_drift_threshold: Threshold for the calculated result to be in a possible drift status.
        :param drift_detected_threshold: Threshold for the calculated result to be in a drift detected status.

        :return: The figured drift status.
        """
        drift_status = DriftStatus.NO_DRIFT
        if drift_result >= drift_detected_threshold:
            drift_status = DriftStatus.DRIFT_DETECTED
        elif drift_result >= possible_drift_threshold:
            drift_status = DriftStatus.POSSIBLE_DRIFT

        return drift_status


class BatchProcessor:
    """
    The main object to handle the batch processing job. This object is used to get the required configurations and
    to manage the main monitoring drift detection process based on the current batch.
    Note that the BatchProcessor object requires access keys along with valid project configurations.
    """

    def __init__(
        self,
        context: mlrun.run.MLClientCtx,
        project: str,
        model_monitoring_access_key: str,
        v3io_access_key: str,
    ):

        """
        Initialize Batch Processor object.

        :param context:                     An MLRun context.
        :param project:                     Project name.
        :param model_monitoring_access_key: Access key to apply the model monitoring process.
        :param v3io_access_key:             Token key for v3io.

        """
        self.context = context
        self.project = project

        self.v3io_access_key = v3io_access_key
        self.model_monitoring_access_key = (
            model_monitoring_access_key or v3io_access_key
        )

        # initialize virtual drift object
        self.virtual_drift = VirtualDrift(inf_capping=10)

        # define the required paths for the project objects.
        # note that the kv table, tsdb, and the input stream paths are located at the default location
        # while the parquet path is located at the user-space location
        template = mlrun.utils.config.model_endpoint_monitoring.store_prefixes.default
        kv_path = template.format(project=self.project, kind="endpoints")
        (
            _,
            self.kv_container,
            self.kv_path,
        ) = mlrun.utils.model_monitoring.parse_model_endpoint_store_prefix(kv_path)
        tsdb_path = template.format(project=project, kind="events")
        (
            _,
            self.tsdb_container,
            self.tsdb_path,
        ) = mlrun.utils.model_monitoring.parse_model_endpoint_store_prefix(tsdb_path)
        stream_path = template.format(project=self.project, kind="log_stream")
        (
            _,
            self.stream_container,
            self.stream_path,
        ) = mlrun.utils.model_monitoring.parse_model_endpoint_store_prefix(stream_path)
        self.parquet_path = mlrun.utils.config.model_endpoint_monitoring.store_prefixes.user_space.format(
            project=project, kind="parquet"
        )

        logger.info(
            "Initializing BatchProcessor",
            project=project,
            model_monitoring_access_key_initalized=bool(model_monitoring_access_key),
            v3io_access_key_initialized=bool(v3io_access_key),
            parquet_path=self.parquet_path,
            kv_container=self.kv_container,
            kv_path=self.kv_path,
            tsdb_container=self.tsdb_container,
            tsdb_path=self.tsdb_path,
            stream_container=self.stream_container,
            stream_path=self.stream_path,
        )

        # get drift thresholds from the model monitoring configuration
        self.default_possible_drift_threshold = (
            mlrun.utils.config.model_endpoint_monitoring.drift_thresholds.default.possible_drift
        )
        self.default_drift_detected_threshold = (
            mlrun.utils.config.model_endpoint_monitoring.drift_thresholds.default.drift_detected
        )

        # get a runtime database
        self.db = mlrun.get_run_db()

        # get the frames clients based on the v3io configuration
        # it will be used later for writing the results into the tsdb
        self.v3io = mlrun.utils.v3io_clients.get_v3io_client(
            access_key=self.v3io_access_key
        )
        self.frames = mlrun.utils.v3io_clients.get_frames_client(
            address=mlrun.utils.config.v3io_framesd,
            container=self.tsdb_container,
            token=self.v3io_access_key,
        )

        # if an error occurs, it will be raised using the following argument
        self.exception = None

    def post_init(self):
        """
        Preprocess of the batch processing.
        """

        # create v3io stream based on the input stream
        response = self.v3io.create_stream(
            container=self.stream_container,
            path=self.stream_path,
            shard_count=1,
            raise_for_status=v3io.dataplane.RaiseForStatus.never,
            access_key=self.v3io_access_key,
        )

        if not (response.status_code == 400 and "ResourceInUse" in str(response.body)):
            response.raise_for_status([409, 204, 403])

    def run(self):
        """
        Main method for manage the drift analysis and write the results into tsdb and KV table.
        """
        # Get model endpoints (each deployed project has at least 1 serving model):
        try:
            endpoints = self.db.list_model_endpoints(self.project)
        except Exception as e:
            logger.error("Failed to list endpoints", exc=e)
            return

        active_endpoints = set()
        for endpoint in endpoints.endpoints:
            if endpoint.spec.active:
                active_endpoints.add(endpoint.metadata.uid)

        store, sub = mlrun.store_manager.get_or_create_store(self.parquet_path)
        prefix = self.parquet_path.replace(sub, "")
        fs = store.get_filesystem(silent=False)

        if not fs.exists(sub):
            logger.warn(f"{sub} does not exist")
            return

        for endpoint_dir in fs.ls(sub):
            endpoint_id = endpoint_dir["name"].split("=")[-1]
            if endpoint_id not in active_endpoints:
                continue

        # Perform drift analysis for each model endpoint:
        for endpoint_id in active_endpoints:
            try:
                last_year = self.get_last_created_dir(fs, endpoint_dir)
                last_month = self.get_last_created_dir(fs, last_year)
                last_day = self.get_last_created_dir(fs, last_month)
                last_hour = self.get_last_created_dir(fs, last_day)

                full_path = f"{prefix}{last_hour['name']}"

                logger.info(f"Now processing {full_path}")

                # Get model endpoint object:
                endpoint = self.db.get_model_endpoint(
                    project=self.project, endpoint_id=endpoint_id
                )

                # Skip router endpoint:
                if (
                    endpoint.status.endpoint_type
                    == mlrun.utils.model_monitoring.EndpointType.ROUTER
                ):
                    # endpoint.status.feature_stats is None
                    logger.info(f"{endpoint_id} is router skipping")
                    continue

                df = pd.read_parquet(full_path)

                # Get the timestamp of the latest request:
                timestamp = df["timestamp"].iloc[-1]

                # Create DataFrame based on the input features:
                named_features_df = list(df["named_features"])
                named_features_df = pd.DataFrame(named_features_df)

                # Get the current stats that are represented by histogram of each feature within the dataset. In the
                # following dictionary, each key is a feature with dictionary of stats (including histogram
                # distribution) as a value:
                current_stats = mlrun.data_types.infer.DFDataInfer.get_stats(
                    df=named_features_df,
                    options=mlrun.data_types.infer.InferOptions.Histogram,
                )

                # Compute the drift based on the histogram of the current stats and the histogram of the original
                # feature stats that can be found in the model endpoint object:
                drift_result = self.virtual_drift.compute_drift_from_histograms(
                    feature_stats=endpoint.status.feature_stats,
                    current_stats=current_stats,
                )
                logger.info("Drift result", drift_result=drift_result)

                # Get drift thresholds from the model configuration:
                monitor_configuration = endpoint.spec.monitor_configuration or {}
                possible_drift = monitor_configuration.get(
                    "possible_drift", self.default_possible_drift_threshold
                )
                drift_detected = monitor_configuration.get(
                    "drift_detected", self.default_drift_detected_threshold
                )

                # Check for possible drift based on the results of the statistical metrics defined above:
                drift_status, drift_measure = self.virtual_drift.check_for_drift(
                    metrics_results_dictionary=drift_result,
                    possible_drift_threshold=possible_drift,
                    drift_detected_threshold=drift_detected,
                )
                logger.info(
                    "Drift status",
                    endpoint_id=endpoint_id,
                    drift_status=drift_status.value,
                    drift_measure=drift_measure,
                )

                # If drift was detected, add the results to the input stream
                if (
                    drift_status == DriftStatus.POSSIBLE_DRIFT
                    or drift_status == DriftStatus.DRIFT_DETECTED
                ):
                    self.v3io.stream.put_records(
                        container=self.stream_container,
                        stream_path=self.stream_path,
                        records=[
                            {
                                "data": json.dumps(
                                    {
                                        "endpoint_id": endpoint_id,
                                        "drift_status": drift_status.value,
                                        "drift_measure": drift_measure,
                                        "drift_per_feature": {**drift_result},
                                    }
                                )
                            }
                        ],
                    )

                # Update the results in the KV table:
                self.v3io.kv.update(
                    container=self.kv_container,
                    table_path=self.kv_path,
                    key=endpoint_id,
                    attributes={
                        "current_stats": json.dumps(current_stats),
                        "drift_measures": json.dumps(drift_result),
                        "drift_status": drift_status.value,
                    },
                )

                # Update the results in tsdb:
                tsdb_drift_measures = {
                    "endpoint_id": endpoint_id,
                    "timestamp": pd.to_datetime(timestamp, format=_TIME_FORMAT),
                    "record_type": "drift_measures",
                    "tvd_mean": drift_result["tvd_mean"],
                    "kld_mean": drift_result["kld_mean"],
                    "hellinger_mean": drift_result["hellinger_mean"],
                }

                self.frames.write(
                    backend="tsdb",
                    table=self.tsdb_path,
                    dfs=pd.DataFrame.from_dict([tsdb_drift_measures]),
                    index_cols=["timestamp", "endpoint_id", "record_type"],
                )

                # logger.info(f"Done updating drift measures {full_path}")

            except Exception as e:
                logger.error(f"Exception for endpoint {endpoint_id}")
                self.exception = e

    @staticmethod
    def get_last_created_dir(fs, endpoint_dir):
        dirs = fs.ls(endpoint_dir["name"])
        last_dir = sorted(dirs, key=lambda k: k["name"].split("=")[-1])[-1]
        return last_dir


def handler(context: mlrun.run.MLClientCtx):
    batch_processor = BatchProcessor(
        context=context,
        project=context.project,
        model_monitoring_access_key=os.environ.get("MODEL_MONITORING_ACCESS_KEY"),
        v3io_access_key=os.environ.get("V3IO_ACCESS_KEY"),
    )
    batch_processor.post_init()
    batch_processor.run()
    if batch_processor.exception:
        raise batch_processor.exception
