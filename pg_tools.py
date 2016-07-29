import sqlalchemy
import collections
import os
import sys
import time
import subprocess
import luigi
import luigi.target
from luigi import configuration
import logging


import pandas.io.sql
class PgSQLDatabase(pandas.io.sql.SQLDatabase):
    """
    This class is modified from util.py in https://github.com/dssg/drain/
    """
    import tempfile
    def to_sql(self, frame, name, env, if_exists='fail', index=True,
               index_label=None, schema=None, chunksize=None, dtype=None, 
               pk=None, prefixes=None, raise_on_error=True):
        """
        Write records stored in a DataFrame to a SQL database.

        Parameters
        ----------
        frame : DataFrame
        name : string
            Name of SQL table
        if_exists : {'fail', 'replace', 'append'}, default 'fail'
            - fail: If table exists, do nothing.
            - replace: If table exists, drop it, recreate it, and insert data.
            - append: If table exists, insert data. Create if does not exist.
        index : boolean, default True
            Write DataFrame index as a column
        index_label : string or sequence, default None
            Column label for index column(s). If None is given (default) and
            `index` is True, then the index names are used.
            A sequence should be given if the DataFrame uses MultiIndex.
        schema : string, default None
            Name of SQL schema in database to write to (if database flavor
            supports this). If specified, this overwrites the default
            schema of the SQLDatabase object.
        chunksize : int, default None
            If not None, then rows will be written in batches of this size at a
            time.  If None, all rows will be written at once.
        dtype : dict of column name to SQL type, default None
            Optional specifying the datatype for columns. The SQL type should
            be a SQLAlchemy type.
        pk: name of column(s) to set as primary keys
        """
        table = pandas.io.sql.SQLTable(name, self, frame=frame, index=index,
                                       if_exists=if_exists,
                                       index_label=index_label,
                                       schema=schema, dtype=dtype)
        existed = table.exists()
        table.create()
        replaced = existed and if_exists=='replace'

        table_name=name
        if schema is not None:
            table_name = schema + '.' + table_name

        if pk is not None and ( (not existed) or replaced):
            if isinstance(pk, str):
                pks = pk
            else:
                pks = ", ".join(pk)
            sql = ("ALTER TABLE {table_name} "
                   "ADD PRIMARY KEY ({pks})").format(table_name=table_name,
                                                     pks=pks)
            self.execute(sql)


        from subprocess import Popen, PIPE, STDOUT

        columns = (frame.index.names + list(frame.columns) 
                   if index else frame.columns)
        columns = str.join(",", map(lambda c: '"' + c + '"', columns))

        sql = ("COPY {table_name} ({columns}) FROM STDIN WITH "
               "(FORMAT CSV, HEADER TRUE)").format(table_name=table_name,
                                                   columns=columns)
        p = Popen(['psql', '-c', sql], stdout=PIPE, stdin=PIPE, stderr=STDOUT,
                  env=env)
        frame.to_csv(p.stdin, index=index)

        psql_out = p.communicate()[0]
        logging.info(psql_out.decode()),

        r = p.wait()
        if raise_on_error and (r > 0):
            sys.exit(r)

        return r

    def read_table(self, name, schema=None):
        table_name=name
        if schema is not None:
            table_name = schema + '.' + table_name

        return self.read_query('select * from %s' % table_name)

    def read_sql(self, query, env, raise_on_error=True, **kwargs):
        from subprocess import Popen, PIPE, STDOUT

        sql = "COPY (%s) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)" % query
        p = Popen(['psql', '-c', sql], stdout=PIPE, stdin=PIPE, stderr=STDOUT,
                  env=env)
        df = pd.read_csv(p.stdout, **kwargs)

        psql_out = p.communicate()
        logging.info(psql_out[0].decode(),)

        r = p.wait()
        if raise_on_error and (r > 0):
            sys.exit(r)

        return df


