from textwrap import dedent
import _ast
from itertools import product
import os
import json
from datetime import timedelta
import csv
import io
from uuid import uuid4
import tempfile
import traceback

from dateutil.parser import parse
from flask import (
    Blueprint, request, Response, url_for, jsonify, send_file, after_this_request
)
from eoxserver.core.util.timetools import parse_iso8601, parse_duration
from eoxserver.render.browse.generate import parse_expression, extract_fields
from eoxserver.contrib.vsi import TemporaryVSIFile
from osgeo import ogr, gdal
import numpy as np
import netCDF4

from edc_ogc.configapi import ConfigAPIDefaultLayers


CONFIG_CLIENT = None

def get_config_client():
    global CONFIG_CLIENT
    datasets_path = os.environ.get('DATASETS_PATH')
    layers_path = os.environ.get('LAYERS_PATH')
    dataproducts_path = os.environ.get('DATAPRODUCTS_PATH')
    client_id = os.environ.get('SH_CLIENT_ID')
    client_secret = os.environ.get('SH_CLIENT_SECRET')

    if CONFIG_CLIENT is None:
        CONFIG_CLIENT = ConfigAPIDefaultLayers(
            client_id,
            client_secret,
            datasets_path=datasets_path,
            layers_path=layers_path,
            dataproducts_path=dataproducts_path
        )

    return CONFIG_CLIENT


dapa = Blueprint('dapa', __name__)


'''
/{collection}/dapa/
    fields/
    cube/
    area/
    timeseries/
        area/
        position/
    value/
        area/
        position/
'''

def parse_fields(value):
    fields = value.split(',')

    parsed = []
    inputs = set()
    for field in fields:
        if '=' in field:
            key, _, value = field.partition('=')
            expr = parse_expression(value)
            inputs.update(extract_fields(expr))
            parsed.append((key, expr))
        else:
            parsed.append((field, field))
            inputs.add(field)

    return parsed, inputs


def parse_aggregates(value):
    aggs = value.split(',')
    allowed = ('min', 'max', 'avg', 'stdev')
    for agg in aggs:
        if agg not in allowed:
            raise ValueError(
                f"Invalid aggregates item. Must be one of {', '.join(allowed)}"
            )
    return aggs


def parse_bbox(value):
    bbox = [float(v) for v in value.split(',')]
    if len(bbox) not in (4, 6):
        raise ValueError('Invalid number of elements in bbox')
    return bbox

def parse_point(value):
    bbox = [float(v) for v in value.split(',')]
    if len(bbox) not in (2, 3):
        raise ValueError('Invalid number of elements in position')
    return bbox


def parse_time(value):
    parts = value.split('/')

    if len(parts) == 1:
        return [parse_iso8601(parts[0])]

    elif len(parts) == 2:
        # TODO also allow durations
        return [
            parse_iso8601(part) for part in parts
        ]

    else:
        raise ValueError(f'Invalid time value: {value}')


def search_times(dataset, catalog_client, bbox_or_geom, time):
    result_times = []
    next_key = None
    while True:
        search_response = json.loads(
            catalog_client.search(
                dataset['search_collection'], bbox_or_geom, time,
                fields=['property.datetime'],
                next_key=next_key,
            )
        )
        result_times.extend(
            feature['properties']['datetime']
            for feature in search_response['features']
        )

        next_key = None
        for link in search_response.get('links', []):
            if link.get('rel') == 'next':
                next_key = link.get('body', {}).get('next')

        if not next_key:
            break

    result_times.sort()
    return result_times


OPERATOR_MAP = {
    _ast.Add: '+',
    _ast.Sub: '-',
    _ast.Div: '/',
    _ast.Mult: '*',
}


def eval_expression(expr, varname='sample'):
    if isinstance(expr, _ast.Name):
        return f'{varname}.{expr.id}'
    elif isinstance(expr, _ast.BinOp):
        op = OPERATOR_MAP[type(expr.op)]
        return f'({eval_expression(expr.left)} {op} {eval_expression(expr.right)})'
    elif isinstance(expr, _ast.Num):
        return str(expr.n)


