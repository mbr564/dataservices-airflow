import datetime
import operator
import os
import re
import subprocess
from pathlib import Path
from typing import Final

import dateutil.parser
import shapefile
from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.postgres.operators.postgres import PostgresOperator
from common import SHARED_DIR, default_args
from common.path import mk_dir
from contact_point.callbacks import get_contact_point_on_failure_callback
from postgres_check_operator import COUNT_CHECK, PostgresMultiCheckOperator
from postgres_permissions_operator import PostgresPermissionsOperator
from postgres_rename_operator import PostgresTableRenameOperator
from postgres_table_copy_operator import PostgresTableCopyOperator
from shapely.geometry import Polygon
from sqlalchemy_create_object_operator import SqlAlchemyCreateObjectOperator
from swift_hook import SwiftHook

DAG_ID: Final = "parkeervakken"
POSTGRES_CONN_ID: Final = "parkeervakken_postgres"

# CONFIG = Variable.get(DAG_ID, deserialize_json=True)

TABLES: Final = {
    "BASE": f"{DAG_ID}_{DAG_ID}",
    "BASE_TEMP": f"{DAG_ID}_{DAG_ID}_temp",
    "REGIMES": f"{DAG_ID}_{DAG_ID}_regimes",
    "REGIMES_TEMP": f"{DAG_ID}_{DAG_ID}_regimes_temp",
}
WEEK_DAYS: Final = ["ma", "di", "wo", "do", "vr", "za", "zo"]
TMP_DIR: Final = Path(SHARED_DIR) / DAG_ID
TMP_TABLE_POSTFIX: Final = "_temp"
TABLE_ID: Final = f"{DAG_ID}_{DAG_ID}"
E_TYPES: Final = {
    "E1": "Parkeerverbod",
    "E2": "Verbod stil te staan",
    "E3": "Verbod fietsen en bromfietsen te plaatsen",
    "E4": "Parkeergelegenheid",
    "E5": "Taxistandplaats",
    "E6": "Gehandicaptenparkeerplaats",
    "E6a": "Gehandicaptenparkeerplaats algemeen",
    "E6b": "Gehandicaptenparkeerplaats op kenteken",
    "E7": "Gelegenheid bestemd voor het onmiddellijk laden en lossen van goederen",
    "E8": "Parkeergelegenheid alleen bestemd voor de voertuigcategorie of groep voertuigen die op het bord is aangegeven",
    "E9": "Parkeergelegenheid alleen bestemd voor vergunninghouders",
    "E10": (
        "Parkeerschijf-zone met verplicht gebruik van parkeerschijf, tevens parkeerverbod indien er langer wordt "
        "geparkeerd dan de parkeerduur die op het bord is aangegeven"
    ),
    "E11": "Einde parkeerschijf-zone met verplicht gebruik van parkeerschijf",
    "E12": "Parkeergelegenheid ten behoeve van overstappers op het openbaar vervoer",
    "E13": "Parkeergelegenheid ten behoeve van carpoolers",
}


