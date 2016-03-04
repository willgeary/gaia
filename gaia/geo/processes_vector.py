#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc. and Epidemico Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################
import json
import logging
import re
import fiona
import numpy as np
import pandas as pd
try:
    import osr
except ImportError:
    from osgeo import osr
from geopandas import GeoDataFrame, GeoSeries
import gaia.formats as formats
from gaia.core import GaiaException
from gaia.geo.gaia_process import GaiaProcess
from gaia.geo.gdal_functions import gdal_zonalstats
from gaia.inputs import VectorFileIO, df_from_postgis

logger = logging.getLogger('gaia.geo')


class BufferProcess(GaiaProcess):
    """
    Generates a buffer polygon around the geometries of the input data.
    The size of the buffer is determined by the 'buffer_size' args key
    and the unit of measure should be meters.  If inputs are not in a
    metric projection they will be reprojected to EPSG:3857.
    """
    required_inputs = (('input', formats.VECTOR),)
    required_args = ('buffer_size',)
    default_output = formats.JSON

    def __init__(self, inputs=None, buffer_size=None, **kwargs):
        super(BufferProcess, self).__init__(inputs, **kwargs)
        self.buffer_size = buffer_size
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        featureio = self.inputs[0]
        original_projection = featureio.get_epsg()
        epsg = original_projection
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857
        else:
            original_projection = None
        feature_df = featureio.read(epsg=epsg)
        buffer = GeoSeries(feature_df.buffer(self.buffer_size).unary_union)
        buffer_df = GeoDataFrame(geometry=buffer)
        buffer_df.crs = feature_df.crs
        if original_projection:
            buffer_df[buffer_df.geometry.name] = buffer_df.to_crs(
                epsg=original_projection)
            buffer_df.crs = fiona.crs.from_epsg(original_projection)
        return buffer_df

    def calc_postgis(self):
        pg_io = self.inputs[0]
        original_projection = pg_io.epsg
        io_query, params = pg_io.get_query()
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))

        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            geom_query = 'ST_Transform({}, {})'.format(
                pg_io.geom_column, 3857)
        else:
            original_projection = None
        buffer_query = 'ST_Union(ST_Buffer({}, %s))'.format(geom_query)
        if original_projection:
            buffer_query = 'ST_Transform({}, {})'.format(buffer_query,
                                                         original_projection)

        query = 'SELECT {buffer} as {geocol} ' \
                'FROM ({query}) as foo'.format(buffer=buffer_query,
                                               geocol=pg_io.geom_column,
                                               query=io_query.rstrip(';'))
        params.insert(0, self.buffer_size)
        logger.debug(query)
        return df_from_postgis(pg_io.engine, query, params,
                               pg_io.geom_column, pg_io.epsg)

    def compute(self):
        if self.inputs[0].__class__.__name__ == 'PostgisIO':
            data = self.calc_postgis()
        else:
            data = self.calc_pandas()
        self.output.data = data
        self.output.write()


