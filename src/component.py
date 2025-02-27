"""
Template Component main class.

"""

import logging
import os
import time
from collections import OrderedDict

import duckdb
import polars
from duckdb.duckdb import DuckDBPyConnection
from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import SupportedDataTypes, BaseType, ColumnDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement, ValidationResult, MessageType

from configuration import Configuration

DUCK_DB_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "duckdb")


class Component(ComponentBase):
    def __init__(self):
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)
        self._connection = self.init_connection()
        self.source_uri = self.build_source_uri()

    def run(self):
        """
        Main execution code
        """

        table_name = self.get_table_name()
        query = self.get_query()

        if self.params.destination.parquet_output:
            out_file = self.create_out_file_definition(f"{table_name}.parquet")
            q = f" COPY ({query}) TO '{out_file.full_path}'; "
            logging.debug(f"Running query: {q}; ")
            start = time.time()
            self._connection.execute(q)
            logging.debug(f"Query finished successfully in {time.time() - start} seconds")
            self.write_manifest(out_file)
        else:
            table_meta = self._connection.execute(f"""DESCRIBE {query};""").fetchall()
            schema = OrderedDict(
                {c[0]: ColumnDefinition(data_types=BaseType(dtype=self.convert_base_types(c[1]))) for c in table_meta}
            )

            out_table = self.create_out_table_definition(
                f"{table_name}.csv",
                schema=schema,
                primary_key=self.params.destination.primary_key,
                incremental=self.params.destination.incremental,
                has_header=True,
            )

            try:
                q = f"COPY ({query}) TO '{out_table.full_path}' (HEADER, DELIMITER ',', FORCE_QUOTE *)"
                logging.debug(f"Running query: {q}; ")
                start = time.time()
                self._connection.execute(q)
                logging.debug(f"Query finished successfully in {time.time() - start} seconds")
            except duckdb.duckdb.ConversionException as e:
                raise UserException(f"Error during query execution: {e}")

            self.write_manifest(out_table)
        self._connection.close()

    def init_connection(self) -> DuckDBPyConnection:
        """
        Returns connection to temporary DuckDB database
        """
        os.makedirs(DUCK_DB_DIR, exist_ok=True)
        # TODO: On GCP consider changin tmp to /opt/tmp
        config = dict(
            temp_directory=DUCK_DB_DIR,
            extension_directory=os.path.join(DUCK_DB_DIR, "extensions"),
            threads=self.params.threads,
            max_memory=f"{self.params.max_memory}MB",
        )
        conn = duckdb.connect(config=config)

        conn.execute(self.build_connection_query())

        if not self.params.destination.preserve_insertion_order:
            conn.execute("SET preserve_insertion_order = false;").fetchall()

        return conn

    def build_connection_query(self):
        match self.params.provider:
            case "abs":
                abs_conn_str = (
                    f"AccountName={self.params.abs_account_name};"
                    f"SharedAccessSignature={self.params.abs_sas_token}"
                )
                query = f"""
                        CREATE SECRET (
                            TYPE AZURE,
                            CONNECTION_STRING '{abs_conn_str}');
                        SET azure_transport_option_type = 'curl';
                        """
            case "s3":
                query = f"""
                        CREATE SECRET (
                            TYPE S3,
                            REGION '{self.params.aws_region}',
                            KEY_ID '{self.params.aws_key_id}',
                            SECRET '{self.params.aws_key_secret}'
                            );
                       """
            case "gcs":
                query = f"""
                        INSTALL httpfs;
                        CREATE SECRET (
                            TYPE GCS,
                            KEY_ID '{self.params.gcp_hmac_id}',
                            SECRET '{self.params.gcp_hmac_secret}'
                            );
                       """
            case _:
                raise UserException(f"Unknown provider: {self.params.provider}")

        return query

    def build_source_uri(self):
        match self.params.provider:
            case "abs":
                source_uri = f"az://{self.params.source.container_name}/{self.params.source.blob_name}"
            case "s3":
                source_uri = f"s3://{self.params.source.container_name}/{self.params.source.blob_name}"
            case "gcs":
                source_uri = f"gs://{self.params.source.container_name}/{self.params.source.blob_name}"
            case _:
                raise UserException(f"Unknown provider: {self.params.provider}")

        return source_uri

    def get_table_name(self):
        if not (self.params.destination.table_name or self.params.destination.file_name):
            table_name = f"{self.params.source.container_name}-{self.params.source.blob_name}"
        else:
            table_name = self.params.destination.table_name or self.params.destination.file_name
        return table_name

    def get_query(self):
        if self.params.data_selection.mode == "custom_query":
            query = self.params.data_selection.query.lower().replace(
                "from in_table ", f"FROM delta_scan('{self.source_uri}')"
            )
        elif self.params.data_selection.mode == "select_columns":
            query = f"""
            SELECT {", ".join(self.params.data_selection.columns)}
            FROM delta_scan('{self.source_uri}')"""
        elif self.params.data_selection.mode == "all_data":
            query = f"SELECT * FROM delta_scan('{self.source_uri}')"
        else:
            raise UserException("Invalid data selection mode")

        return query

    def convert_base_types(self, dtype: str) -> SupportedDataTypes:
        if dtype in [
            "TINYINT",
            "SMALLINT",
            "INTEGER",
            "BIGINT",
            "HUGEINT",
            "UTINYINT",
            "USMALLINT",
            "UINTEGER",
            "UBIGINT",
            "UHUGEINT",
        ]:
            return SupportedDataTypes.INTEGER
        elif dtype in ["REAL", "DECIMAL"]:
            return SupportedDataTypes.NUMERIC
        elif dtype == "DOUBLE":
            return SupportedDataTypes.FLOAT
        elif dtype == "BOOLEAN":
            return SupportedDataTypes.BOOLEAN
        elif dtype in ["TIMESTAMP", "TIMESTAMP WITH TIME ZONE"]:
            return SupportedDataTypes.TIMESTAMP
        elif dtype == "DATE":
            return SupportedDataTypes.DATE
        else:
            return SupportedDataTypes.STRING

    @sync_action("list_columns")
    def list_columns(self):
        out = self._connection.execute(f"""
        DESCRIBE
        FROM delta_scan('{self.source_uri}');
        """).fetchall()

        column_names = [SelectElement(c[0], f"{c[0]} ({c[1]})") for c in out]

        return column_names

    @sync_action("table_preview")
    def table_preview(self):
        out = self._connection.execute(f"""
                SELECT *
                FROM delta_scan('{self.source_uri}')
                LIMIT 10;
                """).pl()

        formatted_output = self.to_markdown(out)

        return ValidationResult(formatted_output, MessageType.SUCCESS)

    def to_markdown(self, out):
        polars.Config.set_tbl_formatting("ASCII_MARKDOWN")
        polars.Config.set_tbl_hide_dataframe_shape(True)
        formatted_output = str(out)
        return formatted_output

    @sync_action("query_preview")
    def query_preview(self):
        query = self.params.data_selection.query.lower().replace(
            "from in_table", f"FROM delta_scan('{self.source_uri}')"
        )

        if "limit" not in query.lower():
            query = f"{query} LIMIT 10;"

        out = self._connection.execute(query).pl()

        formatted_output = self.to_markdown(out)

        return ValidationResult(formatted_output, MessageType.SUCCESS)


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
