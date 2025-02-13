#!/usr/bin/env python3
import csv
import logging
from pathlib import Path
from typing import Any, Final

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.postgres.operators.postgres import PostgresOperator
from common import (
    DATAPUNT_ENVIRONMENT,
    DATASTORE_TYPE,
    SHARED_DIR,
    MessageOperator,
    default_args,
    slack_webhook_token,
)
from common.path import mk_dir
from contact_point.callbacks import get_contact_point_on_failure_callback
from http_fetch_operator import HttpFetchOperator
from postgres_permissions_operator import PostgresPermissionsOperator
from postgres_table_copy_operator import PostgresTableCopyOperator
from sqlalchemy_create_object_operator import SqlAlchemyCreateObjectOperator

logger = logging.getLogger(__name__)

DAG_ID: Final = "anpr"
TMP_DIR: Final = Path(SHARED_DIR) / DAG_ID
TABLE_ID: Final = "anpr_taxi"
HTTP_CONN_ID: Final = (
    "taxi_waarnemingen_conn_id"
    if DATASTORE_TYPE != "acceptance"
    else "taxi_waarnemingen_acc_conn_id"
)
ENDPOINT: Final = "/v0/milieuzone/passage/export-taxi/"


args = default_args.copy()

SQL_RENAME_TEMP_TABLE: Final = """
    DROP TABLE IF EXISTS {{ params.base_table }}_old CASCADE;
    ALTER TABLE IF EXISTS {{ params.base_table }}
        RENAME TO {{ params.base_table }}_old;
    ALTER TABLE {{ params.base_table }}_temp
        RENAME TO {{ params.base_table }};
"""


def import_csv_data(**_: Any) -> None:
    """Insert rows for CSV file into DB table."""
    sql_header = f"INSERT INTO {TABLE_ID}_temp (datum, aantal_taxi_passages) VALUES "
    with open(TMP_DIR / "taxi_passages.csv") as csvfile:
        reader = csv.DictReader(csvfile)
        items = []
        for row in reader:
            items.append(
                "('{date}', {aantal_taxi_passages})".format(
                    date=row["datum"], aantal_taxi_passages=row["aantal_taxi_passages"]
                )
            )
        if len(items):
            hook = PostgresHook()
            sql = "{header} {items};".format(header=sql_header, items=",".join(items))
            try:
                hook.run(sql)
            except Exception as e:
                raise Exception(f"Failed to create data: {e}"[:150])
            logger.debug("Created %d records.", len(items))


with DAG(
    DAG_ID,
    default_args=args,
    description="aantal geidentificeerde taxikentekenplaten per dag",
    on_failure_callback=get_contact_point_on_failure_callback(dataset_id=DAG_ID),
) as dag:

    slack_at_start = MessageOperator(
        task_id="slack_at_start",
        http_conn_id="slack",
        webhook_token=slack_webhook_token,
        message=f"Starting {DAG_ID} ({DATAPUNT_ENVIRONMENT})",
        username="admin",
    )

    mk_tmp_dir = mk_dir(TMP_DIR, clean_if_exists=True)

    # Drop the identity on the `id` column, otherwise SqlAlchemyCreateObjectOperator
    # gets seriously confused; as in: its finds something it didn't create and errors out.
    drop_identity_from_table = PostgresOperator(
        task_id="drop_identity_from_table",
        sql="""
            ALTER TABLE IF EXISTS {{ params.table_id }}
                ALTER COLUMN id DROP IDENTITY IF EXISTS;
        """,
        params={"table_id": TABLE_ID},
    )

    sqlalchemy_create_objects_from_schema = SqlAlchemyCreateObjectOperator(
        task_id="sqlalchemy_create_objects_from_schema",
        data_schema_name=DAG_ID,
        ind_extra_index=False,
    )

    add_identity_to_table = PostgresOperator(
        task_id="add_identity_to_table",
        sql="""
            ALTER TABLE {{ params.table_id }}
                ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
        """,
        params={"table_id": TABLE_ID},
    )

    download_data = HttpFetchOperator(
        task_id="download",
        endpoint=ENDPOINT,
        http_conn_id=HTTP_CONN_ID,
        tmp_file=TMP_DIR / "taxi_passages.csv",
        output_type="text",
    )

    create_temp_table = PostgresTableCopyOperator(
        task_id="create_temp_table",
        source_table_name=TABLE_ID,
        target_table_name=f"{TABLE_ID}_temp",
        # Only copy table definitions. Don't do anything else.
        truncate_target=False,
        copy_data=False,
        drop_source=False,
    )

    import_data = PythonOperator(
        task_id="import_data",
        python_callable=import_csv_data,
        dag=dag,
    )

    rename_temp_table = PostgresOperator(
        task_id="rename_temp_tables",
        sql=SQL_RENAME_TEMP_TABLE,
        params={"base_table": TABLE_ID},
    )

    # Grant database permissions
    grant_db_permissions = PostgresPermissionsOperator(task_id="grants", dag_name=DAG_ID)

    (
        slack_at_start
        >> mk_tmp_dir
        >> drop_identity_from_table
        >> sqlalchemy_create_objects_from_schema
        >> add_identity_to_table
        >> download_data
        >> create_temp_table
        >> import_data
        >> rename_temp_table
        >> grant_db_permissions
    )
