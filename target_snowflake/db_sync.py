import json
import sys
import snowflake.connector
import collections
import inflection
import re
import itertools
import time

from singer import get_logger

from target_snowflake.s3_upload_client import S3UploadClient
from target_snowflake.snowflake_upload_client import SnowflakeUploadClient

from snowflake.connector.errors import ProgrammingError
from snowflake.connector.encryption_util import SnowflakeEncryptionUtil
from snowflake.connector.remote_storage_util import SnowflakeFileEncryptionMaterial
from snowflake.ingest import SimpleIngestManager, \
    StagedFile
from snowflake.ingest.utils.uris import DEFAULT_SCHEME
from requests import HTTPError
from cryptography.hazmat.primitives.serialization import load_pem_private_key, \
    Encoding, \
    PrivateFormat, \
    NoEncryption
from cryptography.hazmat.backends import default_backend
from distutils.util import strtobool

class TooManyRecordsException(Exception):
    """Exception to raise when query returns more records than max_records"""
    pass


def validate_config(config):
    errors = []
    s3_required_config_keys = [
        'account',
        'dbname',
        'user',
        'password',
        'warehouse',
        's3_bucket',
        'stage',
        'file_format'
    ]

    snowflake_required_config_keys = [
        'account',
        'dbname',
        'user',
        'password',
        'warehouse',
        'file_format'
    ]

    required_config_keys = []

    # Use external stages if both s3_bucket and stage defined
    if config.get('s3_bucket', None) and config.get('stage', None):
        required_config_keys = s3_required_config_keys
    # Use table stage if none s3_bucket and stage defined
    elif not config.get('s3_bucket', None) and not config.get('stage', None):
        required_config_keys = snowflake_required_config_keys
    else:
        errors.append("Only one of 's3_bucket' or 'stage' keys defined in config. "
                      "Use both of them if you want to use an external stage when loading data into snowflake "
                      "or don't use any of them if you want ot use table stages.")

    # Check if mandatory keys exist
    for k in required_config_keys:
        if not config.get(k, None):
            errors.append("Required key is missing from config: [{}]".format(k))

    # Check target schema config
    config_default_target_schema = config.get('default_target_schema', None)
    config_schema_mapping = config.get('schema_mapping', None)
    if not config_default_target_schema and not config_schema_mapping:
        errors.append("Neither 'default_target_schema' (string) nor 'schema_mapping' (object) keys set in config.")

    # Check client-side encryption config
    config_cse_key = config.get('client_side_encryption_master_key', None)

    return errors


def column_type(schema_property):
    property_type = schema_property['type']
    property_format = schema_property['format'] if 'format' in schema_property else None
    column_type = 'text'
    if 'object' in property_type or 'array' in property_type:
        column_type = 'variant'

    # Every date-time JSON value is currently mapped to TIMESTAMP_NTZ
    #
    # TODO: Detect if timezone postfix exists in the JSON and find if TIMESTAMP_TZ or
    # TIMSTAMP_NTZ is the better column type
    elif property_format == 'date-time':
        column_type = 'timestamp_ntz'
    elif property_format == 'time':
        column_type = 'time'
    elif property_format == 'binary':
        column_type = 'binary'
    elif 'number' in property_type:
        column_type = 'float'
    elif 'integer' in property_type and 'string' in property_type:
        column_type = 'text'
    elif 'integer' in property_type:
        column_type = 'number'
    elif 'boolean' in property_type:
        column_type = 'boolean'

    return column_type


def column_trans(schema_property):
    property_type = schema_property['type']
    column_trans = ''
    if 'object' in property_type or 'array' in property_type:
        column_trans = 'parse_json'
    elif schema_property.get('format') == 'binary':
        column_trans = 'to_binary'

    return column_trans


def safe_column_name(name):
    return '"{}"'.format(name).upper()


def column_clause(name, schema_property):
    return '{} {}'.format(safe_column_name(name), column_type(schema_property))


def flatten_key(k, parent_key, sep):
    full_key = parent_key + [k]
    inflected_key = full_key.copy()
    reducer_index = 0
    while len(sep.join(inflected_key)) >= 255 and reducer_index < len(inflected_key):
        reduced_key = re.sub(r'[a-z]', '', inflection.camelize(inflected_key[reducer_index]))
        inflected_key[reducer_index] = \
            (reduced_key if len(reduced_key) > 1 else inflected_key[reducer_index][0:3]).lower()
        reducer_index += 1

    return sep.join(inflected_key)


