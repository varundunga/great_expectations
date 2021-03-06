import logging
import traceback
from collections import Iterable
from typing import Any, Dict, List, Tuple

import numpy as np

from great_expectations.execution_engine.execution_engine import MetricDomainTypes

try:
    from sqlalchemy.engine import RowProxy
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import Label, TextClause, WithinGroup
    from sqlalchemy.sql.selectable import CTE
except ImportError:
    RowProxy = None
    ProgrammingError = None
    Select = None
    Label = None
    TextClaus = None
    WithinGroup = None
    CTE = None

from great_expectations.execution_engine import (
    PandasExecutionEngine,
    SparkDFExecutionEngine,
)
from great_expectations.execution_engine.sqlalchemy_execution_engine import (
    SqlAlchemyExecutionEngine,
)
from great_expectations.execution_engine.util import get_approximate_percentile_disc_sql
from great_expectations.expectations.metrics.column_aggregate_metric import (
    ColumnMetricProvider,
    column_aggregate_partial,
    column_aggregate_value,
)
from great_expectations.expectations.metrics.column_aggregate_metric import sa as sa
from great_expectations.expectations.metrics.metric_provider import metric_value
from great_expectations.expectations.metrics.util import attempt_allowing_relative_error

logger = logging.getLogger(__name__)


class ColumnQuantileValues(ColumnMetricProvider):
    metric_name = "column.quantile_values"
    value_keys = ("quantiles", "allow_relative_error")

    @column_aggregate_value(engine=PandasExecutionEngine)
    def _pandas(cls, column, quantiles, **kwargs):
        """Quantile Function"""

        return column.quantile(quantiles, interpolation="nearest").tolist()

    @metric_value(engine=SqlAlchemyExecutionEngine)
    def _sqlalchemy(
        cls,
        execution_engine: "SqlAlchemyExecutionEngine",
        metric_domain_kwargs: Dict,
        metric_value_kwargs: Dict,
        metrics: Dict[Tuple, Any],
        runtime_configuration: Dict,
    ):
        (
            selectable,
            compute_domain_kwargs,
            accessor_domain_kwargs,
        ) = execution_engine.get_compute_domain(
            metric_domain_kwargs, domain_type=MetricDomainTypes.COLUMN
        )
        column_name = accessor_domain_kwargs["column"]
        column = sa.column(column_name)
        sqlalchemy_engine = execution_engine.engine
        dialect = sqlalchemy_engine.dialect
        quantiles = metric_value_kwargs["quantiles"]
        allow_relative_error = metric_value_kwargs.get("allow_relative_error", False)
        if dialect.name.lower() == "mssql":
            return _get_column_quantiles_mssql(
                column=column,
                quantiles=quantiles,
                selectable=selectable,
                sqlalchemy_engine=sqlalchemy_engine,
            )
        elif dialect.name.lower() == "bigquery":
            return _get_column_quantiles_bigquery(
                column=column,
                quantiles=quantiles,
                selectable=selectable,
                sqlalchemy_engine=sqlalchemy_engine,
            )
        elif dialect.name.lower() == "mysql":
            return _get_column_quantiles_mysql(
                column=column,
                quantiles=quantiles,
                selectable=selectable,
                sqlalchemy_engine=sqlalchemy_engine,
            )
        else:
            return _get_column_quantiles_generic_sqlalchemy(
                column=column,
                quantiles=quantiles,
                allow_relative_error=allow_relative_error,
                dialect=dialect,
                selectable=selectable,
                sqlalchemy_engine=sqlalchemy_engine,
            )

    @metric_value(engine=SparkDFExecutionEngine)
    def _spark(
        cls,
        execution_engine: "SqlAlchemyExecutionEngine",
        metric_domain_kwargs: Dict,
        metric_value_kwargs: Dict,
        metrics: Dict[Tuple, Any],
        runtime_configuration: Dict,
    ):
        (
            df,
            compute_domain_kwargs,
            accessor_domain_kwargs,
        ) = execution_engine.get_compute_domain(
            metric_domain_kwargs, domain_type=MetricDomainTypes.COLUMN
        )
        allow_relative_error = metric_value_kwargs.get("allow_relative_error", False)
        quantiles = metric_value_kwargs["quantiles"]
        column = accessor_domain_kwargs["column"]
        if allow_relative_error is False:
            allow_relative_error = 0.0
        if (
            not isinstance(allow_relative_error, float)
            or allow_relative_error < 0
            or allow_relative_error > 1
        ):
            raise ValueError(
                "SparkDFDataset requires relative error to be False or to be a float between 0 and 1."
            )
        return df.approxQuantile(column, list(quantiles), allow_relative_error)


def _get_column_quantiles_mssql(
    column, quantiles: Iterable, selectable, sqlalchemy_engine
) -> list:
    # mssql requires over(), so we add an empty over() clause
    selects: List[WithinGroup] = [
        sa.func.percentile_disc(quantile).within_group(column.asc()).over()
        for quantile in quantiles
    ]
    quantiles_query: Select = sa.select(selects).select_from(selectable)

    try:
        quantiles_results: RowProxy = sqlalchemy_engine.execute(
            quantiles_query
        ).fetchone()
        return list(quantiles_results)
    except ProgrammingError as pe:
        exception_message: str = "An SQL syntax Exception occurred."
        exception_traceback: str = traceback.format_exc()
        exception_message += (
            f'{type(pe).__name__}: "{str(pe)}".  Traceback: "{exception_traceback}".'
        )
        logger.error(exception_message)
        raise pe