def create_engine(dbitems=None, path_to_default_profile='../default_profile'):
    """
    Creates a self.engine to a postgres database

    :param dict dbitems: dictionary of database access items
    :param str path_to_default_profile: path to the default database config
    :returns: (engine to database, database items)
    :rtype: (sqlalchemy engine, dict)

    """
    # Read database parameters from default_profile
    if not dbitems:
        # Assumes default_profile to be of the format:
        # export PGUSER=some_user
        # export PGHOST=some_host
        dbitems = {}
        with open('../default_profile') as f:
            for line in f.readlines():
                item = line.split(" ")[1].split("=")
                dbitems[item[0]] = item[1].strip()
    engine = sqlalchemy.create_engine('postgresql+psycopg2://%s:%s@%s/%s' % (
        dbitems['PGUSER'], dbitems['PGPASSWORD'],
        dbitems['PGHOST'], dbitems['PGDATABASE']))
    return (engine, dbitems)


class PGWrangler(object):
    """
    Wraps functions to wrangle postgres databases
    """

    def __init__(self, dbitems=None, 
                 path_to_default_profile='../default_profile'):
        """
        Constructor

        :param dict dbitems: dictionary of database access items
        :param str path_to_default_profile: path to the default database config

        """

        (engine, dbitems) = create_engine(dbitems, path_to_default_profile)
        self.engine = engine

        self.pg_env = os.environ.copy()
        for key in dbitems:
            self.pg_env[key] = dbitems[key]
        self.pgdb = PgSQLDatabase(self.engine)

    def df_to_pg(self, df, table, schema, **kwargs):
        self.pgdb.to_sql(df, table, self.pg_env, schema=schema, **kwargs)

    def create_pg_env(self):
        default_env = os.environ.copy()
        for key in self.pg_env:
            if not key in default_env:
                os.environ[key]=self.pg_env[key]


    def shell(self, cmd):
        """
        Wrapper function to send a shell command with the postgres environment

        :param str cmd: the command

        """
        ps = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=self.pg_env)
        return ps.communicate()[0]

    def execute(self, sql):
        """
        Executes a sql command

        :param str sql: the command
        :rtype: None or [obj]
        :returns: the queried rows

        """
        conn = self.engine.connect()
        result = conn.execute(sql)
        try:
            output = [x for x in result]
        except:
            output = None
        conn.close()
        return output

    def recreate_schema(self, schema_name):
        """
        Create a new schema and delete it if already exists

        :param str schema_name: The name of the schema to create

        """
        conn = self.engine.connect()
        try:
            conn.execute("create schema %s;" % schema_name)
        except:
            conn.execute("drop schema %s cascade;" % schema_name)
            conn.execute("create schema %s;" % schema_name)
        conn.close()

    def check_schema_exists(self, schema):
        """
        Check if a schema exists

        :param str schema: the schema
        :rtype bool:
        """
        sql_query = """
                    select schema_name
                    from information_schema.schemata
                    where schema_name = '%s'
                    """ % schema
        try:
            table_schema = self.execute(sql_query)[0][0]
            to_return = table_schema == schema
        except:
            to_return = False
        return to_return

    def check_table_exists(self, table, schema):
        """
        Check if a table exists

        :param str table: the table
        :param str schema: the schema of the table
        :rtype bool:
        """
        sql_query = """
                    select exists(
                        select 1
                        from information_schema.tables
                        where table_schema = '%s'
                        and table_name = '%s'
                    )
                    """ % (schema, table)
        try:
            return self.execute(sql_query)[0][0]
        except:
            return False

    def check_column_exists(self, column, table, schema):
        """
        Check if column exists

        :param str column: column
        :param str table: table
        :param str schema: schema
        :rtype: bool
        :returns: true if column exists

        """
        sql_query = """
                    select exists(
                        select 1
                        from information_schema.columns
                        where column_name = '%s'
                        and table_name = '%s'
                        and table_schema = '%s'
                    )
                    """ % (column, table, schema)
        try:
            return self.execute(sql_query)[0][0]
        except:
            return False

    def check_column_value(self, f, column, table, schema):
        """
        Check if a column confirms with a pattern

        :param func f: function that checks pattern
        :param str column: column to check
        :param str table: table of column
        :param str schema: schema of table
        """
        conn = self.engine.connect()
        sql_query = """
                    select "%s"
                    from %s.%s
                    """ % (column, schema, table)
        resultproxy = conn.execute(sql_query)
        sample = None
        while sample is None:
            sample = resultproxy.fetchone()[0]
        conn.close()
        return f(sample)

    def delete_duplicates(self, column, table, schema):
        """ Delete duplicate rows

        :param str column: column to identify duplicates
        :param str table: table
        :param str schema: schema

        """
        p = {'table':schema+'.'+table,
             'col':column}
        sql = ("delete from {p[table]} a using( "
               "select min(ctid) as ctid, {p[col]} "
               "from {p[table]} "
               "group by {p[col]} having count(*) > 1 "
               ") b "
               "where a.{p[col]} = b.{p[col]} "
               "and a.ctid <> b.ctid").format(p=p)
        self.execute(sql)

    def check_table_has_nrows(self,table,schema,nrows):
        """
        Check if a table has at least nrows data.

        :param str table: the table
        :param str schema: the schema of the table
        :param nrows int: number of rows expected
        :rtype bool:
        """
        conn = self.engine.connect()

        sql_query = """
                    select count(*)
                    from %s.%s
                    """%(schema,table)

        try:
            to_return = self.execute(sql_query)[0][0]>=nrows
        except:
            to_return = False

        conn.close()
        return to_return

    def get_column_type(self, column, table, schema):
        """
        Returns the data type of a column

        :param str column: the column
        :param str table: the table
        :param str schema: the schema

        :rtype str:
        :returns: the data type of the column

        """
        sql = ("select data_type from "
               "information_schema.columns "
               "where "
               "table_schema = '%s' "
               "and table_name = '%s' "
               "and column_name = '%s'"
               "" % (schema, table, column))
        try:
            return self.execute(sql)[0][0]
        except:
            return None
    
    def get_n_cols(self, table, schema):
        """
        Returns the number of columns in a table

        :param str table: table
        :param str column: column
        :returns: the number of oclumns
        :rtype: int
        """
        sql = ("select count(*) "
               "from information_schema.columns "
               "where table_name='%s' "
               "and table_schema='%s' ")%(table, schema)
        return self.execute(sql)[0][0]



    def drop_table(self, table, schema):
        """
        drops a table

        :param str table: the table to be dropped
        :param str schema: schema of the table to be dropped

        """
        conn = self.engine.connect()
        conn.execute("DROP TABLE IF EXISTS " + schema + "." + table)
        conn.close()

    # ## Copy tables to schema processing
    def get_column_names(self, table_name, schema):
        """
        Takes a postgres table and queries the column names.

        :param string table_name: name of the table
        :param string schema: schema of the table
        :return: list of column names
        :rtype: [str]

        """
        conn = self.engine.connect()
        # Return columns with "" in case they contain caps
        output = set(['"%s"' % x[0].encode() for x in conn.execute(
            "select column_name from information_schema.columns "
            "where table_name='%s' "
            "and table_schema='%s';" % (table_name, schema))])
        conn.close()
        return output

    def copy_table(self, table_name, schema_from='raw', schema_to='processing'):
        """
        Takes a postgres table and copies it to a different schema.
        Formats table name to lower case in destination

        :param str table_name: name of table
        :param str schema_from: origin schema of table
        :param str schema_to: destination schema of table

        """
        conn = self.engine.connect()
        p = {'table': table_name, 'table_lower': table_name.lower(),
             'schema_from': schema_from, 'schema_to': schema_to}
        try:
            conn.execute('create table {p[schema_to]}.{p[table_lower]} '
                         '(like {p[schema_from]}."{p[table]}" including '
                         'constraints '
                         'including indexes);'.format(p=p))
        except:
            # table probably already exists
            # (catching specific psycopg2 error didn't work)
            return
        column_names = self.get_column_names(table_name, schema_from)
        # Only copy table if it has content
        if column_names:
            p['columns'] = ', '.join(column_names)
            conn.execute('insert into {p[schema_to]}.{p[table_lower]} '
                         '({p[columns]}) select {p[columns]} from '
                         '{p[schema_from]}."{p[table]}";'.format(p=p))
            conn.execute("commit;")
        conn.close()

    def drop_empty_columns(self, table_name, schema):
        """
        Takes a postgres table and drops all columns without variation.

        :param str table_name: name of table
        :param str schema: schema of table
        :return: list of dropped columns
        :rtype: [str]

        """
        conn = self.engine.connect()
        columns = self.get_column_names(table_name, schema)
        dropped_columns = []
        for column in columns:
            p = {'col': column, 'table': schema + '.' + table_name}
            # Also drop columns that only have one value
            distinct = conn.execute("select distinct {p[col]} "
                                    "from {p[table]}".format(p=p))
            counter = 0
            nonzero = False
            # Iteratively fetch values and break if >1 distinct values
            for _ in distinct:
                counter += 1
                if counter > 2:
                    nonzero = True
                    break
            if not nonzero:
                conn.execute("alter table {p[table]} "
                             "drop column {p[col]}".format(p=p))
                dropped_columns.append(column)
        conn.close()
        return dropped_columns

    def prettify_date(self, column, table_name, schema):
        """
        Converts a postgres column to datetime.

        :param str column: name of column to prettify
        :param str table_name: name of table
        :param str schema: schema of table

        """
        conn = self.engine.connect()
        p = {'table': schema + '.' + table_name, 'col': '"%s"' % column}
        try:
            # First try built-in conversion
            conn.execute("alter table {p[table]} alter column {p[col]} "
                         "type timestamp "
                         "using {p[col]}::timestamp".format(p=p))
        except:
            # Some very project-specific cases
            try:
                # If column is int/float in the form 20150816235959
                conn.execute("alter table {p[table]} "
                             "alter column {p[col]} type timestamp using "
                             "to_timestamp(to_char({p[col]},"
                             "'99999999999999'),"
                             "'YYYYMMDDHH24MISS')".format(p=p))
            except:
                try:
                    # Some columns are string instead of number
                    # -> convert to int
                    conn.execute("alter table {p[table]} "
                                 "alter column {p[col]} type timestamp "
                                 "using to_timestamp(to_char("
                                 "cast(nullif({p[col]}, '') as float),"
                                 "'99999999999999'),"
                                 "'YYYYMMDDHH24MISS')".format(p=p))
                except:
                    try:
                        # Sometimes only times are given
                        conn.execute("alter table {p[table]} "
                                     "alter column {p[col]} type time "
                                     'using "time"(to_timestamp(to_char('
                                     "cast(nullif({p[col]}, '') as float),"
                                     "'fm000000'),"
                                     "'HH24MISS'))".format(p=p))
                    except:
                        print("Wasn't able to parse " + column)
        conn.close()

    def cast_column(self, column, table_name, schema, typestring):
        """
        Casts a postgres column to the typestring.

        :param str column: name of column to cast
        :param str table_name: name of table
        :param str schema: schema of table
        :param str typestring: sql type to cast to

        """
        conn = self.engine.connect()
        p = {'table': schema + '.' + table_name,
             'col': column,
             'type': typestring}

        sql = ("alter table {p[table]} "
               "alter column {p[col]} "
               "type {p[type]} "
               "using {p[col]}::{p[type]}".format(p=p))

        conn.execute(sql)
        conn.close()

    def left_join(self, left_table, right_table, left_key, right_key,
                  left_schema, right_schema, new_table, cond=None,
                  append_names=True):
        """
        left join between two tables, taking only unique column names

        :param str left_table: name of the left table
        :param str right_table: name of the right table
        :param str left_key: name of the left key
        :param str right_key: name of the right key
        :param str left_schema: name of the left table schema
        :param str right_schema: name of the right table schema
        :param str new_table: name of the new table
        :param (str,(str,str)) cond: name and (min,max) pair of column in left
                                     table to condition on
        :param bool append_names: append name of RIGHT table to its column
                                  names in joint table

        """
        conn = self.engine.connect()
        # Create List of column names for sql command
        left_cols = self.get_column_names(left_table, left_schema)
        right_cols = self.get_column_names(right_table, right_schema)

        if append_names:
            left_unique = [left_schema + '.' +
                           left_table + '.' + x for x in left_cols]
            right_unique = [right_schema + '.' + right_table + '.' + x + " AS "
                            + '"%s__%s"' % (right_table, x[1:-1])
                            for x in right_cols]
        else:
            # Not appending names will discard right-table columns that
            # already exist in left table
            join_set = set(left_cols | right_cols)
            left_unique = [left_schema + '.' + left_table + '.'
                           + col for col in join_set.intersection(left_cols)]
            right_unique = [right_schema + '.' + right_table + '.'
                            + col for col in join_set.difference(left_cols)]

        final_cols = ", ".join(sorted(left_unique + right_unique))

        # Create sql command
        param = {'cols': final_cols, 'l_tab': left_schema + '.' + left_table,
                 'r_tab': right_schema + '.' + right_table, 'l_key': left_key,
                 'r_key': right_key, 'new_tab': new_table}

        sql_string = ("CREATE TABLE {p[new_tab]} AS "
                      "SELECT {p[cols]} "
                      "FROM {p[l_tab]} "
                      "LEFT JOIN "
                      "{p[r_tab]} "
                      'ON {p[l_tab]}."{p[l_key]}" = '
                      '{p[r_tab]}."{p[r_key]}"'.format(p=param))

        # Condition on column and range of values if given in cond
        if cond:
            param['cond_col'] = left_schema + "." + \
                left_table + '."' + cond[0] + '"'
            param['cond_min'] = "'%s'" % cond[1][0]
            param['cond_max'] = "'%s'" % cond[1][1]
            sql_string += (' WHERE {p[cond_col]} '
                           'BETWEEN {p[cond_min]} '
                           'AND {p[cond_max]}'.format(p=param))

        conn.execute(sql_string)
        conn.close()

