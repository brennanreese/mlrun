# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import warnings
from datetime import datetime
from typing import Dict, List, Optional, Union

import pandas as pd
from storey import EmitEveryEvent, EmitPolicy

import mlrun
import mlrun.api.schemas

from ..config import config as mlconf
from ..datastore import get_store_uri
from ..datastore.targets import (
    TargetTypes,
    convert_wasb_schema_to_az,
    default_target_names,
    get_offline_target,
    get_online_target,
    get_target_driver,
    update_targets_run_id_for_ingest,
    validate_target_list,
    validate_target_placement,
)
from ..features import Entity, Feature
from ..model import (
    DataSource,
    DataTarget,
    DataTargetBase,
    ModelObj,
    ObjectList,
    VersionedObjMetadata,
)
from ..runtimes.function_reference import FunctionReference
from ..serving.states import BaseStep, RootFlowStep, previous_step
from ..serving.utils import StepToDict
from ..utils import StorePrefix, logger
from .common import verify_feature_set_permissions

aggregates_step = "Aggregates"


class FeatureAggregation(ModelObj):
    """feature aggregation requirements"""

    def __init__(
        self, name=None, column=None, operations=None, windows=None, period=None
    ):
        self.name = name
        self.column = column
        self.operations = operations or []
        self.windows = windows or []
        self.period = period


class FeatureSetSpec(ModelObj):
    def __init__(
        self,
        owner=None,
        description=None,
        entities=None,
        features=None,
        partition_keys=None,
        timestamp_key=None,
        label_column=None,
        relations=None,
        source=None,
        targets=None,
        graph=None,
        function=None,
        analysis=None,
        engine=None,
        output_path=None,
    ):
        self._features: ObjectList = None
        self._entities: ObjectList = None
        self._targets: ObjectList = None
        self._graph: RootFlowStep = None
        self._source = None
        self._engine = None
        self._function: FunctionReference = None

        self.owner = owner
        self.description = description
        self.entities: List[Union[Entity, str]] = entities or []
        self.features: List[Feature] = features or []
        self.partition_keys = partition_keys or []
        self.timestamp_key = timestamp_key
        self.relations = relations or {}
        self.source = source
        self.targets = targets or []
        self.graph = graph
        self.label_column = label_column
        self.function = function
        self.analysis = analysis or {}
        self.engine = engine
        self.output_path = output_path or mlconf.artifact_path

    @property
    def entities(self) -> List[Entity]:
        """feature set entities (indexes)"""
        return self._entities

    @entities.setter
    def entities(self, entities: List[Union[Entity, str]]):
        if entities:
            # if the entity is a string, convert it to Entity class
            for i, entity in enumerate(entities):
                if isinstance(entity, str):
                    entities[i] = Entity(entity)
        self._entities = ObjectList.from_list(Entity, entities)

    @property
    def features(self) -> List[Feature]:
        """feature set features list"""
        return self._features

    @features.setter
    def features(self, features: List[Feature]):
        self._features = ObjectList.from_list(Feature, features)

    @property
    def targets(self) -> List[DataTargetBase]:
        """list of desired targets (material storage)"""
        return self._targets

    @targets.setter
    def targets(self, targets: List[DataTargetBase]):
        self._targets = ObjectList.from_list(DataTargetBase, targets)

    @property
    def engine(self) -> str:
        """feature set processing engine (storey, pandas, spark)"""
        return self._engine

    @engine.setter
    def engine(self, engine: str):
        engine_list = ["pandas", "spark", "storey"]
        if engine and engine not in engine_list:
            raise mlrun.errors.MLRunInvalidArgumentError(
                f"engine must be one of {','.join(engine_list)}"
            )
        self.graph.engine = "sync" if engine and engine in ["pandas", "spark"] else None
        self._engine = engine

    @property
    def graph(self) -> RootFlowStep:
        """feature set transformation graph/DAG"""
        return self._graph

    @graph.setter
    def graph(self, graph):
        self._graph = self._verify_dict(graph, "graph", RootFlowStep)
        self._graph.engine = (
            "sync" if self.engine and self.engine in ["pandas", "spark"] else None
        )

    @property
    def function(self) -> FunctionReference:
        """reference to template graph processing function"""
        return self._function

    @function.setter
    def function(self, function):
        self._function = self._verify_dict(function, "function", FunctionReference)

    @property
    def source(self) -> DataSource:
        """feature set data source definitions"""
        return self._source

    @source.setter
    def source(self, source: DataSource):
        self._source = self._verify_dict(source, "source", DataSource)

    def require_processing(self):
        return len(self._graph.steps) > 0