def _get_column_quantiles_bigquery(
    column, quantiles: Iterable, selectable, sqlalchemy_engine
) -> list:
    # BigQuery does not support "WITHIN", so we need a special case for it
    selects: List[WithinGroup] = [
        sa.func.percentile_disc(column, quantile).over() for quantile in quantiles
    ]
    quantiles_query: Select = sa.select(selects).select_from(selectable)

    try:
        quantiles_results: RowProxy = sqlalchemy_engine.execute(
            quantiles_query
        ).fetchone()
        return list(quantiles_results)
    except ProgrammingError as pe:
        exception_message: str = "An SQL syntax Exception occurred."
        exception_traceback: str = traceback.format_exc()
        exception_message += (
            f'{type(pe).__name__}: "{str(pe)}".  Traceback: "{exception_traceback}".'
        )
        logger.error(exception_message)
        raise pe


def _get_column_quantiles_mysql(
    column, quantiles: Iterable, selectable, sqlalchemy_engine
) -> list:
    # MySQL does not support "percentile_disc", so we implement it as a compound query.
    # Please see https://stackoverflow.com/questions/19770026/calculate-percentile-value-using-mysql for reference.
    percent_rank_query: CTE = (
        sa.select(
            [
                column,
                sa.cast(
                    sa.func.percent_rank().over(order_by=column.asc()),
                    sa.dialects.mysql.DECIMAL(18, 15),
                ).label("p"),
            ]
        )
        .order_by(sa.column("p").asc())
        .select_from(selectable)
        .cte("t")
    )

    selects: List[WithinGroup] = []
    for idx, quantile in enumerate(quantiles):
        # pymysql cannot handle conversion of numpy float64 to float; convert just in case
        if np.issubdtype(type(quantile), np.float_):
            quantile = float(quantile)
        quantile_column: Label = (
            sa.func.first_value(column)
            .over(
                order_by=sa.case(
                    [
                        (
                            percent_rank_query.c.p
                            <= sa.cast(quantile, sa.dialects.mysql.DECIMAL(18, 15)),
                            percent_rank_query.c.p,
                        )
                    ],
                    else_=None,
                ).desc()
            )
            .label(f"q_{idx}")
        )
        selects.append(quantile_column)
    quantiles_query: Select = (
        sa.select(selects).distinct().order_by(percent_rank_query.c.p.desc())
    )

    try:
        quantiles_results: RowProxy = sqlalchemy_engine.execute(
            quantiles_query
        ).fetchone()
        return list(quantiles_results)
    except ProgrammingError as pe:
        exception_message: str = "An SQL syntax Exception occurred."
        exception_traceback: str = traceback.format_exc()
        exception_message += (
            f'{type(pe).__name__}: "{str(pe)}".  Traceback: "{exception_traceback}".'
        )
        logger.error(exception_message)
        raise pe


# Support for computing the quantiles column for PostGreSQL and Redshift is included in the same method as that for
# the generic sqlalchemy compatible DBMS engine, because users often use the postgresql driver to connect to Redshift
# The key functional difference is that Redshift does not support the aggregate function
# "percentile_disc", but does support the approximate percentile_disc or percentile_cont function version instead.```
def _get_column_quantiles_generic_sqlalchemy(
    column,
    quantiles: Iterable,
    allow_relative_error: bool,
    dialect,
    selectable,
    sqlalchemy_engine,
) -> list:
    selects: List[WithinGroup] = [
        sa.func.percentile_disc(quantile).within_group(column.asc())
        for quantile in quantiles
    ]
    quantiles_query: Select = sa.select(selects).select_from(selectable)

    try:
        quantiles_results: RowProxy = sqlalchemy_engine.execute(
            quantiles_query
        ).fetchone()
        return list(quantiles_results)
    except ProgrammingError:
        # ProgrammingError: (psycopg2.errors.SyntaxError) Aggregate function "percentile_disc" is not supported;
        # use approximate percentile_disc or percentile_cont instead.
        if attempt_allowing_relative_error(dialect):
            # Redshift does not have a percentile_disc method, but does support an approximate version.
            sql_approx: str = get_approximate_percentile_disc_sql(
                selects=selects, sql_engine_dialect=dialect
            )
            selects_approx: List[TextClause] = [sa.text(sql_approx)]
            quantiles_query_approx: Select = sa.select(selects_approx).select_from(
                selectable
            )
            if allow_relative_error:
                try:
                    quantiles_results: RowProxy = sqlalchemy_engine.execute(
                        quantiles_query_approx
                    ).fetchone()
                    return list(quantiles_results)
                except ProgrammingError as pe:
                    exception_message: str = "An SQL syntax Exception occurred."
                    exception_traceback: str = traceback.format_exc()
                    exception_message += f'{type(pe).__name__}: "{str(pe)}".  Traceback: "{exception_traceback}".'
                    logger.error(exception_message)
                    raise pe
            else:
                raise ValueError(
                    f'The SQL engine dialect "{str(dialect)}" does not support computing quantiles '
                    "without approximation error; set allow_relative_error to True to allow approximate quantiles."
                )
        else:
            raise ValueError(
                f'The SQL engine dialect "{str(dialect)}" does not support computing quantiles with '
                "approximation error; set allow_relative_error to False to disable approximate quantiles."
            )
