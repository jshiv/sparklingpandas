"""Provide a way to work with panda data frames in Spark"""
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from sparklingpandas.utils import add_pyspark_path
from sparklingpandas.pstats import PStats
from functools import reduce
from itertools import chain, imap

add_pyspark_path()
from pyspark.join import python_join, python_left_outer_join, \
    python_right_outer_join, python_cogroup
from pyspark.rdd import RDD
import pyspark
import pandas



class Dataframe:

    """A Panda Resilient Distributed Dataset (Dataframe), is based on
    Spark SQL's DataFrame (previously known as SchemaRDD). Dataframes aim to
    provide, as close as reasonable, Panda's compatable inferface.
    Note: RDDs are lazy, so you operations are not performed until required."""

    def __init__(self, schema_rdd, sql_ctx, index_names=[None]):
        self._schema_rdd = schema_rdd
        self.sql_ctx = sql_ctx
        # Keep track of what our index is called.
        # Defaults to None. If None we use an implicit index called index.
        self._index_names = index_names

    def _rdd(self):
        """Return an RDD of Panda DataFrame objects. This can be expensive
        especially if we don't do a narrow transformation after and get it back
        to Spark SQL land quickly."""
        columns = self._schema_rdd.columns
        index_names = self._index_names

        def fromRecords(records):
            if not records:
                return []
            else:
                df = pandas.DataFrame.from_records([records], columns=columns)
                df = _update_index_on_df(df, index_names)
                return [df]

        return self._schema_rdd.rdd.flatMap(fromRecords)

    def __evil_apply_with_dataframes(self, func):
        """Convert the underlying SchmeaRDD to an RDD of DataFrames.
        apply the provide function and convert the result back.
        This is hella slow."""
        source_rdd = self._rdd()
        result_rdd = func(source_rdd)
        return self.from_rdd_of_dataframes(result_rdd)

    def from_rdd_of_dataframes(self, rdd):
        """Take an RDD of dataframes and return a Dataframe"""
        def frame_to_spark_sql(frame):
            """Convert a Panda's DataFrame into Spark SQL Rows"""
            # TODO: Convert to row objects directly?
            return [r.tolist() for r in frame.to_records()]
        # Todo, compute worker side rather than bringing a frame back
        first_df = rdd.first()
        schema = list(first_df.columns)
        # If we have an explicit index use it, otherwise create implicit index field
        index_names = list(first_df.index.names)
        z = 0
        while z < len(index_names):
            if not index_names[z]:
                index_names[z] = "_sparkling_panda_magic_index_"+str(z)
            z = z+1
        schema = index_names + schema
        ddf = Dataframe.fromSchemaRDD(self.sql_ctx.createDataFrame(rdd.flatMap(frame_to_spark_sql), schema=schema))
        ddf._index_names = index_names
        return ddf

    @classmethod
    def fromSchemaRDD(cls, schemaRdd):
        """Construct a Dataframe from an SchemaRDD.
        No checking or validation occurs."""
        return Dataframe(schemaRdd, schemaRdd.sql_ctx)

    @classmethod
    def fromDataFrameRDD(cls, rdd, sql_ctx):
        """Construct a Dataframe from an RDD of DataFrames.
        No checking or validation occurs."""
        result = Dataframe(None, sql_ctx)
        return result.from_rdd_of_dataframes(rdd)

    @classmethod
    def from_spark_df(cls, schema_rdd):
        """Construct a Dataframe from an RDD. No checking or validation occurs."""
        return Dataframe(schema_rdd, schema_rdd.sql_ctx)

    def to_spark_sql(self):
        """A Sparkling Pandas specific function to turn a DDF into
        something that Spark SQL can query. To use the result you will
        need to call sqlCtx.inferSchema(rdd) and then register the result
        as a table. Once Spark 1.1 is released this function may be deprecated
        and replacted with to_spark_sql_schema_rdd."""
        return self._schema_rdd

    def applymap(self, f, **kwargs):
        """Return a new Dataframe by applying a function to each element of each
        Panda DataFrame."""
        return self.from_rdd_of_dataframes(
            self._rdd().map(lambda data: data.applymap(f), **kwargs))

    def __getitem__(self, key):
        """Returns a new Dataframe of elements from those keys.
        Note: this differs from Pandas DataFrames in that when selecting
        a single key it returns a series, however we don't have good
        support for series just yet so we always returns DataFrames.
        """
        return self.from_spark_df(self._schema_rdd.select(key))

    def groupby(self, by=None, axis=0, level=None, as_index=True, sort=True,
                group_keys=True, squeeze=False):
        """Returns a groupby on the schema rdd. This returns a GroupBy object.
        Note that grouping by a column name will be faster than most other
        options due to implementation."""
        from sparklingpandas.groupby import GroupBy
        return GroupBy(self, by=by, axis=axis, level=level, as_index=as_index,
                       sort=sort, group_keys=group_keys, squeeze=squeeze)

    def _first_as_df(self):
        """Gets the first row as a dataframe. Useful for functions like
        dtypas & ftypes"""
        columns=self._schema_rdd.columns
        df = pandas.DataFrame.from_records(
            [self._schema_rdd.first()],
            columns=self._schema_rdd.columns)
        df = _update_index_on_df(df, self._index_names)
        return df


    @property
    def dtypes(self):
        """
        Return the dtypes associated with this object
        Uses the types from the first frame.
        """
        return self._first_as_df().dtypes

    @property
    def ftypes(self):
        """
        Return the ftypes associated with this object
        Uses the types from the first frame.
        """
        return self._first_as_df().ftypes

    def get_dtype_counts(self):
        """
        Return the counts of dtypes in this object
        Uses the information from the first frame
        """
        return self._first_as_df().get_dtype_counts()

    def get_ftype_counts(self):
        """
        Return the counts of ftypes in this object
        Uses the information from the first frame
        """
        return self._first_as_df().get_ftype_counts()

    @property
    def axes(self):
        """
        Returns the axes. Note that the row indexes are junk (TODO fix this)
        """
        return (self._rdd().map(lambda frame: frame.axes)
                .reduce(lambda xy, ab: [xy[0].append(ab[0]), xy[1]]))

    @property
    def shape(self):
        return (self._rdd().map(lambda frame: frame.shape)
                .reduce(lambda xy, ab: (xy[0] + ab[0], xy[1])))

    def collect(self):
        """Collect the elements in an Dataframe and concatenate the partition."""
        df = self._schema_rdd.toPandas()
        df = _update_index_on_df(df, self._index_names)
        return df

    def stats(self, columns):
        """Compute the stats for each column provided in columns.
        Parameters
        ----------
        columns : list of str, contains all columns to compute stats on.
        """
        assert (not isinstance(columns, basestring)), "columns should be a " \
                                                      "list of strs,  " \
                                                      "not a str!"
        assert isinstance(columns, list), "columns should be a list!"

        from pyspark.sql import functions as F
        functions = [F.min, F.max, F.avg, F.count]
        aggs = list(self._flatmap(lambda column: map(lambda f: f(column), functions),columns))
        return PStats(self.fromSchemaRDD(self._schema_rdd.agg(*aggs)))

    def min(self):
        return self.from_spark_df(Dataframe._schema_rdd.min())

    def max(self):
        return self.from_spark_df(Dataframe._schema_rdd.max())

    def avg(self):
        return self.from_spark_df(Dataframe._schema_rdd.avg())

    def _flatmap(self, f, items):
        return chain.from_iterable(imap(f, items))

def _update_index_on_df(df, index_names):
    """Helper function to restore index information after collection. Doesn't
    use self so we can serialize this."""
    df = df.set_index(index_names)
    # Remove names from unnamed indexes
    z = 0
    while z < len(index_names):
        if index_names[z].startswith('_sparkling_panda_magic_index_'):
            index_names[z] = None
        z = z+1
    df.index.names = index_names
    return df