class FeatureSetStatus(ModelObj):
    def __init__(
        self,
        state=None,
        targets=None,
        stats=None,
        preview=None,
        function_uri=None,
        run_uri=None,
    ):
        self.state = state or "created"
        self._targets: ObjectList = None
        self.targets = targets or []
        self.stats = stats or {}
        self.preview = preview or []
        self.function_uri = function_uri
        self.run_uri = run_uri

    @property
    def targets(self) -> List[DataTarget]:
        """list of material storage targets + their status/path"""
        return self._targets

    @targets.setter
    def targets(self, targets: List[DataTarget]):
        self._targets = ObjectList.from_list(DataTarget, targets)

    def update_target(self, target: DataTarget):
        self._targets.update(target)

    def update_last_written_for_target(self, target_path: str, last_written: datetime):
        for target in self._targets:
            actual_target_path = get_target_driver(target).get_target_path()
            if (
                actual_target_path == target_path
                or actual_target_path.rstrip("/") == target_path
            ):
                target.last_written = last_written


def emit_policy_to_dict(policy: EmitPolicy):
    # Storey expects the policy to be converted to a dictionary with specific params and won't allow extra params
    # (see Storey's _dict_to_emit_policy function). This takes care of creating a dict conforming to it.
    # TODO - fix Storey's handling of emit policy and parsing of dict in _dict_to_emit_policy.
    struct = {"mode": policy.name()}
    if hasattr(policy, "delay_in_seconds"):
        struct["delay"] = getattr(policy, "delay_in_seconds")
    if hasattr(policy, "max_events"):
        struct["maxEvents"] = getattr(policy, "max_events")
    return struct


