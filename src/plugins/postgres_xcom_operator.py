from airflow.hooks.postgres_hook import PostgresHook
from airflow.operators.postgres_operator import PostgresOperator


class PostgresXcomOperator(PostgresOperator):
    """Regular PostgresOperator does not return a value,
    so cannot do Xcom
    """

    def execute(self, context):
        self.log.info("Executing: %s", self.sql)
        self.hook = PostgresHook(postgres_conn_id=self.postgres_conn_id, schema=self.database)
        return self.hook.get_first(self.sql, parameters=self.parameters)