# Luigi stuff from here on


def get_pgdict_from_cfg():
    """
    loads postgres configuration from luigi config file
    """
    try:
        cfg = configuration.get_config()
        pghost = cfg.get('postgres', 'host')
        pgdb = cfg.get('postgres', 'database')
        pguser = cfg.get('postgres', 'user')
        pgpassword = cfg.get('postgres', 'password')

        dbitems = {'PGUSER': pguser, 'PGPASSWORD': pgpassword,
                   'PGHOST': pghost, 'PGDATABASE': pgdb}

        return dbitems
    except:
        return None


class PostgresTask(luigi.Task):
    """
    Add pgw to LuigiTask
    """

    pgw = PGWrangler(dbitems=get_pgdict_from_cfg())


class PostgresTarget(luigi.target.Target):
    """
    Add pgw to LuigiTarget
    """

    pgw = PGWrangler(dbitems=get_pgdict_from_cfg())


class PGSchemaTarget(PostgresTarget):
    """
    Postgres target that checks the existence of a schema

    :param str schema: the schema
    """

    def __init__(self, schema):
        self.schema = schema

    def exists(self):
        return self.pgw.check_schema_exists(self.schema)


class PGTableTarget(PostgresTarget):
    """
    Postgres target that checks the existence of a table

    :param str table: the table
    :param str schema: the schema
    :param bool inverse: check that it doesn't exist
    """

    def __init__(self, table, schema, inverse=False):
        self.schema = schema
        self.table = table
        self.inverse = inverse

    def exists(self):
        existence =  self.pgw.check_table_exists(self.table, self.schema) 
        if self.inverse:
            return not existence
        else:
            return existence