def import_data(shp_file, ids):
    """
    Import Shape File into database.
    """
    base_regime_sql = (
        "("
        "'{parent_id}',"
        "'{soort}',"
        "'{e_type}',"
        "'{e_type_description}',"
        "'{bord}',"
        "'{begin_tijd}',"
        "'{eind_tijd}',"
        "E'{opmerking}',"
        "{dagen},"
        "{kenteken},"
        "{begin_datum},"
        "{eind_datum},"
        "'{aantal}'"
        ")"
    )

    parkeervakken_sql = []
    regimes_sql = []
    duplicates = []
    print(f"Processing: {shp_file}")
    with shapefile.Reader(shp_file, encodingErrors="ignore") as shape:
        for row in shape:
            if int(row.record.PARKEER_ID) in ids:
                # Exclude dupes
                duplicates.append(row.record.PARKEER_ID)
                continue
            ids.append(int(row.record.PARKEER_ID))

            regimes = create_regimes(row=row)
            soort = "FISCAAL"
            if len(regimes) == 1:
                soort = regimes[0]["soort"]

            parkeervakken_sql.append(create_parkeervaak(row=row, soort=soort))
            regimes_sql += [
                base_regime_sql.format(
                    parent_id=row.record.PARKEER_ID,
                    soort=mode["soort"],
                    e_type=mode["e_type"],
                    e_type_description=E_TYPES.get(mode["e_type"], ""),
                    bord=mode["bord"],
                    begin_tijd=mode["begin_tijd"].strftime("%H:%M"),
                    eind_tijd=mode["eind_tijd"].strftime("%H:%M"),
                    opmerking=mode["opmerking"],
                    dagen="'{" + ",".join([f'"{day}"' for day in mode["dagen"]]) + "}'",
                    kenteken=f"'{mode['kenteken']}'" if mode["kenteken"] else "NULL",
                    begin_datum=f"'{mode['begin_datum']}'" if mode["begin_datum"] else "NULL",
                    eind_datum=f"'{mode['eind_datum']}'" if mode["eind_datum"] else "NULL",
                    aantal=row.record.AANTAL,
                )
                for mode in regimes
            ]

    create_parkeervakken_sql = (
        "INSERT INTO {} ("
        "id, buurtcode, straatnaam, soort, type, aantal, geometry, e_type"
        ") VALUES {};"
    ).format(TABLES["BASE_TEMP"], ",".join(parkeervakken_sql))
    create_regimes_sql = (
        "INSERT INTO {} ("
        "parent_id, soort, e_type, e_type_description, bord, begin_tijd, eind_tijd, opmerking, dagen, "
        "kenteken, begin_datum, eind_datum, aantal"
        ") VALUES {};"
    ).format(TABLES["REGIMES_TEMP"], ",".join(regimes_sql))

    hook = PostgresHook()
    if len(parkeervakken_sql):
        try:
            hook.run(create_parkeervakken_sql)
        except Exception as e:
            raise Exception(f"Failed to create parkeervakken: {str(e)[0:150]}")
    if len(regimes_sql):
        try:
            hook.run(create_regimes_sql)
        except Exception as e:
            raise Exception(f"Failed to create regimes: {str(e)[0:150]}")

    print(f"Created: {len(parkeervakken_sql)} parkeervakken and {len(regimes_sql)} regimes")
    return duplicates


def download_latest_export_file(swift_conn_id, container, name_regex, *args, **kwargs):
    """
    Find latest export filename
    """
    hook = SwiftHook(swift_conn_id=swift_conn_id)
    latest = None

    name_reg = re.compile(name_regex)
    for x in hook.list_container(container=container):
        if x["content_type"] != "application/zip":
            # Skip non-zip files.
            continue

        if name_reg.match(x["name"]) is None:
            # Search for latest file matching regex
            continue

        if latest is None:
            latest = x

        if dateutil.parser.parse(x["last_modified"]) > dateutil.parser.parse(
            latest["last_modified"]
        ):
            latest = x

    if latest is None:
        raise AirflowException("Failed to fetch objectstore listing.")
    zip_path = os.path.join(TMP_DIR, latest["name"])
    hook.download(container=container, object_id=latest["name"], output_path=zip_path)

    try:
        subprocess.run(
            ["unzip", "-o", zip_path, "-d", TMP_DIR.as_posix()], stderr=subprocess.PIPE, check=True
        )
    except subprocess.CalledProcessError as e:
        raise AirflowException(f"Failed to extract zip: {e.stderr}")

    return latest["name"]


def run_imports(source: Path) -> None:
    """
    Run imports for all files in zip that match date.
    """
    ids = []
    for shp_file in source.glob("*.shp"):
        duplicates = import_data(str(shp_file), ids)

        if len(duplicates):
            print("Duplicates found: {}".format(", ".join(duplicates)))


args = default_args.copy()