def expressions_to_evalscript(fields, inputs, aggregates=None):
    static_fields = []
    dynamic_fields = []
    for name, value in fields:
        if isinstance(value, str):
            static_fields.append(name)
        else:
            dynamic_fields.append((name, eval_expression(value)))

    if aggregates:
        out_fields = [
            f'agg_{agg_method}(values.{name})'
            for name, _ in fields for agg_method in aggregates
        ]
    else:
        out_fields = [
            f'sample.{name}'
            for name, _ in fields
        ]

    return dedent(f"""\
        //VERSION=3
        function setup() {{
            return {{
                input: [{', '.join(f'"{input_}"' for input_ in inputs)}],
                mosaicking: {'"ORBIT"' if aggregates else '"SIMPLE"'}, // TODO
                //mosaicking: "ORBIT",
                output: {{
                    bands: {len(fields) * (len(aggregates) if aggregates else 1)},
                    sampleType: 'FLOAT32'
                }}
            }};
        }}

        function agg_min(values) {{
            return values
                .reduce((acc, value) => Math.min(acc, value));
        }}

        function agg_max(values) {{
            return values
                .reduce((acc, value) => Math.max(acc, value));
        }}

        function agg_avg(values) {{
            return values
                .reduce((acc, value) => acc + value) / values.length;
        }}

        function agg_stdev(values) {{
            const mean = agg_avg(values);
            return Math.sqrt(
                values
                    .reduce((acc, value) => acc + Math.pow(value - mean, 2), 0) / (values.length - 1)
            );
        }}

        function evaluatePixelSamples(samples, scenes, inputMetadata, customData, outputMetadata) {{
            samples = samples.map(sample => ({{
                {' '.join(f'{field}: sample.{field},' for field in static_fields)}
                {' '.join(f'{name}: {expr},' for name, expr in dynamic_fields)}
            }}));

            const values = {{
                {', '.join(f'{field}: samples.map(sample => sample.{field})' for field, _ in fields)}
            }};

            return [
                {', '.join(out_fields)}
            ];
        }}

        function evaluatePixelSample(sample, scenes, inputMetadata, customData, outputMetadata) {{
            sample = {{
                {' '.join(f'{field}: sample.{field},' for field in static_fields)}
                {' '.join(f'{name}: {expr},' for name, expr in dynamic_fields)}
            }};

            return [
                {', '.join(out_fields)}
            ];
        }}

        function evaluatePixel(samples, scenes, inputMetadata, customData, outputMetadata) {{
            return {'evaluatePixelSamples' if aggregates else 'evaluatePixelSample'}(samples, scenes, inputMetadata, customData, outputMetadata);
        }}
    """)


def get_area_aggregate_time(collection, fields, inputs, aggregates, time, bbox_or_geom, bbox,
                            width=None, height=None, format='image/tiff'):
    client = get_config_client()

    evalscript = expressions_to_evalscript(fields, inputs, aggregates)

    ds = client.get_dataset(collection)

    dx, dy = ds['resolution']
    width = width if width is not None else min(512, int(abs((bbox[2] - bbox[0]) / dx)))
    height = height if height is not None else min(512, int(abs((bbox[3] - bbox[1]) / dy)))

    return client.get_mdi(collection).process_image(
        [{'type': collection}],
        bbox_or_geom,
        crs='http://www.opengis.net/def/crs/EPSG/0/4326',
        width=width,
        height=height,
        format=format,
        evalscript=evalscript,
        time=time
    )


#
#  -------------- Routes
#

@dapa.route('/')
def root():
    # TODO: better structure
    return jsonify([
        url_for('.collection_dapa', collection=ds['id'])
        for ds in get_config_client().get_datasets()
    ])


@dapa.route('/<collection>/dapa/')
def collection_dapa(collection):
    return jsonify({
        'fields': url_for('.fields', collection=collection)
    })


@dapa.route('/<collection>/dapa/fields')
def fields(collection):
    # TODO: add more metadata
    ds = get_config_client().get_dataset(collection)
    return jsonify([
        {
            'id': band
        }
        for band in ds['bands']
    ])