def flatten_schema(d, parent_key=[], sep='__', level=0, max_level=0):
    items = []

    if 'properties' not in d:
        return {}

    for k, v in d['properties'].items():
        new_key = flatten_key(k, parent_key, sep)
        if 'type' in v.keys():
            if 'object' in v['type'] and 'properties' in v and level < max_level:
                items.extend(flatten_schema(v, parent_key + [k], sep=sep, level=level + 1, max_level=max_level).items())
            else:
                items.append((new_key, v))
        else:
            if len(v.values()) > 0:
                if list(v.values())[0][0]['type'] == 'string':
                    list(v.values())[0][0]['type'] = ['null', 'string']
                    items.append((new_key, list(v.values())[0][0]))
                elif list(v.values())[0][0]['type'] == 'array':
                    list(v.values())[0][0]['type'] = ['null', 'array']
                    items.append((new_key, list(v.values())[0][0]))
                elif list(v.values())[0][0]['type'] == 'object':
                    list(v.values())[0][0]['type'] = ['null', 'object']
                    items.append((new_key, list(v.values())[0][0]))

    key_func = lambda item: item[0]
    sorted_items = sorted(items, key=key_func)
    for k, g in itertools.groupby(sorted_items, key=key_func):
        if len(list(g)) > 1:
            raise ValueError('Duplicate column name produced in schema: {}'.format(k))

    return dict(sorted_items)


def _should_json_dump_value(key, value, flatten_schema=None):
    if isinstance(value, (dict, list)):
        return True

    if flatten_schema and key in flatten_schema and 'type' in flatten_schema[key] and set(
            flatten_schema[key]['type']) == {'null', 'object', 'array'}:
        return True

    return False


# pylint: disable-msg=too-many-arguments
def flatten_record(d, flatten_schema=None, parent_key=[], sep='__', level=0, max_level=0):
    items = []
    for k, v in d.items():
        new_key = flatten_key(k, parent_key, sep)
        if isinstance(v, collections.MutableMapping) and level < max_level:
            items.extend(flatten_record(v, flatten_schema, parent_key + [k], sep=sep, level=level + 1,
                                        max_level=max_level).items())
        else:
            items.append((new_key, json.dumps(v) if _should_json_dump_value(k, v, flatten_schema) else v))

    return dict(items)


def primary_column_names(stream_schema_message):
    return [safe_column_name(p) for p in stream_schema_message['key_properties']]


def stream_name_to_dict(stream_name, separator='-'):
    catalog_name = None
    schema_name = None
    table_name = stream_name

    # Schema and table name can be derived from stream if it's in <schema_nama>-<table_name> format
    s = stream_name.split(separator)
    if len(s) == 2:
        schema_name = s[0]
        table_name = s[1]
    if len(s) > 2:
        catalog_name = s[0]
        schema_name = s[1]
        table_name = '_'.join(s[2:])

    return {
        'catalog_name': catalog_name,
        'schema_name': schema_name,
        'table_name': table_name
    }


def create_query_tag(query_tag_pattern: str, schema: str = None, table: str = None) -> str:
    """
    Generate a string to tag executed queries in Snowflake.
    Replaces tokens `schema` and `table` with the appropriate values.

    Example with tokens:
        'Loading data into {schema}.{table}'

    Args:
        query_tag_pattern:
        schema: optional value to replace {schema} token in query_tag_pattern
        table: optional value to replace {table} token in query_tag_pattern

    Returns:
        String if query_tag_patter defined otherwise None
    """
    if not query_tag_pattern:
        return None

    query_tag = query_tag_pattern

    # replace tokens
    for k, v in {
        '{schema}': schema or 'unknown-schema',
        '{table}': table or 'unknown-table'
    }.items():
        if k in query_tag:
            query_tag = query_tag.replace(k, v)

    return query_tag