class FeatureSet(ModelObj):
    """Feature set object, defines a set of features and their data pipeline"""

    kind = mlrun.api.schemas.ObjectKind.feature_set.value
    _dict_fields = ["kind", "metadata", "spec", "status"]

    def __init__(
        self,
        name: str = None,
        description: str = None,
        entities: List[Union[Entity, str]] = None,
        timestamp_key: str = None,
        engine: str = None,
        label_column: str = None,
    ):
        """Feature set object, defines a set of features and their data pipeline

        example::

            import mlrun.feature_store as fstore
            ticks = fstore.FeatureSet("ticks", entities=["stock"], timestamp_key="timestamp")
            fstore.ingest(ticks, df)

        :param name:          name of the feature set
        :param description:   text description
        :param entities:      list of entity (index key) names or :py:class:`~mlrun.features.FeatureSet.Entity`
        :param timestamp_key: timestamp column name
        :param engine:        name of the processing engine (storey, pandas, or spark), defaults to storey
        :param label_column:  name of the label column (the one holding the target (y) values)
        """
        self._spec: FeatureSetSpec = None
        self._metadata = None
        self._status = None
        self._api_client = None
        self._run_db = None

        self.spec = FeatureSetSpec(
            description=description,
            entities=entities,
            timestamp_key=timestamp_key,
            engine=engine,
            label_column=label_column,
        )

        if timestamp_key in self.spec.entities.keys():
            raise mlrun.errors.MLRunInvalidArgumentError(
                "timestamp key can not be entity"
            )

        self.metadata = VersionedObjMetadata(name=name)
        self.status = None
        self._last_state = ""
        self._aggregations = {}

    @property
    def spec(self) -> FeatureSetSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, "spec", FeatureSetSpec)

    @property
    def metadata(self) -> VersionedObjMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, metadata):
        self._metadata = self._verify_dict(metadata, "metadata", VersionedObjMetadata)

    @property
    def status(self) -> FeatureSetStatus:
        return self._status

    @status.setter
    def status(self, status):
        self._status = self._verify_dict(status, "status", FeatureSetStatus)

    @property
    def uri(self):
        """fully qualified feature set uri"""
        return get_store_uri(StorePrefix.FeatureSet, self.fullname)

    @property
    def fullname(self) -> str:
        """full name in the form {project}/{name}[:{tag}]"""
        fullname = (
            f"{self._metadata.project or mlconf.default_project}/{self._metadata.name}"
        )
        if self._metadata.tag:
            fullname += ":" + self._metadata.tag
        return fullname

    def _override_run_db(
        self,
        session,
    ):
        # Import here, since this method only runs in API context. If this import was global, client would need
        # API requirements and would fail.
        from ..api.api.utils import get_run_db_instance

        self._run_db = get_run_db_instance(session)

    def _get_run_db(self):
        if self._run_db:
            return self._run_db
        else:
            return mlrun.get_run_db()

    def get_target_path(self, name=None):
        """get the url/path for an offline or specified data target"""
        target = get_offline_target(self, name=name)

        if not target and name:
            target = get_online_target(self, name)

        if target:
            return target.get_path().get_absolute_path()

    def set_targets(
        self,
        targets=None,
        with_defaults=True,
        default_final_step=None,
        default_final_state=None,
    ):
        """set the desired target list or defaults

        :param targets:  list of target type names ('csv', 'nosql', ..) or target objects
                         CSVTarget(), ParquetTarget(), NoSqlTarget(), StreamTarget(), ..
        :param with_defaults: add the default targets (as defined in the central config)
        :param default_final_step: the final graph step after which we add the
                                    target writers, used when the graph branches and
                                    the end cant be determined automatically
        :param default_final_state: *Deprecated* - use default_final_step instead
        """
        if default_final_state:
            warnings.warn(
                "The default_final_state parameter is deprecated. Use default_final_step instead",
                # TODO: In 0.7.0 do changes in examples & demos In 0.9.0 remove
                PendingDeprecationWarning,
            )
            default_final_step = default_final_step or default_final_state

        if targets is not None and not isinstance(targets, list):
            raise mlrun.errors.MLRunInvalidArgumentError(
                "targets can only be None or a list of kinds or DataTargetBase derivatives"
            )
        targets = targets or []
        if with_defaults:
            targets.extend(default_target_names())

        validate_target_list(targets=targets)

        for target in targets:
            kind = target.kind if hasattr(target, "kind") else target
            if kind not in TargetTypes.all():
                raise mlrun.errors.MLRunInvalidArgumentError(
                    f"target kind is not supported, use one of: {','.join(TargetTypes.all())}"
                )
            if not hasattr(target, "kind"):
                target = DataTargetBase(target, name=str(target))
            if target.path is not None and (
                target.path.startswith("wasb") or target.path.startswith("wasbs")
            ):
                convert_wasb_schema_to_az(target)
            self.spec.targets.update(target)
        if default_final_step:
            self.spec.graph.final_step = default_final_step

    def purge_targets(self, target_names: List[str] = None, silent: bool = False):
        """Delete data of specific targets
        :param target_names: List of names of targets to delete (default: delete all ingested targets)
        :param silent: Fail silently if target doesn't exist in featureset status"""

        verify_feature_set_permissions(
            self, mlrun.api.schemas.AuthorizationAction.delete
        )

        purge_targets = self._reload_and_get_status_targets(
            target_names=target_names, silent=silent
        )

        if purge_targets:
            purge_target_names = list(purge_targets.keys())
            for target_name in purge_target_names:
                target = purge_targets[target_name]
                driver = get_target_driver(target_spec=target, resource=self)
                try:
                    driver.purge()
                except FileNotFoundError:
                    pass
                del self.status.targets[target_name]

            self.save()

    def update_targets_for_ingest(
        self,
        targets: List[DataTargetBase],
        overwrite: bool = None,
    ):
        if not targets:
            return

        ingestion_target_names = [t.name for t in targets]

        status_targets = {}
        if not overwrite:
            # silent=True always because targets are not guaranteed to be found in status
            status_targets = (
                self._reload_and_get_status_targets(
                    target_names=ingestion_target_names, silent=True
                )
                or {}
            )

        update_targets_run_id_for_ingest(overwrite, targets, status_targets)

    def _reload_and_get_status_targets(
        self, target_names: List[str] = None, silent: bool = False
    ):
        try:
            self.reload(update_spec=False)
        except mlrun.errors.MLRunNotFoundError:
            # If the feature set doesn't exist in DB there shouldn't be any target to delete
            if silent:
                return
            else:
                raise

        if target_names:
            targets = ObjectList(DataTarget)
            for target_name in target_names:
                try:
                    targets[target_name] = self.status.targets[target_name]
                except KeyError:
                    if silent:
                        pass
                    else:
                        raise mlrun.errors.MLRunNotFoundError(
                            "Target not found in status (fset={0}, target={1})".format(
                                self.metadata.name, target_name
                            )
                        )
        else:
            targets = self.status.targets

        return targets

    def has_valid_source(self):
        """check if object's spec has a valid (non empty) source definition"""
        source = self.spec.source
        return source is not None and source.path is not None and source.path != "None"

    def add_entity(
        self,
        name: str,
        value_type: mlrun.data_types.ValueType = None,
        description: str = None,
        labels: Optional[Dict[str, str]] = None,
    ):
        """add/set an entity (dataset index)

        example::

            import mlrun.feature_store as fstore

            ticks = fstore.FeatureSet("ticks",
                            entities=["stock"],
                            timestamp_key="timestamp")
            ticks.add_entity("country",
                            mlrun.data_types.ValueType.STRING,
                            description="stock country")
            ticks.add_entity("year", mlrun.data_types.ValueType.INT16)
            ticks.save()

        :param name:        entity name
        :param value_type:  type of the entity (default to ValueType.STRING)
        :param description: description of the entity
        :param labels:      label tags dict
        """
        entity = Entity(name, value_type, description=description, labels=labels)
        self._spec.entities.update(entity, name)

    def add_feature(self, feature: mlrun.features.Feature, name=None):
        """add/set a feature

        example::

            import mlrun.feature_store as fstore
            from mlrun.features import Feature

            ticks = fstore.FeatureSet("ticks",
                            entities=["stock"],
                            timestamp_key="timestamp")
            ticks.add_feature(Feature(value_type=mlrun.data_types.ValueType.STRING,
                            description="client consistency"),"ABC01")
            ticks.add_feature(Feature(value_type=mlrun.data_types.ValueType.FLOAT,
                            description="client volatility"),"SAB")
            ticks.save()

        :param feature:         setting of Feature
        :param name:            feature name
        """
        self._spec.features.update(feature, name)

    def link_analysis(self, name, uri):
        """add a linked file/artifact (chart, data, ..)"""
        self._spec.analysis[name] = uri

    @property
    def graph(self):
        """feature set transformation graph/DAG"""
        return self.spec.graph

    def _add_aggregation_to_existing(self, new_aggregation):
        name = new_aggregation["name"]
        if name in self._aggregations:
            current_aggr = self._aggregations[name]
            if current_aggr["windows"] != new_aggregation["windows"]:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    f"Aggregation with name {name} already exists but with window {current_aggr['windows']}. "
                    f"Please provide name for the aggregation"
                )
            if current_aggr["period"] != new_aggregation["period"]:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    f"Aggregation with name {name} already exists but with period {current_aggr['period']}. "
                    f"Please provide name for the aggregation"
                )
            if current_aggr["column"] != new_aggregation["column"]:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    f"Aggregation with name {name} already exists but for different column {current_aggr['column']}. "
                    f"Please provide name for the aggregation"
                )
            current_aggr["operations"] = list(
                set(current_aggr["operations"] + new_aggregation["operations"])
            )

            return
        self._aggregations[name] = new_aggregation

    def add_aggregation(
        self,
        column,
        operations,
        windows,
        period=None,
        name=None,
        step_name=None,
        after=None,
        before=None,
        state_name=None,
        emit_policy: EmitPolicy = None,
    ):
        """add feature aggregation rule

        example::

            myset.add_aggregation("ask", ["sum", "max"], "1h", "10m", name="asks")

        :param column:     name of column/field aggregate. Do not name columns starting with either `_` or `aggr_`.
                           They are reserved for internal use, and the data does not ingest correctly.
                           When using the pandas engine, do not use spaces (` `) or periods (`.`) in the column names;
                           they cause errors in the ingestion.
        :param operations: aggregation operations, e.g. ['sum', 'std']
        :param windows:    time windows, can be a single window, e.g. '1h', '1d',
                            or a list of same unit windows e.g. ['1h', '6h']
                            windows are transformed to fixed windows or
                            sliding windows depending whether period parameter
                            provided.

                            - Sliding window is fixed-size overlapping windows
                              that slides with time.
                              The window size determines the size of the sliding window
                              and the period determines the step size to slide.
                              Period must be integral divisor of the window size.
                              If the period is not provided then fixed windows is used.

                            - Fixed window is fixed-size, non-overlapping, gap-less window.
                              The window is referred to as a tumbling window.
                              In this case, each record on an in-application stream belongs
                              to a specific window. It is processed only once
                              (when the query processes the window to which the record belongs).
        :param period:     optional, sliding window granularity, e.g. '20s' '10m'  '3h' '7d'
        :param name:       optional, aggregation name/prefix. Must be unique per feature set. If not passed,
                            the column will be used as name.
        :param step_name: optional, graph step name
        :param state_name: *Deprecated* - use step_name instead
        :param after:      optional, after which graph step it runs
        :param before:     optional, comes before graph step
        :param emit_policy: optional, which emit policy to use when performing the aggregations. Use the derived
                            classes of ``storey.EmitPolicy``. The default is to emit every period for Spark engine
                            and emit every event for storey. Currently the only other supported option is to use
                            ``emit_policy=storey.EmitEveryEvent()`` when using the Spark engine to emit every event

        """
        if isinstance(operations, str):
            raise mlrun.errors.MLRunInvalidArgumentError(
                "Invalid parameters provided - operations must be a list."
            )
        if state_name:
            warnings.warn(
                "The state_name parameter is deprecated. Use step_name instead",
                # TODO: In 0.7.0 do changes in examples & demos In 0.9.0 remove
                PendingDeprecationWarning,
            )
            step_name = step_name or state_name

        name = name or column

        if isinstance(windows, str):
            windows = [windows]
        if isinstance(operations, str):
            operations = [operations]

        aggregation = FeatureAggregation(
            name, column, operations, windows, period
        ).to_dict()

        def upsert_feature(name):
            if name in self.spec.features:
                self.spec.features[name].aggregate = True
            else:
                self.spec.features[name] = Feature(
                    name=column, aggregate=True, value_type="float"
                )

        step_name = step_name or aggregates_step
        graph = self.spec.graph
        if step_name in graph.steps:
            step = graph.steps[step_name]
            self._add_aggregation_to_existing(aggregation)
            step.class_args["aggregates"] = list(self._aggregations.values())
            if emit_policy and self.spec.engine == "spark":
                # Using simple override here - we might want to consider exploding if different emit policies
                # were used for multiple aggregations.
                emit_policy_dict = emit_policy_to_dict(emit_policy)
                if "emit_policy" in step.class_args:
                    curr_emit_policy = step.class_args["emit_policy"]["mode"]
                    if curr_emit_policy != emit_policy_dict["mode"]:
                        logger.warning(
                            f"Current emit policy will be overridden: {curr_emit_policy} => {emit_policy_dict['mode']}"
                        )
                step.class_args["emit_policy"] = emit_policy_dict
        else:
            class_args = {}
            self._aggregations[aggregation["name"]] = aggregation
            if not self.spec.engine or self.spec.engine == "storey":
                step = graph.add_step(
                    name=step_name,
                    after=after or previous_step,
                    before=before,
                    class_name="storey.AggregateByKey",
                    aggregates=[aggregation],
                    table=".",
                    **class_args,
                )
            elif self.spec.engine == "spark":
                key_columns = []
                if emit_policy:
                    class_args["emit_policy"] = emit_policy_to_dict(emit_policy)
                for entity in self.spec.entities:
                    key_columns.append(entity.name)
                step = graph.add_step(
                    name=step_name,
                    key_columns=key_columns,
                    time_column=self.spec.timestamp_key,
                    aggregates=[aggregation],
                    after=after or previous_step,
                    before=before,
                    class_name="mlrun.feature_store.feature_set.SparkAggregateByKey",
                    **class_args,
                )
            else:
                raise ValueError(
                    "Aggregations are only implemented for storey and spark engines."
                )

        for operation in operations:
            for window in windows:
                upsert_feature(f"{name}_{operation}_{window}")

        return step

    def get_stats_table(self):
        """get feature statistics table (as dataframe)"""
        if self.status.stats:
            return pd.DataFrame.from_dict(self.status.stats, orient="index")

    def __getitem__(self, name):
        return self._spec.features[name]

    def __setitem__(self, key, item):
        self._spec.features.update(item, key)

    def plot(self, filename=None, format=None, with_targets=False, **kw):
        """generate graphviz plot"""
        graph = self.spec.graph
        _, default_final_step, _ = graph.check_and_process_graph(allow_empty=True)
        targets = None
        if with_targets:
            validate_target_list(targets=targets)
            validate_target_placement(graph, default_final_step, self.spec.targets)
            targets = [
                BaseStep(
                    target.kind,
                    after=target.after_step or default_final_step,
                    shape="cylinder",
                )
                for target in self.spec.targets
            ]
        return graph.plot(filename, format, targets=targets, **kw)

    def to_dataframe(
        self,
        columns=None,
        df_module=None,
        target_name=None,
        start_time=None,
        end_time=None,
        time_column=None,
        **kwargs,
    ):
        """return featureset (offline) data as dataframe

        :param columns:      list of columns to select (if not all)
        :param df_module:    py module used to create the DataFrame (pd for Pandas, dd for Dask, ..)
        :param target_name:  select a specific target (material view)
        :param start_time:   filter by start time
        :param end_time:     filter by end time
        :param time_column:  specify the time column name in the file
        :param kwargs:       additional reader (csv, parquet, ..) args
        :return: DataFrame
        """
        entities = list(self.spec.entities.keys())
        if columns:
            if self.spec.timestamp_key and self.spec.timestamp_key not in entities:
                columns = [self.spec.timestamp_key] + columns
            columns = entities + columns
        driver = get_offline_target(self, name=target_name)
        if not driver:
            raise mlrun.errors.MLRunNotFoundError(
                "there are no offline targets for this feature set"
            )
        return driver.as_df(
            columns=columns,
            df_module=df_module,
            entities=entities,
            start_time=start_time,
            end_time=end_time,
            time_column=time_column,
            **kwargs,
        )

    def save(self, tag="", versioned=False):
        """save to mlrun db"""
        db = self._get_run_db()
        self.metadata.project = self.metadata.project or mlconf.default_project
        tag = tag or self.metadata.tag or "latest"
        as_dict = self.to_dict()
        as_dict["spec"]["features"] = as_dict["spec"].get(
            "features", []
        )  # bypass DB bug
        db.store_feature_set(as_dict, tag=tag, versioned=versioned)

    def reload(self, update_spec=True):
        """reload/sync the feature vector status and spec from the DB"""
        feature_set = self._get_run_db().get_feature_set(
            self.metadata.name, self.metadata.project, self.metadata.tag
        )
        if isinstance(feature_set, dict):
            feature_set = FeatureSet.from_dict(feature_set)

        self.status = feature_set.status
        if update_spec:
            self.spec = feature_set.spec