class PGNonEmptyTableTarget(PostgresTarget):
    """
    Postgres target that checks the existence of a table

    :param str table: the table
    :param str schema: the schema

    """
    def __init__(self,table,schema):
        self.schema = schema
        self.table = table

    def exists(self):
        return self.pgw.check_table_has_nrows(self.table,self.schema, nrows=1)

class PGColumnTarget(PostgresTarget):
    """
    Postgres target that checks the existence of a column

    :param str column: column
    :param str table: table
    :param str schema: schema

    """

    def __init__(self, column, table, schema):
        self.schema = schema
        self.table = table
        self.column = column

    def exists(self):
        return self.pgw.check_column_exists(self.column, self.table,
                                            self.schema)


class PGColValTarget(PostgresTarget):
    """
    Postgres target that checks a value in a given table

    :param func f: a function that checks a column value and returns bool
    :param str column: the column to be checked
    :param str table: the table to be checked
    :param str schema: the schema

    """

    def __init__(self, f, column, table, schema):
        self.f = f
        self.column = column
        self.table = table
        self.schema = schema

    def exists(self):
        return self.pgw.check_column_value(self.f, self.column, self.table,
                                           self.schema)


class PGColTypeTarget(PostgresTarget):
    """
    Postgres target that checks the type of a column

    :param str dtype: the target type
    :param str column: column
    :param str table: table
    :param str schema: schema

    """

    def __init__(self, dtype, column, table, schema):
        self.dtype = dtype
        self.column = column
        self.table = table
        self.schema = schema

    def exists(self):
        return self.pgw.get_column_type(self.column, self.table,
                                        self.schema) == self.dtype