@dapa.route('/<collection>/dapa/cube')
def cube(collection):
    fields, inputs = parse_fields(request.args['fields'])
    time = parse_time(request.args['time'])

    if 'bbox' in request.args:
        bbox_or_geom = parse_bbox(request.args['bbox'])
        bbox = bbox_or_geom
    elif 'geom' in request.args:
        geometry = ogr.CreateGeometryFromWkt(request.args['geom'])
        bbox_or_geom = json.loads(geometry.ExportToJson())
        bbox = geometry.GetEnvelope()
    else:
        raise NotImplementedError('Either bbox or geom is required')

    client = get_config_client()
    catalog_client = client.get_catalog_client(collection)
    ds = client.get_dataset(collection)
    dx, dy = ds['resolution']

    filename = f'{tempfile.gettempdir()}/{uuid4().hex}.nc'

    rootgrp = netCDF4.Dataset(filename, 'w', format='NETCDF4')
    fieldnames = [name for name, _ in fields]

    width = min(512, int(abs((bbox[2] - bbox[0]) / dx)))
    height = min(512, int(abs((bbox[3] - bbox[1]) / dy)))

    # create dimensions
    rootgrp.createDimension("time", None)
    rootgrp.createDimension("x", width)
    rootgrp.createDimension("y", height)

    v_time = rootgrp.createVariable("time", "f8", ("time",))
    v_x = rootgrp.createVariable("x", "f4", ("x",))
    v_y = rootgrp.createVariable("y", "f4", ("y",))

    v_time.units = "hours since 0001-01-01 00:00:00.0"
    v_time.calendar = "gregorian"

    variables = [
        rootgrp.createVariable(name, "f4", ("time", "y", "x"))
        for name in fieldnames
    ]

    v_x[:] = np.linspace(bbox[0], bbox[2], width)
    v_y[:] = np.linspace(bbox[3], bbox[1], height)

    # iterate over all slices
    for i, raw_time in enumerate(search_times(ds, catalog_client, bbox_or_geom, time)):
        item_time = parse_iso8601(raw_time)
        response = get_area_aggregate_time(
            collection, fields, inputs, None,
            [item_time - timedelta(minutes=30), item_time + timedelta(minutes=30)],
            bbox_or_geom, bbox,
            width, height,
        )

        v_time[i] = netCDF4.date2num(
            item_time, units=v_time.units, calendar=v_time.calendar
        )

        with TemporaryVSIFile.from_buffer(response) as f:
            ds = gdal.Open(f.name)

            arrays = ds.ReadAsArray()
            if len(arrays.shape) == 2:
                arrays = arrays.reshape(1, *arrays.shape)

            for var, values in zip(variables, arrays):
                var[i, :, :] = values

    rootgrp.close()

    # add handler to delete the file after sending it
    @after_this_request
    def delete_file(response):
        os.remove(filename)
        return response

    return send_file(filename, mimetype='application/x-netcdf')


@dapa.route('/<collection>/dapa/area')
def area(collection):
    client = get_config_client()
    ds = client.get_dataset(collection)

    timeextent = ds.get('timeextent')

    # parse inputs
    fields, inputs = parse_fields(request.args['fields'])

    if timeextent:
        aggregates = parse_aggregates(request.args['aggregate'])
        time = parse_time(request.args['time'])
    else:
        aggregates = None
        time = None

    if 'bbox' in request.args:
        bbox_or_geom = parse_bbox(request.args['bbox'])
        bbox = bbox_or_geom
    elif 'geom' in request.args:
        geometry = ogr.CreateGeometryFromWkt(request.args['geom'])
        bbox_or_geom = json.loads(geometry.ExportToJson())
        bbox = geometry.GetEnvelope()
    else:
        raise NotImplementedError('Either bbox or geom is required')

    response = get_area_aggregate_time(
        collection, fields, inputs, aggregates, time, bbox_or_geom, bbox
    )

    # open with GDAL to set the band names
    with TemporaryVSIFile.from_buffer(response) as f:
        ds = gdal.Open(f.name, gdal.GA_Update)

        if aggregates:
            names = [f'{name}_{agg}' for name, _ in fields for agg in aggregates]
        else:
            names = [name for name, _ in fields]

        for i, name in enumerate(names, start=1):
            band = ds.GetRasterBand(i)
            band.SetDescription(name)
        ds.FlushCache()
        ds = None

        # rewind and re-read
        f.seek(0)
        response = f.read()

    return Response(response, mimetype='image/tiff')


NUMPY_AGG_METHODS = {
    'min': np.nanmin,
    'max': np.nanmax,
    'avg': np.nanmean,
    'stdev': np.nanstd,
}


@dapa.route('/<collection>/dapa/timeseries/area')
def timeseries_area(collection):
    client = get_config_client()
    ds = client.get_dataset(collection)
    timeextent = ds.get('timeextent')
    if not timeextent:
        raise Exception('This collection does not support timeseries extraction')

    # parse inputs
    fields, inputs = parse_fields(request.args['fields'])
    aggregates = parse_aggregates(request.args['aggregate'])
    time = parse_time(request.args['time'])

    if 'bbox' in request.args:
        bbox_or_geom = parse_bbox(request.args['bbox'])
        bbox = bbox_or_geom
    elif 'geom' in request.args:
        geometry = ogr.CreateGeometryFromWkt(request.args['geom'])
        bbox_or_geom = json.loads(geometry.ExportToJson())
        bbox = geometry.GetEnvelope()
    else:
        raise NotImplementedError('Either bbox or geom is required')

    catalog_client = client.get_catalog_client(collection)

    tmp = io.StringIO()
    writer = csv.writer(tmp)
    writer.writerow(['datetime'] + [f'{name}_{agg}' for name, _ in fields for agg in aggregates])

    for raw_time in search_times(ds, catalog_client, bbox_or_geom, time):
        item_time = parse_iso8601(raw_time)
        response = get_area_aggregate_time(
            collection, fields, inputs, None,
            [item_time - timedelta(minutes=30), item_time + timedelta(minutes=30)],
            bbox_or_geom, bbox,
        )

        # TIFF reading here is necessary
        with TemporaryVSIFile.from_buffer(response) as f:
            ds = gdal.Open(f.name)
            arrays = ds.ReadAsArray()
            if len(arrays.shape) == 2:
                arrays = arrays.reshape(1, *arrays.shape)

            writer.writerow(
                [raw_time] + [
                    str(NUMPY_AGG_METHODS[agg](array))
                    for array in arrays for agg in aggregates
                ]
            )
            del ds

    return Response(tmp.getvalue(), mimetype='text/csv')