class SparkAggregateByKey(StepToDict):
    def __init__(
        self,
        key_columns: List[str],
        time_column: str,
        aggregates: List[Dict],
        emit_policy: Union[EmitPolicy, Dict] = None,
    ):
        self.key_columns = key_columns
        self.time_column = time_column
        self.aggregates = aggregates
        self.emit_policy_mode = None
        if emit_policy:
            if isinstance(emit_policy, EmitPolicy):
                emit_policy = emit_policy_to_dict(emit_policy)
            self.emit_policy_mode = emit_policy["mode"]

    @staticmethod
    def _duration_to_spark_format(duration):
        num = duration[:-1]
        unit = duration[-1:]
        if unit == "d":
            unit = "day"
        elif unit == "h":
            unit = "hour"
        elif unit == "m":
            unit = "minute"
        elif unit == "s":
            unit = "second"
        else:
            raise ValueError(f"Invalid duration '{duration}'")
        return f"{num} {unit}"

    def _extract_fields_from_aggregate_dict(self, aggregate):
        name = aggregate["name"]
        column = aggregate["column"]
        operations = aggregate["operations"]
        windows = aggregate["windows"]
        spark_period = (
            self._duration_to_spark_format(aggregate["period"])
            if "period" in aggregate
            else None
        )
        return name, column, operations, windows, spark_period

    def do(self, event):
        import pyspark.sql.functions as funcs
        from pyspark.sql import Window

        time_column = self.time_column or "time"
        input_df = event

        if not self.emit_policy_mode or self.emit_policy_mode != EmitEveryEvent.name():
            last_value_aggs = [
                funcs.last(column).alias(column)
                for column in input_df.columns
                if column not in self.key_columns and column != time_column
            ]

            dfs = []
            for aggregate in self.aggregates:
                (
                    name,
                    column,
                    operations,
                    windows,
                    spark_period,
                ) = self._extract_fields_from_aggregate_dict(aggregate)

                for window in windows:
                    spark_window = self._duration_to_spark_format(window)
                    aggs = last_value_aggs
                    for operation in operations:
                        func = getattr(funcs, operation)
                        agg_name = f"{name if name else column}_{operation}_{window}"
                        agg = func(column).alias(agg_name)
                        aggs.append(agg)
                    window_column = funcs.window(
                        time_column, spark_window, spark_period
                    )
                    df = input_df.groupBy(
                        *self.key_columns,
                        window_column.end.alias(time_column),
                    ).agg(*aggs)
                    df = df.withColumn(f"{time_column}_window", funcs.lit(window))
                    dfs.append(df)

            union_df = dfs[0]
            for df in dfs[1:]:
                union_df = union_df.unionByName(df, allowMissingColumns=True)

            return union_df

        else:
            window_counter = 0
            # We'll use this column to identify our original row and group-by across the various windows
            # (either sliding windows or multiple windows provided). See below comment for more details.
            rowid_col = "__mlrun_rowid"
            df = input_df.withColumn(rowid_col, funcs.monotonically_increasing_id())

            drop_columns = [rowid_col]
            window_rank_cols = []
            union_df = None
            for aggregate in self.aggregates:
                (
                    name,
                    column,
                    operations,
                    windows,
                    spark_period,
                ) = self._extract_fields_from_aggregate_dict(aggregate)

                for window in windows:
                    spark_window = self._duration_to_spark_format(window)
                    window_col = f"__mlrun_window_{window_counter}"
                    win_df = df.withColumn(
                        window_col,
                        funcs.window(time_column, spark_window, spark_period).end,
                    )
                    function_window = Window.partitionBy(*self.key_columns, window_col)

                    window_rank_col = f"__mlrun_win_rank_{window_counter}"
                    rank_window = Window.partitionBy(rowid_col).orderBy(window_col)
                    win_df = win_df.withColumn(
                        window_rank_col, funcs.row_number().over(rank_window)
                    )
                    window_rank_cols.append(window_rank_col)
                    drop_columns.extend([window_col, window_rank_col])

                    window_counter += 1

                    for operation in operations:
                        func = getattr(funcs, operation)
                        agg_name = f"{name if name else column}_{operation}_{window}"
                        win_df = win_df.withColumn(
                            agg_name, func(column).over(function_window)
                        )

                    union_df = (
                        union_df.unionByName(win_df, allowMissingColumns=True)
                        if union_df
                        else win_df
                    )

            # We need to collapse the multiple window rows that were generated during the query processing. For that
            # purpose we'll pick just the 1st row for each window, and then group-by with ignorenulls. Basically since
            # the result is a union of multiple windows, we'll get something like this for each input row:
            # row   window_1    rank_window_1   window_2    rank_window_2   ...calculations and fields...
            # 1     10:00       1               null        null            ...
            # 2     10:10       2               null        null            ...
            # 3     null        null            10:00       1               ...
            # 4     null        null            10:10       2               ...
            # And we want to take rows 1 and 3 in this case. Then the group-by will merge them to a single line since
            # it ignores nulls, so it will take the values for window_1 from row 1 and for window_2 from row 3.
            window_filter = " or ".join(
                [f"{window_rank_col} == 1" for window_rank_col in window_rank_cols]
            )
            first_value_aggs = [
                funcs.first(column, ignorenulls=True).alias(column)
                for column in union_df.columns
                if column not in drop_columns
            ]

            return (
                union_df.filter(window_filter)
                .groupBy(rowid_col)
                .agg(*first_value_aggs)
                .drop(rowid_col)
            )