class PGNColumnsTarget(PostgresTarget):
    """
    Postgres target that checks the number of columns

    :param int N: number of required columns
    :param str table: table
    :param str schema: schema
    """

    def __init__(self, N, table, schema):
        self.N = N
        self.table = table
        self.schema = schema

    def exists(self):
        return self.pgw.get_n_cols(self.table, self.schema) == self.N

 
class PGNoDuplicatesTarget(PostgresTarget):
    """ Checks for duplicates

    :param str column: column to identify duplicates
    :param str table: table
    :param str schema: schema
    """
    
    def __init__(self, column, table, schema):
        self.column = column
        self.table = table
        self.schema = schema

    def exists(self):
        full_count = self.pgw.execute("select count(%s) "
                                      "from %s.%s"%(self.column, 
                                      self.schema, self.table))[0][0]
        distinct_count = self.pgw.execute("select count(distinct %s) "
                                          "from %s.%s"%(self.column,
                                          self.schema, self.table))[0][0]
        return full_count == distinct_count

class FileOlderTarget(luigi.target.Target):
    """ Cheks that one file was more recently modified than another file

    :param str old_file: the file assumed to be older
    :param str young_file: the file assumed to be more recent
    """
    
    def __init__(self, old_file, young_file):
        self.old_file = old_file
        self.young_file = young_file

    def exists(self):
        try:
            oldtime = time.ctime(os.path.getmtime(self.old_file))
            youngtime = time.ctime(os.path.getmtime(self.young_file))
            return oldtime < youngtime
        except:
            return False

