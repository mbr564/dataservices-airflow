import operator
from pathlib import Path
from typing import Final

from airflow import DAG
from airflow.models import Variable
from airflow.operators.bash import BashOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.operators.postgres import PostgresOperator
from common import (
    DATAPUNT_ENVIRONMENT,
    SHARED_DIR,
    MessageOperator,
    default_args,
    pg_params,
    quote_string,
    slack_webhook_token,
)
from common.path import mk_dir
from contact_point.callbacks import get_contact_point_on_failure_callback
from importscripts.convert_bedrijveninvesteringszones_data import convert_biz_data
from postgres_check_operator import COUNT_CHECK, GEO_CHECK, PostgresMultiCheckOperator
from postgres_permissions_operator import PostgresPermissionsOperator
from postgres_rename_operator import PostgresTableRenameOperator
from postgres_table_copy_operator import PostgresTableCopyOperator
from sql.bedrijveninvesteringszones import UPDATE_TABLE
from sqlalchemy_create_object_operator import SqlAlchemyCreateObjectOperator
from swift_operator import SwiftOperator

dag_id = "bedrijveninvesteringszones"
variables = Variable.get(dag_id, deserialize_json=True)
tmp_dir = f"{SHARED_DIR}/{dag_id}"
files_to_download = variables["files_to_download"]
total_checks = []
count_checks = []
geo_checks = []
check_name = {}

TABLE_ID: Final = f"{dag_id}_{dag_id}"
TMP_TABLE_POSTFIX: Final = "_new"

with DAG(
    dag_id,
    description="tariefen, locaties en overige contextuele gegevens over bedrijveninvesteringszones.",
    default_args=default_args,
    user_defined_filters={"quote": quote_string},
    template_searchpath=["/"],
    on_failure_callback=get_contact_point_on_failure_callback(dataset_id=dag_id),
) as dag:

    slack_at_start = MessageOperator(
        task_id="slack_at_start",
        http_conn_id="slack",
        webhook_token=slack_webhook_token,
        message=f"Starting {dag_id} ({DATAPUNT_ENVIRONMENT})",
        username="admin",
    )

    mkdir = mk_dir(Path(tmp_dir))

    download_data = [
        SwiftOperator(
            task_id=f"download_{file}",
            # if conn is ommitted, it defaults to Objecstore Various Small Datasets
            # swift_conn_id="SWIFT_DEFAULT",
            container="bedrijveninvesteringszones",
            object_id=str(file),
            output_path=f"{tmp_dir}/{file}",
        )
        for files in files_to_download.values()
        for file in files
    ]

    # Dummy operator acts as an interface between parallel tasks to another parallel tasks (i.e.
    # lists or tuples) with different number of lanes (without this intermediar, Airflow will
    # give an error)
    Interface = DummyOperator(task_id="interface")

    SHP_to_SQL = [
        BashOperator(
            task_id="SHP_to_SQL",
            bash_command="ogr2ogr -f 'PGDump' " f"{tmp_dir}/{dag_id}.sql {tmp_dir}/{file}",
        )
        for files in files_to_download.values()
        for file in files
        if "shp" in file
    ]

    SQL_convert_UTF8 = BashOperator(
        task_id="convert_to_UTF8",
        bash_command=f"iconv -f iso-8859-1 -t utf-8  {tmp_dir}/{dag_id}.sql > "
        f"{tmp_dir}/{dag_id}.utf8.sql",
    )

    SQL_update_data = [
        PythonOperator(
            task_id="update_SQL_data",
            python_callable=convert_biz_data,
            op_args=[
                f"{dag_id}_{dag_id}_new",
                f"{tmp_dir}/{dag_id}.utf8.sql",
                f"{tmp_dir}/{file}",
                f"{tmp_dir}/{dag_id}_updated_data_insert.sql",
            ],
        )
        for files in files_to_download.values()
        for file in files
        if "xlsx" in file
    ]

    sqlalchemy_create_objects_from_schema = SqlAlchemyCreateObjectOperator(
        task_id="sqlalchemy_create_objects_from_schema",
        data_schema_name=dag_id,
        ind_extra_index=True,
    )

    create_temp_table = PostgresTableCopyOperator(
        task_id="create_temp_table",
        source_table_name=TABLE_ID,
        target_table_name=f"{TABLE_ID}{TMP_TABLE_POSTFIX}",
        drop_target_if_unequal=True,
        truncate_target=True,
        copy_data=False,
        drop_source=False,
    )

    import_data = BashOperator(
        task_id="import_data",
        bash_command=f"psql {pg_params()} < {tmp_dir}/{dag_id}_updated_data_insert.sql",
    )

    update_table = PostgresOperator(
        task_id="update_target_table",
        sql=UPDATE_TABLE,
        params=dict(tablename=f"{dag_id}_{dag_id}_new"),
    )

    count_checks.append(
        COUNT_CHECK.make_check(
            check_id="count_check",
            pass_value=50,
            params=dict(table_name=f"{dag_id}_{dag_id}_new "),
            result_checker=operator.ge,
        )
    )

    geo_checks.append(
        GEO_CHECK.make_check(
            check_id="geo_check",
            params=dict(
                table_name=f"{dag_id}_{dag_id}_new",
                geotype=["POLYGON"],
            ),
            pass_value=1,
        )
    )

    total_checks = count_checks + geo_checks

    multi_checks = PostgresMultiCheckOperator(task_id="multi_check", checks=total_checks)

    rename_temp_table = PostgresTableRenameOperator(
        task_id=f"rename_tables_for_{TABLE_ID}",
        new_table_name=TABLE_ID,
        old_table_name=f"{TABLE_ID}{TMP_TABLE_POSTFIX}",
        cascade=True,
    )
    grant_db_permissions = PostgresPermissionsOperator(task_id="grants", dag_name=dag_id)


slack_at_start >> mkdir >> download_data

for data in download_data:
    data >> Interface

(
    Interface
    >> SHP_to_SQL
    >> SQL_convert_UTF8
    >> SQL_update_data
    >> sqlalchemy_create_objects_from_schema
    >> create_temp_table
    >> import_data
    >> update_table
    >> multi_checks
    >> rename_temp_table
    >> grant_db_permissions
)

dag.doc_md = """
    #### DAG summary
    This DAG contains data about business investment zones
    #### Mission Critical
    Classified as 2 (beschikbaarheid [range: 1,2,3])
    #### On Failure Actions
    Fix issues and rerun dag on working days
    #### Point of Contact
    Inform the businessowner at [businessowner]@amsterdam.nl
    https://data.amsterdam.nl/datasets/bl6Wf85K8CfnwA/bedrijfsinvesteringszones-biz/
    #### Business Use Case / process / origin
    Na
    #### Prerequisites/Dependencies/Resourcing
    https://api.data.amsterdam.nl/v1/docs/datasets/bedrijveninvesteringszones.html
    https://api.data.amsterdam.nl/v1/docs/wfs-datasets/bedrijveninvesteringszones.html
    Example geosearch:
    https://api.data.amsterdam.nl/geosearch?datasets=bedrijveninvesteringszones/bedrijveninvesteringszones&x=106434&y=488995&radius=10
"""