with DAG(
    DAG_ID,
    default_args=args,
    description="Parkeervakken",
    on_failure_callback=get_contact_point_on_failure_callback(dataset_id=DAG_ID),
) as dag:
    mk_tmp_dir = mk_dir(TMP_DIR)

    download_and_extract_zip = PythonOperator(
        task_id="download_and_extract_zip",
        python_callable=download_latest_export_file,
        op_kwargs={
            "swift_conn_id": "objectstore_parkeervakken",
            "container": "tijdregimes",
            "name_regex": r"^nivo_\d+\.zip",
        },
    )

    download_and_extract_nietfiscaal_zip = PythonOperator(
        task_id="download_and_extract_nietfiscaal_zip",
        python_callable=download_latest_export_file,
        op_kwargs={
            "swift_conn_id": "objectstore_parkeervakken",
            "container": "Parkeervakken",
            "name_regex": r"^\d+\_nietfiscaal\.zip",
        },
    )

    drop_identity_from_table = [
        PostgresOperator(
            task_id="drop_identity_from_table",
            sql="""
                    ALTER TABLE IF EXISTS {{ params.table_id }}
                        ALTER COLUMN id DROP IDENTITY IF EXISTS;
                """,
            params={"table_id": table_id},
        )
        for table_id in (TABLES["BASE"], TABLES["REGIMES"])
    ]

    sqlalchemy_create_objects_from_schema = SqlAlchemyCreateObjectOperator(
        task_id="sqlalchemy_create_objects_from_schema",
        data_schema_name=DAG_ID,
        ind_extra_index=True,
    )

    add_identity_to_table = [
        PostgresOperator(
            task_id="add_identity_to_table",
            sql="""
                ALTER TABLE {{ params.table_id }}
                    ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
            """,
            params={"table_id": table_id},
        )
        for table_id in (TABLES["BASE"], TABLES["REGIMES"])
    ]

    join = DummyOperator(task_id="join")

    create_temp_table = [
        PostgresTableCopyOperator(
            task_id="create_temp_table",
            source_table_name=table_id,
            target_table_name=f"{table_id}{TMP_TABLE_POSTFIX}",
            drop_target_if_unequal=True,
            truncate_target=False,
            copy_data=False,
            drop_source=False,
        )
        for table_id in (TABLES["BASE"], TABLES["REGIMES"])
    ]

    run_import_task = PythonOperator(
        task_id="run_import_task",
        python_callable=run_imports,
        op_kwargs={"source": TMP_DIR},
        dag=dag,
    )

    count_check = PostgresMultiCheckOperator(
        task_id="count_check",
        checks=[
            COUNT_CHECK.make_check(
                check_id="non_zero_check",
                pass_value=10,
                params={"table_name": f"{DAG_ID}_{DAG_ID}_temp"},
                result_checker=operator.ge,
            )
        ],
    )

    # Though identity columns are much more convenient than the old style serial columns, the
    # sequences associated with them are not renamed automatically when the table with the
    # identity columns is renamed. Hence, when renaming a table `tmp_fubar`, with identity column
    # `id`, to `fubar`, the associated sequence will still be named `tmp_fubar_id_seq`.
    #
    # This is not a problem, unless we suffer from certain OCD tendencies. Hence, we leave the
    # slight naming inconsistency as-is. Should you want to address it, don't rename the
    # sequence. Simply drop cascade it instead. After all, the identity column is only required
    # for the duration of the import; we don't need it afterwards. And when you do want to drop
    # cascade it, reuse the existing `drop_identity_to_tables` task by wrapping it in a
    # function and call it just before renaming the tables.
    rename_temp_table = [
        PostgresTableRenameOperator(
            task_id=f"rename_tables_for_{TABLE_ID}",
            new_table_name=table_id,
            old_table_name=f"{table_id}{TMP_TABLE_POSTFIX}",
            cascade=True,
        )
        for table_id in (TABLES["BASE"], TABLES["REGIMES"])
    ]

    # Grant database permissions
    grant_db_permissions = PostgresPermissionsOperator(task_id="grants", dag_name=DAG_ID)

(
    mk_tmp_dir
    >> download_and_extract_zip
    >> download_and_extract_nietfiscaal_zip
    >> drop_identity_from_table
    >> sqlalchemy_create_objects_from_schema
    >> add_identity_to_table
    >> create_temp_table
    >> join
    >> run_import_task
    >> count_check
    >> rename_temp_table
    >> grant_db_permissions
)


# Internals
def create_parkeervaak(row, soort=None):
    geometry = "''"
    if row.shape.shapeTypeName == "POLYGON":
        geometry = f"ST_GeometryFromText('{str(Polygon(row.shape.points))}', 28992)"
    sql = (
        "("
        "'{parkeer_id}',"
        "'{buurtcode}',"
        "E'{straatnaam}',"
        "'{soort}',"
        "'{type}',"
        "'{aantal}',"
        "{geometry},"
        "'{e_type}'"
        ")"
    ).format(
        parkeer_id=row.record.PARKEER_ID,
        buurtcode=row.record.BUURTCODE,
        straatnaam=row.record.STRAATNAAM.replace("'", "\\'"),
        soort=row.record.SOORT or soort,
        type=row.record.TYPE or "",
        aantal=row.record.AANTAL,
        geometry=geometry,
        e_type=row.record.E_TYPE or "",
    )
    return sql