class DeleteDuplicates(PostgresTask):
    """ Deletes duplicate rows

    :param str column: column to identify duplicates
    :param str table: table
    :param str column: column
    :param [luigi.Task] required: list of required tasks
    """

    column = luigi.Parameter()
    table = luigi.Parameter()
    schema = luigi.Parameter()
    required = luigi.Parameter(default=[])

    def requires(self):
        for x in self.required:
            yield x

    def run(self):
        self.pgw.delete_duplicates(self.column, self.table, self.schema)

    def output(self):
        return PGNoDuplicatesTarget(self.column, self.table, self.schema)


class CreateSchema(PostgresTask):
    """ Task to create a postgres schema

    :param str schema: the schema to be created

    """
    schema = luigi.Parameter()

    def requires(self):
        pass

    def run(self):
        self.pgw.recreate_schema(self.schema)

    def output(self):
        return PGSchemaTarget(self.schema)

class DropTable(PostgresTask):
    """
    Drops a table if it exists

    :param str table: table
    :param str schema: schema
    """

    table = luigi.Parameter()
    schema = luigi.Parameter()

    def run(self):
        self.pgw.drop_table(table=self.table, schema=self.schema)

    def output(self):
        return PGTableTarget(table=self.table, schema=self.schema,
                             inverse=True)

class CastColumn(PostgresTask):
    """
    Task to Cast a column
    """
    dtype = luigi.Parameter()
    column = luigi.Parameter()
    table = luigi.Parameter()
    schema = luigi.Parameter()
    required_tasks = luigi.Parameter()

    def requires(self):
        for t in self.required_tasks:
            yield t

    def run(self):
        self.pgw.cast_column(self.column, self.table, self.schema, self.dtype)

    def output(self):
        return PGColTypeTarget(self.dtype, self.column, self.table,
                               self.schema)


class PGTable(luigi.ExternalTask):
    table = luigi.Parameter()
    schema = luigi.Parameter()

    def output(self):
        return PGTableTarget(self.table, self.schema)


class RawData(luigi.ExternalTask):
    filename = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.filename)