class WithinProcess(GaiaProcess):
    """
    Similar to SubsetProcess but for vectors: calculates the features within
    a vector dataset that are within (or whose centroids are within) the
    polygons of a second vector dataset.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(WithinProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_within = first_df[first_df.geometry.within(
            second_df.geometry.unary_union)]
        return first_within

    def calc_postgis(self):
        first = self.inputs[0]
        within_queries = []
        within_params = []
        geom0 = first.geom_column
        epsg = first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            within_queries.append(io_query.rstrip(';'))
            within_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in within_queries[0].upper() else ' WHERE '
        query = '{query0} {join} ST_Within(ST_Transform({geom0},{epsg}), ' \
                '(SELECT ST_Union(ST_TRANSFORM({geom1},{epsg})) ' \
                'from ({query1}) as q2))'\
            .format(query0=within_queries[0], join=joinstr, geom0=geom0,
                    geom1=geom1, epsg=epsg, query1=within_queries[1])
        return df_from_postgis(first.engine, query, params, geom0, epsg)

    def compute(self):
        if len(self.inputs) != 2:
            raise GaiaException('WithinProcess requires 2 inputs')
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()


class IntersectsProcess(GaiaProcess):
    """
    Calculates the features within the first vector dataset that touch
    the features of the second vector dataset.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(IntersectsProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_intersects = first_df[first_df.geometry.intersects(
            second_df.geometry.unary_union)]
        return first_intersects

    def calc_postgis(self):
        int_queries = []
        int_params = []
        first = self.inputs[0]
        geom0 = first.geom_column
        epsg = first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            int_queries.append(io_query.rstrip(';'))
            int_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in int_queries[0].upper() else ' WHERE '
        query = '{query0} {join} (SELECT ST_Intersects(ST_Transform(' \
                '{table}.{geom0},{epsg}), ST_Union(ST_Transform(' \
                'q2.{geom1},{epsg}))) from ({query1}) as q2)'\
            .format(query0=int_queries[0], join=joinstr, geom0=geom0,
                    geom1=geom1, epsg=epsg, query1=int_queries[1],
                    table=first.table)
        return df_from_postgis(first.engine,
                               query, int_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class DisjointProcess(GaiaProcess):
    """
    Calculates which features of the first vector dataset do not
    intersect the features of the second dataset.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(DisjointProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_difference = first_df[first_df.geometry.disjoint(
            second_df.geometry.unary_union)]
        return first_difference

    def calc_postgis(self):
        diff_queries = []
        diff_params = []
        first = self.inputs[0]
        geom0, epsg = first.geom_column, first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            diff_queries.append(io_query.rstrip(';'))
            diff_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in diff_queries[0].upper() else ' WHERE '
        query = '{query0} {join} (SELECT ST_Disjoint(ST_Transform(' \
                '{table}.{geom0},{epsg}), ST_Union(ST_Transform(' \
                'q2.{geom1},{epsg}))) from ({query1}) as q2)'\
            .format(query0=diff_queries[0], join=joinstr, geom0=geom0,
                    geom1=geom1, epsg=epsg, query1=diff_queries[1],
                    table=first.table)
        return df_from_postgis(first.engine, query, diff_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class UnionProcess(GaiaProcess):
    """
    Combines two vector datasets into one.
    They should have the same columns.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(UnionProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        if ''.join(first_df.columns) != ''.join(second_df.columns):
            raise GaiaException('Inputs must have the same columns')
        uniondf = GeoDataFrame(pd.concat([first_df, second_df]))
        return uniondf

    def calc_postgis(self):
        union_queries = []
        union_params = []
        first = self.inputs[0]
        second = self.inputs[1]
        geom0, epsg = first.geom_column, first.epsg
        geom1, epsg1 = second.geom_column, second.epsg
        if ''.join(first.columns) != ''.join(second.columns):
            raise GaiaException('Inputs must have the same columns')
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            union_queries.append(io_query.rstrip(';'))
            union_params.extend(params)

        if epsg1 != epsg:
            geom1_query = 'ST_Transform({},{})'.format(geom1, epsg)
            union_queries[1] = union_queries[1].replace(
                '"{}"'.format(geom1), geom1_query)
        query = '({query0}) UNION ({query1})'\
            .format(query0=union_queries[0], query1=union_queries[1])
        return df_from_postgis(first.engine,
                               query, union_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class CentroidProcess(GaiaProcess):
    """
    Calculates the centroid point of a vector dataset.
    """

    required_inputs = (('first', formats.VECTOR),)
    required_args = ()
    default_output = formats.JSON

    def __init__(self, combined=False, **kwargs):
        super(CentroidProcess, self).__init__(**kwargs)
        self.combined = combined
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())

    def calc_pandas(self):
        df_in = self.inputs[0].read()
        df = GeoDataFrame(df_in.copy(), geometry=df_in.geometry.name)
        if self.combined:
            gs = GeoSeries(df.geometry.unary_union.centroid,
                           name=df_in.geometry.name)
            return GeoDataFrame(gs)
        else:
            df[df.geometry.name] = df.geometry.centroid
            return df

    def calc_postgis(self):
        pg_io = self.inputs[0]
        io_query, params = pg_io.get_query()
        geom0, epsg = pg_io.geom_column, pg_io.epsg
        if self.combined:
            query = 'SELECT ST_Centroid(ST_Union({geom})) as {geom}' \
                    ' from ({query}) as foo'.format(geom=geom0,
                                                    query=io_query.rstrip(';'))
        else:
            query = re.sub('"{}"'.format(geom0),
                           'ST_Centroid("{geom}") as {geom}'.format(
                               geom=geom0), io_query, 1)
        return df_from_postgis(pg_io.engine, query, params, geom0, epsg)

    def compute(self):
        use_postgis = self.inputs[0].__class__.__name__ == 'PostgisIO'
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class DistanceProcess(GaiaProcess):
    """
    Calculates the minimum distance from each feature of the first dataset
    to the nearest feature of the second dataset.
    """
    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(DistanceProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first = self.inputs[0]
        original_projection = first.get_epsg()
        epsg = original_projection
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857
        else:
            original_projection = None
        first_df = first.read(epsg=epsg)
        first_gs = first_df.geometry
        first_length = len(first_gs)
        second_df = self.inputs[1].read(epsg=epsg)
        second_gs = second_df.geometry
        min_dist = np.empty(first_length)
        for i, first_features in enumerate(first_gs):
            min_dist[i] = np.min([first_features.distance(second_features)
                                  for second_features in second_gs])

        distance_df = GeoDataFrame.copy(first_df)
        distance_df['distance'] = min_dist
        distance_df.sort_values('distance', inplace=True)
        if original_projection:
            distance_df[distance_df.geometry.name] = \
                distance_df.geometry.to_crs(epsg=original_projection)
        return distance_df

    def calc_postgis(self):
        """
        Uses K-Nearest Neighbor (KNN) query
        """
        diff_queries = []
        diff_params = []
        first = self.inputs[0]
        geom0, epsg = first.geom_column, first.epsg
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(epsg))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857

        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            diff_queries.append(io_query.rstrip(';'))
            diff_params.insert(0, params)

        diff_params = [item for x in diff_params for item in x]
        dist1 = """, (SELECT ST_Distance(
                ST_Transform({table0}.{geom0},{epsg}),
                ST_Transform(query2.{geom1},{epsg}))
                as distance
                """.format(table0=self.inputs[0].table,
                           geom0=geom0,
                           geom1=geom1,
                           epsg=epsg)

        dist2 = """
                ORDER BY {table0}.{geom0} <#> query2.{geom1} LIMIT 1) FROM
                """.format(table0=self.inputs[0].table,
                           geom0=geom0,
                           geom1=geom1,
                           epsg=epsg)

        dist3 = ' ORDER BY distance ASC'
        query = re.sub('FROM', dist1 + ' FROM (' + diff_queries[1] +
                       ') as query2 ' + dist2, diff_queries[0]) + dist3
        return df_from_postgis(first.engine, query, diff_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()


class NearProcess(GaiaProcess):
    """
    Takes two inputs, the second assumed to contain a single feature,
    the first a vector dataset. Requires a distance argument, and the unit of
    measure should be meters.  If inputs are not in a
    metric projection they will be reprojected to EPSG:3857.
    Returns the features in the second input within a specified distance
    of the point in the first input.

    """
    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ('distance',)
    default_output = formats.JSON

    def __init__(self, distance=None, **kwargs):
        super(NearProcess, self).__init__(**kwargs)
        self.distance = distance
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        features = self.inputs[0]
        original_projection = self.inputs[0].get_epsg()
        epsg = original_projection
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857
        else:
            original_projection = None
        features_df = features.read(epsg=epsg)
        features_gs = features_df.geometry
        point_df = self.inputs[1].read(epsg=epsg)[:1]
        point_gs = point_df.geometry
        features_length = len(features_gs)
        min_dist = np.empty(features_length)
        for i, feature in enumerate(features_gs):
            min_dist[i] = np.min([feature.distance(point_gs[0])])

        nearby_df = GeoDataFrame.copy(features_df)
        nearby_df['distance'] = min_dist
        distance_max = self.distance
        nearby_df = nearby_df[(nearby_df['distance'] <= distance_max)]\
            .sort_values('distance')
        if original_projection:
            nearby_df[nearby_df.geometry.name] = \
                nearby_df.geometry.to_crs(epsg=original_projection)
        return nearby_df

    def calc_postgis(self):
        """
        Uses DWithin plus K-Nearest Neighbor (KNN) query
        """
        featureio = self.inputs[0]
        pointio = self.inputs[1]
        feature_geom, epsg = featureio.geom_column, featureio.epsg
        point_json = json.loads(pointio.read(
            format=formats.JSON))['features'][0]
        point_epsg = pointio.get_epsg()

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(epsg))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857

        io_query, params = featureio.get_query()

        point_geom = 'ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(\'' \
                     '{geojson}\'),{point_epsg}), {epsg})'.\
            format(geojson=json.dumps(point_json['geometry']),
                   point_epsg=point_epsg, epsg=epsg)

        dist1 = """, (SELECT ST_Distance(
                ST_Transform({table0}.{geom0},{epsg}),
                ST_Transform(point, {epsg}))
                FROM {point_geom} as point
                ORDER BY {table0}.{geom0} <#> point LIMIT 1) as distance FROM
                """.format(table0=featureio.table,
                           geom0=feature_geom,
                           point_geom=point_geom,
                           epsg=epsg)

        dist2 = """
                WHERE ST_DWithin({point_geom},
                ST_Transform({table0}.{geom0},{epsg}), {distance})
                """.format(table0=featureio.table,
                           geom0=feature_geom,
                           point_geom=point_geom,
                           epsg=epsg,
                           distance=self.distance)

        dist3 = ' ORDER BY distance ASC'
        query = re.sub('FROM', dist1, io_query).rstrip(';')
        if 'WHERE' in query:
            query = re.sub('WHERE', dist2 + ' AND ', query)
        else:
            query += dist2
        query += dist3
        logger.debug(query)
        return df_from_postgis(featureio.engine,
                               query, params, feature_geom, epsg)

    def compute(self):
        if self.inputs[0].__class__.__name__ == 'PostgisIO':
            data = self.calc_postgis()
        else:
            data = self.calc_pandas()
        self.output.data = data
        self.output.write()


class AreaProcess(GaiaProcess):
    """
    Calculate the area of each polygon feature in a dataset.
    If the dataset projection is not in metric units, it will
    be temporarily reprojected to EPSG:3857 to calculate the area.
    """
    required_inputs = (('first', formats.VECTOR),)
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(AreaProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        featureio = self.inputs[0]
        original_projection = featureio.get_epsg()
        epsg = original_projection
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857
        else:
            original_projection = None
        feature_df = GeoDataFrame.copy(featureio.read(epsg=epsg))
        feature_df['area'] = feature_df.geometry.area
        if original_projection:
            feature_df[feature_df.geometry.name] = feature_df.geometry.to_crs(
                epsg=original_projection)
            feature_df.crs = fiona.crs.from_epsg(original_projection)
        return feature_df

    def calc_postgis(self):
        pg_io = self.inputs[0]
        geom0, epsg = pg_io.geom_column, pg_io.epsg
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)

        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            geom_query = 'ST_Transform({}, {})'.format(geom0, 3857)
        geom_query = ', ST_Area({}) as area'.format(geom_query)
        query, params = pg_io.get_query()
        query = query.replace('FROM', '{} FROM'.format(geom_query))
        logger.debug(query)
        return df_from_postgis(pg_io.engine, query, params, geom0, epsg)

    def compute(self):
        if self.inputs[0].__class__.__name__ == 'PostgisIO':
            data = self.calc_postgis()
        else:
            data = self.calc_pandas()
        self.output.data = data
        self.output.write()


class LengthProcess(GaiaProcess):
    """
    Calculate the length of each feature in a dataset.
    If the dataset projection is not in metric units, it will
    be temporarily reprojected to EPSG:3857 to calculate the area.
    """
    required_inputs = (('first', formats.VECTOR),)
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(LengthProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        featureio = self.inputs[0]
        original_projection = featureio.get_epsg()
        epsg = original_projection
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(original_projection))
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            epsg = 3857
        else:
            original_projection = None
        feature_df = GeoDataFrame.copy(featureio.read(epsg=epsg))
        feature_df['length'] = feature_df.geometry.length
        if original_projection:
            feature_df[feature_df.geometry.name] = feature_df.geometry.to_crs(
                epsg=original_projection)
            feature_df.crs = fiona.crs.from_epsg(original_projection)
        return feature_df

    def calc_postgis(self):
        featureio = self.inputs[0]
        geom0, epsg = featureio.geom_column, featureio.epsg
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        geom_query = geom0
        geometry_type = featureio.geometry_type
        length_func = 'ST_Perimeter' if 'POLYGON' in geometry_type.upper() \
            else 'ST_Length'
        if not srs.GetAttrValue('UNIT').lower().startswith('met'):
            geom_query = 'ST_Transform({}, {})'.format(
                geom_query, 3857)
        geom_query = ', {}({}) as length'.format(length_func, geom_query)
        query, params = featureio.get_query()
        query = query.replace('FROM', '{} FROM'.format(geom_query))
        logger.debug(query)
        return df_from_postgis(featureio.engine, query, params, geom0, epsg)

    def compute(self):
        if self.inputs[0].__class__.__name__ == 'PostgisIO':
            data = self.calc_postgis()
        else:
            data = self.calc_pandas()
        self.output.data = data
        self.output.write()


class CrossesProcess(GaiaProcess):
    """
    Calculates the features within the first vector dataset that cross
    the combined features of the second vector dataset.
    """
    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(CrossesProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_intersects = first_df[first_df.geometry.crosses(
            second_df.geometry.unary_union)]
        return first_intersects

    def calc_postgis(self):
        cross_queries = []
        cross_params = []
        first = self.inputs[0]
        geom0, epsg = first.geom_column, first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            cross_queries.append(io_query.rstrip(';'))
            cross_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in cross_queries[0].upper() else ' WHERE '
        query = '{query0} {join} (SELECT ST_Crosses(ST_Transform(' \
                '{table}.{geom0},{epsg}), ST_Union(ST_Transform(' \
                'q2.{geom1},{epsg}))) from ({query1}) as q2)'\
            .format(query0=cross_queries[0], join=joinstr, geom0=geom0,
                    geom1=geom1, epsg=epsg, query1=cross_queries[1],
                    table=first.table)
        return df_from_postgis(first.engine, query, cross_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class TouchesProcess(GaiaProcess):
    """
    Calculates the features within the first vector dataset that touch
    the features of the second vector dataset.
    """
    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(TouchesProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_intersects = first_df[first_df.geometry.touches(
            second_df.geometry.unary_union)]
        return first_intersects

    def calc_postgis(self):
        cross_queries = []
        cross_params = []
        first = self.inputs[0]
        geom0, epsg = first.geom_column, first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            cross_queries.append(io_query.rstrip(';'))
            cross_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in cross_queries[0].upper() else ' WHERE '
        query = '{query0} {join} (SELECT ST_Touches(ST_Transform(' \
                '{table}.{geom0},{epsg}), ST_Union(ST_Transform(' \
                'q2.{geom1},{epsg}))) from ({query1}) as q2)'\
            .format(query0=cross_queries[0], join=joinstr, geom0=geom0,
                    geom1=geom1, epsg=epsg, query1=cross_queries[1],
                    table=first.table)
        return df_from_postgis(first.engine, query, cross_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class EqualsProcess(GaiaProcess):
    """
    Calculates the features within the first vector dataset that touch
    the features of the second vector dataset.
    """

    required_inputs = (('first', formats.VECTOR), ('second', formats.VECTOR))
    required_args = ()
    default_output = formats.JSON

    def __init__(self, **kwargs):
        super(EqualsProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def calc_pandas(self):
        first, second = self.inputs[0], self.inputs[1]
        first_df = first.read()
        second_df = second.read(epsg=first.get_epsg())
        first_gs = first_df.geometry
        first_length = len(first_gs)
        second_gs = second_df.geometry
        matches = np.empty(first_length)
        for i, first_features in enumerate(first_gs):
            matched = [first_features.equals(second_features)
                       for second_features in second_gs]
            matches[i] = True if (True in matched) else False
        output_df = GeoDataFrame.copy(first_df)
        output_df['equals'] = matches
        output_df = output_df[
            (output_df['equals'] == 1)].drop('equals', 1)
        return output_df

    def calc_postgis(self):
        equals_queries = []
        equals_params = []
        first = self.inputs[0]
        geom0, epsg = first.geom_column, first.epsg
        geom1 = self.inputs[1].geom_column
        for pg_io in self.inputs:
            io_query, params = pg_io.get_query()
            equals_queries.append(io_query.rstrip(';'))
            equals_params.extend(params)
        joinstr = ' AND ' if 'WHERE' in equals_queries[0].upper() else ' WHERE '
        query = '{query0} {join} {geom0} IN (SELECT {geom1} ' \
                'FROM ({query1}) as second)'.format(query0=equals_queries[0],
                                                    query1=equals_queries[1],
                                                    join=joinstr,
                                                    geom0=geom0,
                                                    geom1=geom1)
        logger.debug(query)
        return df_from_postgis(first.engine, query, equals_params, geom0, epsg)

    def compute(self):
        input_classes = list(self.get_input_classes())
        use_postgis = (len(input_classes) == 1 and
                       input_classes[0] == 'PostgisIO')
        data = self.calc_postgis() if use_postgis else self.calc_pandas()
        self.output.data = data
        self.output.write()
        logger.debug(self.output)


class ZonalStatsProcess(GaiaProcess):
    """
    Calculates statistical values from a raster dataset for each polygon
    in a vector dataset.
    """
    required_inputs = (('raster', formats.RASTER), ('zones', formats.VECTOR),)
    required_args = ()
    default_output = formats.VECTOR

    def __init__(self, **kwargs):
        super(ZonalStatsProcess, self).__init__(**kwargs)
        if not self.output:
            self.output = VectorFileIO(name='result',
                                       uri=self.get_outpath())
        self.validate()

    def compute(self):
        self.output.create_output_dir(self.output.uri)
        features = gdal_zonalstats(
            self.inputs[1].read(format=formats.JSON,
                                epsg=self.inputs[0].get_epsg()),
            self.inputs[0].read())
        self.output.data = GeoDataFrame.from_features(features)
        self.output.write()