def create_regimes(row):

    output = []

    days = days_from_row(row)

    base_data = {
        "parent_id": row.record.PARKEER_ID or "",
        "soort": "FISCAAL",
        "e_type": "",
        "bord": "",
        "begin_tijd": datetime.time(0, 0),
        "eind_tijd": datetime.time(23, 59),
        "opmerking": row.record.OPMERKING or "",
        "dagen": WEEK_DAYS,
        "kenteken": None,
        "begin_datum": None,
        "eind_datum": None,
    }

    mode_start = datetime.time(0, 0)
    mode_end = datetime.time(23, 59)

    modes = get_modes(row)
    if len(modes) == 0:
        # No time modes, but could have full override.
        x = base_data.copy()
        x.update(
            {
                "soort": row.record.SOORT or "FISCAAL",
                "bord": row.record.BORD or "",
                "e_type": row.record.E_TYPE or "",
                "kenteken": row.record.KENTEKEN,
            }
        )
        return [x]

    for mode in modes:
        if mode.get("begin_tijd", datetime.time(0, 0)) > mode_start:
            # Time bound. Start of the day mode.
            sod_mode = base_data.copy()
            sod_mode["dagen"] = days
            sod_mode["begin_tijd"] = mode_start
            sod_mode["eind_tijd"] = remove_a_minute(mode["begin_tijd"])
            output.append(sod_mode)

        mode_data = base_data.copy()
        mode_data.update(mode)
        mode_data["dagen"] = days
        output.append(mode_data)
        mode_start = add_a_minute(mode["eind_tijd"])

    if mode.get("eind_tijd", datetime.time(23, 59)) < mode_end:
        # Time bound. End of the day mode.
        eod_mode = base_data.copy()
        eod_mode["dagen"] = days
        eod_mode["begin_tijd"] = add_a_minute(mode["eind_tijd"])
        output.append(eod_mode)
    return output


def add_a_minute(time):
    return (
        datetime.datetime.combine(datetime.date.today(), time) + datetime.timedelta(minutes=1)
    ).time()


def remove_a_minute(time):
    return (
        datetime.datetime.combine(datetime.date.today(), time) - datetime.timedelta(minutes=1)
    ).time()


def get_modes(row):
    modes = []

    base = {
        "soort": row.record.SOORT or "FISCAAL",
        "bord": row.record.BORD or "",
        "e_type": row.record.E_TYPE or "",
        "kenteken": row.record.KENTEKEN,
    }
    if any(
        [
            row.record.TVM_BEGINT,
            row.record.TVM_EINDT,
            row.record.TVM_BEGIND,
            row.record.TVM_EINDD,
            row.record.TVM_OPMERK,
        ]
    ):
        # TVM
        tvm_mode = base.copy()
        tvm_mode.update(
            {
                "soort": row.record.E_TYPE or "FISCAAL",
                "begin_datum": row.record.TVM_BEGIND or "",
                "eind_datum": row.record.TVM_EINDD or "",
                "opmerking": (row.record.TVM_OPMERK or "").replace("'", "\\'"),
                "begin_tijd": parse_time(row.record.TVM_BEGINT, datetime.time(0, 0)),
                "eind_tijd": parse_time(row.record.TVM_EINDT, datetime.time(23, 59)),
            }
        )
        modes.append(tvm_mode)

    if any([row.record.BEGINTIJD1, row.record.EINDTIJD1]):
        begin_tijd = parse_time(row.record.BEGINTIJD1, datetime.time(0, 0))
        eind_tijd = parse_time(row.record.EINDTIJD1, datetime.time(23, 59))
        if begin_tijd < eind_tijd:
            x = base.copy()
            x.update({"begin_tijd": begin_tijd, "eind_tijd": eind_tijd})
            modes.append(x)
        else:
            # Mode: 20:00, 06:00
            x = base.copy()
            x.update({"begin_tijd": datetime.time(0, 0), "eind_tijd": eind_tijd})
            y = base.copy()
            y.update({"begin_tijd": begin_tijd, "eind_tijd": datetime.time(23, 59)})
            modes.append(x)
            modes.append(y)
    if any([row.record.BEGINTIJD2, row.record.EINDTIJD2]):
        x = base.copy()
        x.update(
            {
                "begin_tijd": parse_time(row.record.BEGINTIJD2, datetime.time(0, 0)),
                "eind_tijd": parse_time(row.record.EINDTIJD2, datetime.time(23, 59)),
            }
        )
        modes.append(x)
    return modes


def days_from_row(row):
    """
    Parse week days from row.
    """

    if row.record.MA_VR:
        # Monday to Friday
        days = WEEK_DAYS[:4]
    elif row.record.MA_ZA:
        # Monday to Saturday
        days = WEEK_DAYS[:5]

    elif not any([getattr(row.record, day.upper()) for day in WEEK_DAYS]):
        # All days apply
        days = WEEK_DAYS
    else:
        # One day permit
        days = [day for day in WEEK_DAYS if getattr(row.record, day.upper()) is not None]

    return days


def parse_time(value, default=None):
    """
    Parse time or return default
    """
    if value is not None:
        if value == "24:00":
            value = "23:59"
        if value.startswith("va "):
            value = value[3:]

        try:
            parsed = dateutil.parser.parse(value)
        except dateutil.parser._parser.ParserError:
            pass
        else:
            return parsed.time()
    return default