@dapa.route('/<collection>/dapa/timeseries/position')
def timeseries_position(collection):
    client = get_config_client()
    ds = client.get_dataset(collection)
    timeextent = ds.get('timeextent')
    if not timeextent:
        raise Exception('This collection does not support timeseries extraction')

    fields, inputs = parse_fields(request.args['fields'])
    time = parse_time(request.args['time'])
    point = parse_point(request.args['point'])

    catalog_client = client.get_catalog_client(collection)

    dx, dy = [abs(v) for v in ds['resolution']]
    bbox = [
        point[0] - dx / 2,
        point[1] - dy / 2,
        point[0] + dx / 2,
        point[1] + dy / 2,
    ]

    tmp = io.StringIO()
    writer = csv.writer(tmp)
    writer.writerow(['datetime'] + [name for name, _ in fields])
    for raw_time in search_times(ds, catalog_client, bbox, time):
        item_time = parse_iso8601(raw_time)
        response = get_area_aggregate_time(
            collection, fields, inputs, None,
            [item_time - timedelta(minutes=30), item_time + timedelta(minutes=30)],
            bbox, bbox,
            width=1, height=1, format='image/tiff'
        )

        # TIFF reading here is necessary
        with TemporaryVSIFile.from_buffer(response) as f:
            ds = gdal.Open(f.name)
            writer.writerow(
                [raw_time] + [str(v) for v in ds.ReadAsArray().flatten()]
            )
            del ds

    return Response(tmp.getvalue(), mimetype='text/csv')


@dapa.route('/<collection>/dapa/value/area')
def value_area(collection):
    client = get_config_client()
    ds = client.get_dataset(collection)

    timeextent = ds.get('timeextent')

    # parse inputs
    fields, inputs = parse_fields(request.args['fields'])
    aggregates = parse_aggregates(request.args['aggregate'])

    if timeextent:
        time = parse_time(request.args['time'])
    else:
        time = None

    if 'bbox' in request.args:
        bbox_or_geom = parse_bbox(request.args['bbox'])
        bbox = bbox_or_geom
    elif 'geom' in request.args:
        geometry = ogr.CreateGeometryFromWkt(request.args['geom'])
        bbox_or_geom = json.loads(geometry.ExportToJson())
        bbox = geometry.GetEnvelope()
    else:
        raise NotImplementedError('Either bbox or geom is required')

    if timeextent:
        response = get_area_aggregate_time(
            collection, fields, inputs, aggregates, time, bbox_or_geom, bbox
        )
    else:
        response = get_area_aggregate_time(
            collection, fields, inputs, None, time, bbox_or_geom, bbox
        )

    with TemporaryVSIFile.from_buffer(response) as f:
        ds = gdal.Open(f.name)
        arrays = ds.ReadAsArray()
        if len(arrays.shape) == 2:
            arrays = arrays.reshape(1, *arrays.shape)

        if timeextent:
            values = ','.join(str(v) for v in np.nanmean(arrays, (1, 2)))
        else:
            values = ','.join([
                str(NUMPY_AGG_METHODS[agg](array))
                for array in arrays for agg in aggregates
            ])
        del ds

    return Response(values, mimetype='text/plain')


@dapa.route('/<collection>/dapa/value/position')
def value_position(collection):
    client = get_config_client()
    ds = client.get_dataset(collection)

    timeextent = ds.get('timeextent')

    # parse inputs
    fields, inputs = parse_fields(request.args['fields'])
    point = parse_point(request.args['point'])
    if timeextent:
        time = parse_time(request.args['time'])
        aggregates = parse_aggregates(request.args['aggregate'])
    else:
        time = None
        aggregates = None

    dx, dy = [abs(v) for v in ds['resolution']]
    bbox = [
        point[0] - dx / 2,
        point[1] - dy / 2,
        point[0] + dx / 2,
        point[1] + dy / 2,
    ]

    response = get_area_aggregate_time(
        collection, fields, inputs, aggregates, time, bbox, bbox,
        width=1, height=1, format='image/tiff'
    )

    # TIFF reading here is necessary
    with TemporaryVSIFile.from_buffer(response) as f:
        ds = gdal.Open(f.name)
        values = ','.join(str(v) for v in ds.ReadAsArray().flatten())
        del ds

    return Response(values, mimetype='text/plain')