# pylint: disable=too-many-public-methods,too-many-instance-attributes
class DbSync:
    def __init__(self, connection_config, stream_schema_message=None, table_cache=None):
        """
            connection_config:      Snowflake connection details

            stream_schema_message:  An instance of the DbSync class is typically used to load
                                    data only from a certain singer tap stream.

                                    The stream_schema_message holds the destination schema
                                    name and the JSON schema that will be used to
                                    validate every RECORDS messages that comes from the stream.
                                    Schema validation happening before creating CSV and before
                                    uploading data into Snowflake.

                                    If stream_schema_message is not defined that we can use
                                    the DbSync instance as a generic purpose connection to
                                    Snowflake and can run individual queries. For example
                                    collecting catalog informations from Snowflake for caching
                                    purposes.
        """
        self.connection_config = connection_config
        self.stream_schema_message = stream_schema_message
        self.table_cache = table_cache

        # logger to be used across the class's methods
        self.logger = get_logger('target_snowflake')

        # Validate connection configuration
        config_errors = validate_config(connection_config)

        # Exit if config has errors
        if len(config_errors) > 0:
            self.logger.error("Invalid configuration:\n   * {}".format('\n   * '.join(config_errors)))
            sys.exit(1)

        if self.connection_config.get('stage', None):
            stage = stream_name_to_dict(self.connection_config['stage'], separator='.')
            if not stage['schema_name']:
                self.logger.error(
                    "The named external stage object in config has to use the <schema>.<stage_name> format.")
                sys.exit(1)

        self.schema_name = None
        self.grantees = None

        # Init stream schema
        if self.stream_schema_message is not None:
            #  Define target schema name.
            #  --------------------------
            #  Target schema name can be defined in multiple ways:
            #
            #   1: 'default_target_schema' key  : Target schema is the same for every incoming stream if
            #                                     not specified explicitly for a given stream in
            #                                     the `schema_mapping` object
            #   2: 'schema_mapping' key         : Target schema defined explicitly for a given stream.
            #                                     Example config.json:
            #                                           "schema_mapping": {
            #                                               "my_tap_stream_id": {
            #                                                   "target_schema": "my_snowflake_schema",
            #                                                   "target_schema_select_permissions": [ "role_with_select_privs" ]
            #                                               }
            #                                           }
            config_default_target_schema = self.connection_config.get('default_target_schema', '').strip()
            config_schema_mapping = self.connection_config.get('schema_mapping', {})

            stream_name = stream_schema_message['stream']
            stream_schema_name = stream_name_to_dict(stream_name)['schema_name']
            if config_schema_mapping and stream_schema_name in config_schema_mapping:
                self.schema_name = config_schema_mapping[stream_schema_name].get('target_schema')
            elif config_default_target_schema:
                self.schema_name = config_default_target_schema

            if not self.schema_name:
                raise Exception(
                    "Target schema name not defined in config. "
                    "Neither 'default_target_schema' (string) nor 'schema_mapping' (object) defines "
                    "target schema for {} stream.".format(stream_name))

            #  Define grantees
            #  ---------------
            #  Grantees can be defined in multiple ways:
            #
            #   1: 'default_target_schema_select_permissions' key  : USAGE and SELECT privileges will be granted on every table to a given role
            #                                                       for every incoming stream if not specified explicitly
            #                                                       in the `schema_mapping` object
            #   2: 'target_schema_select_permissions' key          : Roles to grant USAGE and SELECT privileges defined explicitly
            #                                                       for a given stream.
            #                                                       Example config.json:
            #                                                           "schema_mapping": {
            #                                                               "my_tap_stream_id": {
            #                                                                   "target_schema": "my_snowflake_schema",
            #                                                                   "target_schema_select_permissions": [ "role_with_select_privs" ]
            #                                                               }
            #                                                           }
            self.grantees = self.connection_config.get('default_target_schema_select_permissions')
            if config_schema_mapping and stream_schema_name in config_schema_mapping:
                self.grantees = config_schema_mapping[stream_schema_name].get('target_schema_select_permissions',
                                                                              self.grantees)

            self.data_flattening_max_level = self.connection_config.get('data_flattening_max_level', 0)
            self.flatten_schema = flatten_schema(stream_schema_message['schema'],
                                                 max_level=self.data_flattening_max_level)

        if connection_config.get('s3_bucket', None):
            self.uploadClient = S3UploadClient(connection_config)
        # Use table stage
        else:
            self.uploadClient = SnowflakeUploadClient(connection_config, self)

    def open_connection(self):
        stream = None
        if self.stream_schema_message:
            stream = self.stream_schema_message['stream']

        return snowflake.connector.connect(
            user=self.connection_config['user'],
            password=self.connection_config['password'],
            account=self.connection_config['account'],
            database=self.connection_config['dbname'],
            warehouse=self.connection_config['warehouse'],
            role=self.connection_config.get('role', None),
            autocommit=True,
            session_parameters={
                # Quoted identifiers should be case sensitive
                'QUOTED_IDENTIFIERS_IGNORE_CASE': 'FALSE',
                'QUERY_TAG': create_query_tag(self.connection_config.get('query_tag'),
                                              schema=self.schema_name,
                                              table=self.table_name(stream, False, True))
            }
        )

    def query(self, query, params=None, max_records=0):
        result = []
        with self.open_connection() as connection:
            with connection.cursor(snowflake.connector.DictCursor) as cur:
                queries = []

                # Run every query in one transaction if query is a list of SQL
                if type(query) is list:
                    queries.append("START TRANSACTION")
                    queries.extend(query)
                else:
                    queries = [query]

                for q in queries:
                    self.logger.debug("Running query: {}".format(q))
                    cur.execute(q, params)

                    # Raise exception if returned rows greater than max allowed records
                    if 0 < max_records < cur.rowcount:
                        raise TooManyRecordsException(
                            f"Query returned too many records. This query can return max {max_records} records")

                    result = cur.fetchall()

        return result

    def table_name(self, stream_name, is_temporary, without_schema=False):
        if not stream_name:
            return None

        stream_dict = stream_name_to_dict(stream_name)
        table_name = stream_dict['table_name']
        sf_table_name = table_name.replace('.', '_').replace('-', '_').lower()

        if is_temporary:
            sf_table_name = '{}_temp'.format(sf_table_name)

        if without_schema:
            return f'"{sf_table_name.upper()}"'

        return f'{self.schema_name}."{sf_table_name.upper()}"'

    def record_primary_key_string(self, record):
        if len(self.stream_schema_message['key_properties']) == 0:
            return None
        flatten = flatten_record(record, self.flatten_schema, max_level=self.data_flattening_max_level)
        try:
            key_props = [str(flatten[p]) for p in self.stream_schema_message['key_properties']]
        except Exception as exc:
            self.logger.error(
                "Cannot find {} primary key(s) in record: {}".format(self.stream_schema_message['key_properties'],
                                                                     flatten))
            raise exc
        return ','.join(key_props)

    def record_to_csv_line(self, record):
        flatten = flatten_record(record, self.flatten_schema, max_level=self.data_flattening_max_level)

        return ','.join(
            [
                json.dumps(flatten[name], ensure_ascii=False) if name in flatten and (
                            flatten[name] == 0 or flatten[name]) else ''
                for name in self.flatten_schema
            ]
        )

    def _generate_s3_key_prefix(self,stream, use_snowpipe):
        """ If loading via snowpipe then the table_name is added to the s3 prefix """
        s3_key_prefix = self.connection_config.get('s3_key_prefix', '').replace('/','')
        schema_table_name = self.table_name(stream, None, False).lower().replace('"','').replace('.','__')
        path_arr = ["{}/".format(s3_key_prefix),]
        if use_snowpipe:
            path_arr.append("{}/".format(schema_table_name))

        return "".join(path_arr)

    def put_to_stage(self, file, stream, count, temp_dir=None, load_via_snowpipe=False):
        self.logger.info("Uploading {} rows to stage".format(count))
        s3_key_prefix = self._generate_s3_key_prefix(stream, load_via_snowpipe)
        return self.uploadClient.upload_file(file, stream, temp_dir=temp_dir, s3_key_prefix=s3_key_prefix)

    def delete_from_stage(self, stream, s3_key):
        self.logger.info("Deleting {} from stage".format(s3_key))
        self.uploadClient.delete_object(stream, s3_key)

    def get_stage_name(self, stream):
        stage = self.connection_config.get('stage', None)
        if stage:
            return stage

        table_name = self.table_name(stream, False, without_schema=True)
        return f"{self.schema_name}.%{table_name}"

    def load_csv(self, s3_key, count, size_bytes):
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        self.logger.info("Loading {} rows into '{}'".format(count, self.table_name(stream, False)))

        # Get list if columns with types
        columns_with_trans = [
            {
                "name": safe_column_name(name),
                "trans": column_trans(schema)
            }
            for (name, schema) in self.flatten_schema.items()
        ]

        with self.open_connection() as connection:
            with connection.cursor(snowflake.connector.DictCursor) as cur:
                inserts = 0
                updates = 0

                # Insert or Update with MERGE command if primary key defined
                if len(self.stream_schema_message['key_properties']) > 0:
                    merge_sql = """MERGE INTO {} t
                        USING (
                            SELECT {}
                              FROM '@{}/{}'
                              (FILE_FORMAT => '{}')) s
                        ON {}
                        WHEN MATCHED THEN
                            UPDATE SET {}
                        WHEN NOT MATCHED THEN
                            INSERT ({})
                            VALUES ({})
                    """.format(
                        self.table_name(stream, False),
                        ', '.join(["{}(${}) {}".format(c['trans'], i + 1, c['name']) for i, c in
                                   enumerate(columns_with_trans)]),
                        self.get_stage_name(stream),
                        s3_key,
                        self.connection_config['file_format'],
                        self.primary_key_merge_condition(),
                        ', '.join(['{}=s.{}'.format(c['name'], c['name']) for c in columns_with_trans]),
                        ', '.join([c['name'] for c in columns_with_trans]),
                        ', '.join(['s.{}'.format(c['name']) for c in columns_with_trans])
                    )
                    self.logger.debug("Running query: {}".format(merge_sql))
                    cur.execute(merge_sql)

                    # Get number of inserted and updated records - MERGE does insert and update
                    results = cur.fetchall()
                    if len(results) > 0:
                        inserts = results[0].get('number of rows inserted', 0)
                        updates = results[0].get('number of rows updated', 0)

                # Insert only with COPY command if no primary key
                else:
                    copy_sql = """COPY INTO {} ({}) FROM '@{}/{}'
                        FILE_FORMAT = (format_name='{}')
                    """.format(
                        self.table_name(stream, False),
                        ', '.join([c['name'] for c in columns_with_trans]),
                        self.get_stage_name(stream),
                        s3_key,
                        self.connection_config['file_format'],
                    )
                    self.logger.debug("Running query: {}".format(copy_sql))
                    cur.execute(copy_sql)

                    # Get number of inserted records - COPY does insert only
                    results = cur.fetchall()
                    if len(results) > 0:
                        inserts = results[0].get('rows_loaded', 0)

                self.logger.info('Loading into {}: {}'.format(
                    self.table_name(stream, False),
                    json.dumps({'inserts': inserts, 'updates': updates, 'size_bytes': size_bytes})))

    def load_via_snowpipe(self, s3_key, stream):
        """ Performs data transfer from the stage to snowflake using snowpipe. """

        def _generate_pipe_name(dbname, schema_table_name):
            stripped_db_name = dbname.replace('"','')
            stripped_table_name = schema_table_name.replace('"','')
            return f"{stripped_db_name}.{stripped_table_name}_s3_pipe"

        def _generate_pipe_args(pipe_name, schema_table_name, columns_with_trans):
            pipe_args = dict(
                pipe_name= pipe_name,
                db_name = self.connection_config['dbname'],
                obj_name = schema_table_name,
                stage = self.connection_config['stage'],
                file_format = self.connection_config['file_format'],
                cols = ', '.join([c['name'] for c in columns_with_trans]),
                )
            return pipe_args

        def _load_private_key():
            key_path = getattr(self.connection_config, "private_key_path", "/rsa_key.p8")
            password = getattr(self.connection_config, "private_key_password", None)
            with open(key_path, 'rb') as pem_in:
                private_key_obj = load_pem_private_key(pem_in.read(),password=password,backend=default_backend())

            private_key_text = private_key_obj.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode('utf-8')
            return private_key_text

        def _increment_value(exponentially=False):
                previous = 0
                current = 1
                while True:
                    yield 2**(current + previous) if exponentially  \
                        else current + previous + 30
                    current = current + previous
                    previous = current - previous

        self.logger.info("Loading data using Snowpipe.")
        # Get list if columns with types and transformation
        columns_with_trans = [
            {
                "name": safe_column_name(name),
                "trans": column_trans(schema)
            }
            for (name, schema) in self.flatten_schema.items()
        ]
        schema_table_name = self.table_name(stream, False)
        db_name = self.connection_config['dbname']

        pipe_name = _generate_pipe_name(db_name, schema_table_name)
        pipe_args = _generate_pipe_args(pipe_name, schema_table_name, columns_with_trans)

        create_pipe_sql = """create pipe {pipe_name} as
                            copy into {db_name}.{obj_name} ({cols})
                            from @{db_name}.{stage}
                            file_format = (format_name = {db_name}.{file_format} );""".format(**pipe_args)
        drop_pipe_sql = f"drop pipe if exists {pipe_name};"

        # Create snowpipe
        try:
            self.logger.debug("Creating snowpipe - %s.", pipe_name)

            # primary key in records found, raise warning
            if len(self.stream_schema_message['key_properties']) > 0:
                self.logger.warning("Primary key %s found in the data stream. Snowpipe can not be used to "
                                    "consolidate records based upon keys. It can just copy data. "
                                    "Please refer the docs for further details",
                                    self.stream_schema_message['key_properties'])

            # primary key not present in the records, perform copy
            self.query(create_pipe_sql)
        except ProgrammingError as error:
            self.logger.error("An error was encountered while creating the snowpipe, %s", error)

        #  Private key encription required to perform snowpipe data transfer
        private_key_text = _load_private_key()

        ingest_manager = SimpleIngestManager(account=self.connection_config['account'].split('.')[0],
                                        host=self.connection_config['account']+'.snowflakecomputing.com',
                                        user=self.connection_config['user'],
                                        pipe=pipe_name,
                                        scheme='https',
                                        port=443,
                                        private_key=private_key_text)

        # List of files, but wrapped into a class
        staged_file_list = [StagedFile(s3_key, None)]

        #ingest files using snowpipe
        retries = self.connection_config.get('max_retry', 5)
        wait_time = _increment_value(exponentially=True)
        while True:
            try:
                self.logger.debug("Starting to ingest file via snowpipe, retries left %s", retries)
                resp = ingest_manager.ingest_files(staged_file_list)
                self.logger.info("Snowpipe has recived the files and will now start loading: %s",
                                resp['responseCode'])
                break
            except HTTPError as e:
                # HTTP error, wait and retry, exit if still fails
                self.logger.error(e)
                time.sleep(next(wait_time))
                retries -= 1
                if not retries:
                    self.logger.critcal("Max retry limit reached, Failed to load data using snowpipe")
                    sys.exit(1)

        # Needs to wait for a while to perform transfer, delete pipe after transfer
        wait_time = _increment_value()
        while True:
            history_resp = ingest_manager.get_history()

            if len(history_resp['files']) > 0:
                self.logger.info('''Ingest Report for snowpipe : %s
                                    STATUS: %s
                                    rowsInserted(rowsParsed): %s(%s)''',
                                    history_resp['pipe'],
                                    history_resp['completeResult'],
                                    history_resp['files'][0]['rowsInserted'],
                                    history_resp['files'][0]['rowsParsed'])
                self.query(drop_pipe_sql)
                break
            else:
                self.logger.debug('waiting for snowpipe to transfer data...')
                time.sleep(next(wait_time))

    def primary_key_merge_condition(self):
        stream_schema_message = self.stream_schema_message
        names = primary_column_names(stream_schema_message)
        return ' AND '.join(['s.{} = t.{}'.format(c, c) for c in names])

    def column_names(self):
        return [safe_column_name(name) for name in self.flatten_schema]

    def create_table_query(self, is_temporary=False):
        stream_schema_message = self.stream_schema_message
        columns = [
            column_clause(
                name,
                schema
            )
            for (name, schema) in self.flatten_schema.items()
        ]

        primary_key = ["PRIMARY KEY ({})".format(', '.join(primary_column_names(stream_schema_message)))] \
            if len(stream_schema_message['key_properties']) else []

        return 'CREATE {}TABLE IF NOT EXISTS {} ({}) {}'.format(
            'TEMP ' if is_temporary else '',
            self.table_name(stream_schema_message['stream'], is_temporary),
            ', '.join(columns + primary_key),
            'data_retention_time_in_days = 0 ' if is_temporary else 'data_retention_time_in_days = 1 '
        )

    def grant_usage_on_schema(self, schema_name, grantee):
        query = "GRANT USAGE ON SCHEMA {} TO ROLE {}".format(schema_name, grantee)
        self.logger.info("Granting USAGE privilege on '{}' schema to '{}'... {}".format(schema_name, grantee, query))
        self.query(query)

    def grant_select_on_all_tables_in_schema(self, schema_name, grantee):
        query = "GRANT SELECT ON ALL TABLES IN SCHEMA {} TO ROLE {}".format(schema_name, grantee)
        self.logger.info(
            "Granting SELECT ON ALL TABLES privilege on '{}' schema to '{}'... {}".format(schema_name, grantee, query))
        self.query(query)

    @classmethod
    def grant_privilege(self, schema, grantees, grant_method):
        if isinstance(grantees, list):
            for grantee in grantees:
                grant_method(schema, grantee)
        elif isinstance(grantees, str):
            grant_method(schema, grantees)

    def delete_rows(self, stream):
        table = self.table_name(stream, False)
        query = "DELETE FROM {} WHERE _sdc_deleted_at IS NOT NULL".format(table)
        self.logger.info("Deleting rows from '{}' table... {}".format(table, query))
        self.logger.info("DELETE {}".format(len(self.query(query))))

    def create_schema_if_not_exists(self):
        schema_name = self.schema_name
        schema_rows = 0

        # table_cache is an optional pre-collected list of available objects in snowflake
        if self.table_cache:
            schema_rows = list(filter(lambda x: x['SCHEMA_NAME'] == schema_name.upper(), self.table_cache))
        # Query realtime if not pre-collected
        else:
            schema_rows = self.query(f"SHOW SCHEMAS LIKE '{schema_name.upper()}'")

        if len(schema_rows) == 0:
            query = "CREATE SCHEMA IF NOT EXISTS {}".format(schema_name)
            self.logger.info("Schema '{}' does not exist. Creating... {}".format(schema_name, query))
            self.query(query)

            self.grant_privilege(schema_name, self.grantees, self.grant_usage_on_schema)

            # Refresh columns cache if required
            if self.table_cache:
                self.table_cache = self.get_table_columns(table_schemas=[self.schema_name])

    def get_tables(self, table_schemas=[]):
        tables = []
        if table_schemas:
            for schema in table_schemas:
                queries = []

                # Get column data types by SHOW COLUMNS
                show_tables = f"SHOW TERSE TABLES IN SCHEMA {self.connection_config['dbname']}.{schema}"

                # Convert output of SHOW TABLES to table
                select = f"""
                    SELECT "schema_name" AS schema_name
                          ,"name"        AS table_name
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
                """
                queries.extend([show_tables, select])

                # Run everything in one transaction
                try:
                    tables = self.query(queries, max_records=9999)

                # Catch exception when schema not exists and SHOW TABLES throws a ProgrammingError
                # Regexp to extract snowflake error code and message from the exception message
                # Do nothing if schema not exists
                except snowflake.connector.errors.ProgrammingError as exc:
                    if re.match('002043 \(02000\):.*\n.*does not exist.*', str(sys.exc_info()[1])):
                        pass
                    else:
                        raise exc
        else:
            raise Exception("Cannot get table columns. List of table schemas empty")

        return tables

    def get_table_columns(self, table_schemas=[]):
        table_columns = []
        if table_schemas:
            for schema in table_schemas:
                queries = []

                # Get column data types by SHOW COLUMNS
                show_columns = f"SHOW COLUMNS IN SCHEMA {self.connection_config['dbname']}.{schema}"

                # Convert output of SHOW COLUMNS to table and insert results into the cache COLUMNS table
                select = f"""
                    SELECT "schema_name" AS schema_name
                          ,"table_name"  AS table_name
                          ,"column_name" AS column_name
                          -- ----------------------------------------------------------------------------------------
                          -- Character and numeric columns display their generic data type rather than their defined
                          -- data type (i.e. TEXT for all character types, FIXED for all fixed-point numeric types,
                          -- and REAL for all floating-point numeric types).
                          --
                          -- Further info at https://docs.snowflake.net/manuals/sql-reference/sql/show-columns.html
                          -- ----------------------------------------------------------------------------------------
                          ,CASE PARSE_JSON("data_type"):type::varchar
                             WHEN 'FIXED' THEN 'NUMBER'
                             WHEN 'REAL'  THEN 'FLOAT'
                             ELSE PARSE_JSON("data_type"):type::varchar
                           END data_type
                      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
                """
                queries.extend([show_columns, select])

                # Run everything in one transaction
                try:
                    columns = self.query(queries, max_records=9999)
                    table_columns.extend(columns)

                # Catch exception when schema not exists and SHOW COLUMNS throws a ProgrammingError
                # Regexp to extract snowflake error code and message from the exception message
                # Do nothing if schema not exists
                except snowflake.connector.errors.ProgrammingError as exc:
                    if re.match('002003 \(02000\):.*\n.*does not exist or not authorized.*', str(sys.exc_info()[1])):
                        pass
                    else:
                        raise exc

        else:
            raise Exception("Cannot get table columns. List of table schemas empty")

        return table_columns

    def refresh_table_cache(self):
        self.table_cache = self.get_table_columns([self.schema_name])

    def update_columns(self):
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        table_name = self.table_name(stream, False, True)
        all_table_columns = []

        if self.table_cache:
            all_table_columns = self.table_cache
        else:
            all_table_columns = self.get_table_columns(table_schemas=[self.schema_name])

        # Find the specific table
        columns = list(filter(lambda x: x['SCHEMA_NAME'] == self.schema_name.upper() and
                                        f'"{x["TABLE_NAME"].upper()}"' == table_name,
                              all_table_columns))

        columns_dict = {column['COLUMN_NAME'].upper(): column for column in columns}

        columns_to_add = [
            column_clause(
                name,
                properties_schema
            )
            for (name, properties_schema) in self.flatten_schema.items()
            if name.upper() not in columns_dict
        ]

        for column in columns_to_add:
            self.add_column(column, stream)

        columns_to_replace = [
            (safe_column_name(name), column_clause(
                name,
                properties_schema
            ))
            for (name, properties_schema) in self.flatten_schema.items()
            if name.upper() in columns_dict and
               columns_dict[name.upper()]['DATA_TYPE'].upper() != column_type(properties_schema).upper() and

               # Don't alter table if TIMESTAMP_NTZ detected as the new required column type
               #
               # Target-snowflake maps every data-time JSON types to TIMESTAMP_NTZ but sometimes
               # a TIMESTAMP_TZ column is already available in the target table (i.e. created by fastsync initial load)
               # We need to exclude this conversion otherwise we loose the data that is already populated
               # in the column
               #
               # TODO: Support both TIMESTAMP_TZ and TIMESTAMP_NTZ in target-snowflake
               # when extracting data-time values from JSON
               # (Check the column_type function for further details)
               column_type(properties_schema).upper() != 'TIMESTAMP_NTZ'
        ]

        for (column_name, column) in columns_to_replace:
            # self.drop_column(column_name, stream)
            self.version_column(column_name, stream)
            self.add_column(column, stream)

        # Refresh table cache if required
        if self.table_cache and (len(columns_to_add) > 0 or len(columns_to_replace)):
            self.table_cache = self.get_table_columns(table_schemas=[self.schema_name])

    def drop_column(self, column_name, stream):
        drop_column = "ALTER TABLE {} DROP COLUMN {}".format(self.table_name(stream, False), column_name)
        self.logger.info('Dropping column: {}'.format(drop_column))
        self.query(drop_column)

    def version_column(self, column_name, stream):
        version_column = "ALTER TABLE {} RENAME COLUMN {} TO \"{}_{}\"".format(self.table_name(stream, False),
                                                                               column_name,
                                                                               column_name.replace("\"", ""),
                                                                               time.strftime("%Y%m%d_%H%M"))
        self.logger.info('Dropping column: {}'.format(version_column))
        self.query(version_column)

    def add_column(self, column, stream):
        add_column = "ALTER TABLE {} ADD COLUMN {}".format(self.table_name(stream, False), column)
        self.logger.info('Adding column: {}'.format(add_column))
        self.query(add_column)

    def sync_table(self):
        stream_schema_message = self.stream_schema_message
        stream = stream_schema_message['stream']
        table_name = self.table_name(stream, False, True)
        table_name_with_schema = self.table_name(stream, False)

        if self.table_cache:
            found_tables = list(filter(lambda x: x['SCHEMA_NAME'] == self.schema_name.upper() and
                                                 f'"{x["TABLE_NAME"].upper()}"' == table_name,
                                       self.table_cache))
        else:
            found_tables = [table for table in (self.get_tables([self.schema_name.upper()]))
                            if f'"{table["TABLE_NAME"].upper()}"' == table_name]

        if len(found_tables) == 0:
            query = self.create_table_query()
            self.logger.info("Table '{}' does not exist. Creating...".format(table_name_with_schema))
            self.query(query)

            self.grant_privilege(self.schema_name, self.grantees, self.grant_select_on_all_tables_in_schema)

            # Refresh columns cache if required
            if self.table_cache:
                self.table_cache = self.get_table_columns(table_schemas=[self.schema_name])
        else:
            self.logger.info("Table '{}' exists".format(table_name_with_schema))
            self.update_columns()
