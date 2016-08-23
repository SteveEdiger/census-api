# For real division instead of sometimes-integer
from __future__ import division

from flask import Flask
from flask import abort, request, g
from flask import make_response, current_app, send_file, url_for
from flask import jsonify, redirect
from flask.ext.sqlalchemy import SQLAlchemy
from raven.contrib.flask import Sentry
from werkzeug.exceptions import HTTPException
from functools import update_wrapper
from itertools import groupby
import simplejson as json
from collections import OrderedDict
import decimal
import operator
import math
from math import log10, log
from datetime import timedelta
import re
import os
import shutil
import tempfile
import zipfile
import pylibmc
import mockcache
from boto.s3.connection import S3Connection
from boto.s3.key import Key
from boto.exception import S3ResponseError
from validation import qwarg_validate, NonemptyString, FloatRange, StringList, Bool, OneOf, Integer, ClientRequestValidationException

from census_extractomatic.exporters import create_ogr_download, create_excel_download, supported_formats

app = Flask(__name__)
app.config.from_object(os.environ.get('EXTRACTOMATIC_CONFIG_MODULE', 'census_extractomatic.config.Development'))
db = SQLAlchemy(app)
sentry = Sentry(app)

if not app.debug:
    import logging
    file_handler = logging.FileHandler('/tmp/api.censusreporter.org.wsgi_error.log')
    file_handler.setLevel(logging.WARNING)
    app.logger.addHandler(file_handler)

try:
    app.s3 = S3Connection()
except Exception, e:
    app.s3 = None
    app.logger.warning("S3 Configuration failed.")

# Allowed ACS's in "best" order (newest and smallest range preferred)
allowed_acs = [
    'acs2014_1yr',
    'acs2014_5yr',
]
# When expanding a container geoid shorthand (i.e. 140|05000US12127),
# use this ACS. It should always be a 5yr release so as to include as
# many geos as possible.
release_to_expand_with = allowed_acs[1]
# When table searches happen without a specified release, use this
# release to do the table search.
default_table_search_release = allowed_acs[1]

# Allowed TIGER releases in newest order
allowed_tiger = [
    'tiger2014',
    'tiger2013',
]

allowed_searches = [
    'table',
    'profile',
    'topic',
    'all'
]

ACS_NAMES = {
    'acs2014_1yr': {'name': 'ACS 2014 1-year', 'years': '2014'},
    'acs2014_5yr': {'name': 'ACS 2014 5-year', 'years': '2010-2014'},
    'acs2013_3yr': {'name': 'ACS 2013 3-year', 'years': '2011-2013'},
}

PARENT_CHILD_CONTAINMENT = {
    '040': ['050', '060', '101', '140', '150', '160', '500', '610', '620', '950', '960', '970'],
    '050': ['060', '101', '140', '150'],
    '140': ['101', '150'],
    '150': ['101'],
}

SUMLEV_NAMES = {
    "010": {"name": "nation", "plural": ""},
    "020": {"name": "region", "plural": "regions"},
    "030": {"name": "division", "plural": "divisions"},
    "040": {"name": "state", "plural": "states", "tiger_table": "state"},
    "050": {"name": "county", "plural": "counties", "tiger_table": "county"},
    "060": {"name": "county subdivision", "plural": "county subdivisions", "tiger_table": "cousub"},
    "101": {"name": "block", "plural": "blocks", "tiger_table": "tabblock"},
    "140": {"name": "census tract", "plural": "census tracts", "tiger_table": "tract"},
    "150": {"name": "block group", "plural": "block groups", "tiger_table": "bg"},
    "160": {"name": "place", "plural": "places", "tiger_table": "place"},
    "170": {"name": "consolidated city", "plural": "consolidated cities", "tiger_table": "concity"},
    "230": {"name": "Alaska native regional corporation", "plural": "Alaska native regional corporations", "tiger_table": "anrc"},
    "250": {"name": "native area", "plural": "native areas", "tiger_table": "aiannh250"},
    "251": {"name": "tribal subdivision", "plural": "tribal subdivisions", "tiger_table": "aits"},
    "252": {"name": "native area (reservation)", "plural": "native areas (reservation)", "tiger_table": "aiannh252"},
    "254": {"name": "native area (off-trust land)", "plural": "native areas (off-trust land)", "tiger_table": "aiannh254"},
    "256": {"name": "tribal census tract", "plural": "tribal census tracts", "tiger_table": "ttract"},
    "300": {"name": "MSA", "plural": "MSAs", "tiger_table": "metdiv"},
    "310": {"name": "CBSA", "plural": "CBSAs", "tiger_table": "cbsa"},
    "314": {"name": "metropolitan division", "plural": "metropolitan divisions", "tiger_table": "metdiv"},
    "330": {"name": "CSA", "plural": "CSAs", "tiger_table": "csa"},
    "335": {"name": "combined NECTA", "plural": "combined NECTAs", "tiger_table": "cnecta"},
    "350": {"name": "NECTA", "plural": "NECTAs", "tiger_table": "necta"},
    "364": {"name": "NECTA division", "plural": "NECTA divisions", "tiger_table": "nectadiv"},
    "400": {"name": "urban area", "plural": "urban areas", "tiger_table": "uac"},
    "500": {"name": "congressional district", "plural": "congressional districts", "tiger_table": "cd"},
    "610": {"name": "state senate district", "plural": "state senate districts", "tiger_table": "sldu"},
    "620": {"name": "state house district", "plural": "state house districts", "tiger_table": "sldl"},
    "795": {"name": "PUMA", "plural": "PUMAs", "tiger_table": "puma"},
    "850": {"name": "ZCTA3", "plural": "ZCTA3s"},
    "860": {"name": "ZCTA5", "plural": "ZCTA5s", "tiger_table": "zcta5"},
    "950": {"name": "elementary school district", "plural": "elementary school districts", "tiger_table": "elsd"},
    "960": {"name": "secondary school district", "plural": "secondary school districts", "tiger_table": "scsd"},
    "970": {"name": "unified school district", "plural": "unified school districts", "tiger_table": "unsd"},
}

state_fips = {
    "01": "Alabama",
    "02": "Alaska",
    "04": "Arizona",
    "05": "Arkansas",
    "06": "California",
    "08": "Colorado",
    "09": "Connecticut",
    "10": "Delaware",
    "11": "District of Columbia",
    "12": "Florida",
    "13": "Georgia",
    "15": "Hawaii",
    "16": "Idaho",
    "17": "Illinois",
    "18": "Indiana",
    "19": "Iowa",
    "20": "Kansas",
    "21": "Kentucky",
    "22": "Louisiana",
    "23": "Maine",
    "24": "Maryland",
    "25": "Massachusetts",
    "26": "Michigan",
    "27": "Minnesota",
    "28": "Mississippi",
    "29": "Missouri",
    "30": "Montana",
    "31": "Nebraska",
    "32": "Nevada",
    "33": "New Hampshire",
    "34": "New Jersey",
    "35": "New Mexico",
    "36": "New York",
    "37": "North Carolina",
    "38": "North Dakota",
    "39": "Ohio",
    "40": "Oklahoma",
    "41": "Oregon",
    "42": "Pennsylvania",
    "44": "Rhode Island",
    "45": "South Carolina",
    "46": "South Dakota",
    "47": "Tennessee",
    "48": "Texas",
    "49": "Utah",
    "50": "Vermont",
    "51": "Virginia",
    "53": "Washington",
    "54": "West Virginia",
    "55": "Wisconsin",
    "56": "Wyoming",
    "60": "American Samoa",
    "66": "Guam",
    "69": "Commonwealth of the Northern Mariana Islands",
    "72": "Puerto Rico",
    "78": "United States Virgin Islands"
}

def get_from_cache(cache_key, try_s3=True):
    # Try memcache first
    cached = g.cache.get(cache_key)

    if not cached and try_s3 and current_app.s3 is not None:
        # Try S3 next
        b = current_app.s3.get_bucket('embed.censusreporter.org', validate=False)
        k = Key(b)
        k.key = cache_key
        try:
            cached = k.get_contents_as_string()
        except S3ResponseError:
            cached = None

        # TODO Should stick the S3 thing back in memcache

    return cached


def put_in_cache(cache_key, value, memcache=True, try_s3=True, content_type='application/json', ):
    if memcache:
        g.cache.set(cache_key, value)

    if try_s3 and current_app.s3 is not None:
        b = current_app.s3.get_bucket('embed.censusreporter.org', validate=False)
        k = Key(b, cache_key)
        k.metadata['Content-Type'] = content_type
        k.set_contents_from_string(value, reduced_redundancy=True, policy='public-read')


def crossdomain(origin=None, methods=None, headers=None,
                max_age=21600, attach_to_all=True,
                automatic_options=True):
    if methods is not None:
        methods = ', '.join(sorted(x.upper() for x in methods))
    if headers is not None and not isinstance(headers, basestring):
        headers = ', '.join(x.upper() for x in headers)
    if not isinstance(origin, basestring):
        origin = ', '.join(origin)
    if isinstance(max_age, timedelta):
        max_age = max_age.total_seconds()

    def get_methods():
        if methods is not None:
            return methods

        options_resp = current_app.make_default_options_response()
        return options_resp.headers['allow']

    def decorator(f):
        def wrapped_function(*args, **kwargs):
            if automatic_options and request.method == 'OPTIONS':
                resp = current_app.make_default_options_response()
            else:
                resp = make_response(f(*args, **kwargs))
            if not attach_to_all and request.method != 'OPTIONS':
                return resp

            h = resp.headers

            h['Access-Control-Allow-Origin'] = origin
            h['Access-Control-Allow-Methods'] = get_methods()
            h['Access-Control-Max-Age'] = str(max_age)
            if headers is not None:
                h['Access-Control-Allow-Headers'] = headers
            return resp

        f.provide_automatic_options = False
        f.required_methods = ['OPTIONS']
        return update_wrapper(wrapped_function, f)
    return decorator


@app.errorhandler(400)
@app.errorhandler(500)
@crossdomain(origin='*')
def jsonify_error_handler(error):
    if isinstance(error, ClientRequestValidationException):
        resp = jsonify(error=error.description, errors=error.errors)
        resp.status_code = error.code
    elif isinstance(error, HTTPException):
        resp = jsonify(error=error.description)
        resp.status_code = error.code
    else:
        resp = jsonify(error=error.message)
        resp.status_code = 500
    app.logger.exception("Handling exception %s, %s", error, error.message)
    return resp


def maybe_int(i):
    return int(i) if i else i


def percentify(val):
    return val * 100


def rateify(val):
    return val * 1000


def moe_add(moe_a, moe_b):
    # From http://www.census.gov/acs/www/Downloads/handbooks/ACSGeneralHandbook.pdf
    return math.sqrt(moe_a**2 + moe_b**2)


def moe_ratio(numerator, denominator, numerator_moe, denominator_moe):
    # From http://www.census.gov/acs/www/Downloads/handbooks/ACSGeneralHandbook.pdf
    estimated_ratio = numerator / denominator
    return math.sqrt(numerator_moe**2 + (estimated_ratio**2 * denominator_moe**2)) / denominator


ops = {
    '+': operator.add,
    '-': operator.sub,
    '/': operator.div,
    '%': percentify,
    '%%': rateify,
}
moe_ops = {
    '+': moe_add,
    '-': moe_add,
    '/': moe_ratio,
    '%': percentify,
    '%%': rateify,
}


def value_rpn_calc(data, rpn_string):
    stack = []
    moe_stack = []
    numerator = None
    numerator_moe = None

    for token in rpn_string.split():
        if token in ops:
            b = stack.pop()
            b_moe = moe_stack.pop()

            if token in ('%', '%%'):
                # Single-argument operators
                if b is None:
                    c = None
                    c_moe = None
                else:
                    c = ops[token](b)
                    c_moe = moe_ops[token](b_moe)
            else:
                a = stack.pop()
                a_moe = moe_stack.pop()

                if a is None or b is None:
                    c = None
                    c_moe = None
                elif token == '/':
                    # Broken out because MOE ratio needs both MOE and estimates

                    # We're dealing with ratios, not pure division.
                    if a == 0 or b == 0:
                        c = 0
                        c_moe = 0
                    else:
                        c = ops[token](a, b)
                        c_moe = moe_ratio(a, b, a_moe, b_moe)
                    numerator = a
                    numerator_moe = round(a_moe, 1)
                else:
                    c = ops[token](a, b)
                    c_moe = moe_ops[token](a_moe, b_moe)
        elif token.startswith('b'):
            c = data[token]
            c_moe = data[token + '_moe']
        else:
            c = float(token)
            c_moe = float(token)
        stack.append(c)
        moe_stack.append(c_moe)

    value = stack.pop()
    error = moe_stack.pop()

    return (value, error, numerator, numerator_moe)


def build_item(name, data, parents, rpn_string):
    val = OrderedDict([('name', name),
        ('values', dict()),
        ('error', dict()),
        ('numerators', dict()),
        ('numerator_errors', dict())])

    for parent in parents:
        label = parent['relation']
        geoid = parent['geoid']
        data_for_geoid = data.get(geoid) if data else {}

        value = None
        error = None
        numerator = None
        numerator_moe = None

        if data_for_geoid:
            (value, error, numerator, numerator_moe) = value_rpn_calc(data_for_geoid, rpn_string)

        # provide 2 decimals of precision, let client decide how much to use
        if value is not None:
            value = round(value, 2)
            error = round(error, 2)

        if numerator is not None:
            numerator = round(numerator, 2)
            numerator_moe = round(numerator_moe, 2)

        val['values'][label] = value
        val['error'][label] = error
        val['numerators'][label] = numerator
        val['numerator_errors'][label] = numerator_moe

    return val


def add_metadata(dictionary, table_id, universe, acs_release):
    val = dict(
        table_id=table_id,
        universe=universe,
        acs_release=acs_release,
    )

    dictionary['metadata'] = val


def find_geoid(geoid, acs=None):
    "Find the best acs to use for a given geoid or None if the geoid is not found."

    if acs:
        if acs not in allowed_acs:
            abort(404, "We don't have data for that release.")
        acs_to_search = [acs]
    else:
        acs_to_search = allowed_acs

    for acs in acs_to_search:

        result = db.session.execute(
            """SELECT geoid
               FROM %s.geoheader
               WHERE geoid=:geoid""" % acs,
            {'geoid': geoid}
        )
        if result.rowcount == 1:
            result = result.first()
            return (acs, result['geoid'])
    return (None, None)


@app.before_request
def before_request():
    memcache_addr = app.config.get('MEMCACHE_ADDR')
    g.cache = pylibmc.Client(memcache_addr) if memcache_addr else mockcache.Client(memcache_addr)


def get_data_fallback(table_ids, geoids, acs=None):
    if type(geoids) != list:
        geoids = [geoids]

    if type(table_ids) != list:
        table_ids = [table_ids]

    from_stmt = '%%(acs)s.%s_moe' % (table_ids[0])
    if len(table_ids) > 1:
        from_stmt += ' '
        from_stmt += ' '.join(['JOIN %%(acs)s.%s_moe USING (geoid)' % (table_id) for table_id in table_ids[1:]])

    sql = 'SELECT * FROM %s WHERE geoid IN :geoids;' % (from_stmt,)

    # if acs is specified, we'll use that one and not go searching for data.
    if acs in allowed_acs:
        sql = sql % {'acs': acs}
        result = db.session.execute(
            sql,
            {'geoids': tuple(geoids)},
        )
        data = {}
        for row in result.fetchall():
            row = dict(row)
            geoid = row.pop('geoid')
            data[geoid] = dict([(col, val) for (col, val) in row.iteritems()])

        return data, acs

    else:
        # otherwise we'll start at the best/most recent acs and move down til we have the data we want
        for acs in allowed_acs:
            sql = sql % {'acs': acs}
            result = db.session.execute(
                sql,
                {'geoids': tuple(geoids)},
            )
            data = {}
            for row in result.fetchall():
                row = dict(row)
                geoid = row.pop('geoid')
                data[geoid] = dict([(col, val) for (col, val) in row.iteritems()])

            # Check to see if this release has our data
            data_with_values = filter(lambda geoid_data: geoid_data.values()[0] is not None, data.values())
            if len(geoids) == len(data) and len(geoids) == len(data_with_values):
                return data, acs
            else:
                # Doesn't contain data for all geoids, so keep going.
                continue

    return None, acs

def special_case_parents(geoid, levels):
    '''
    Update/adjust the parents list for special-cased geographies.
    '''
    if geoid == '16000US1150000':
        # compare Washington, D.C., to "parent" state of VA,
        # rather than comparing to self as own parent state

        target = (index for (index, d) in enumerate(levels) if d['geoid'] == '04000US11').next()
        levels[target].update({
            'coverage': 0,
            'display_name': 'Virginia',
            'geoid': '04000US51'
        })

    return levels

def compute_profile_item_levels(geoid):
    levels = []
    geoid_parts = []

    if geoid:
        geoid = geoid.upper()
        geoid_parts = geoid.split('US')

    if len(geoid_parts) is not 2:
        raise Exception('Invalid geoid')

    levels.append({
        'relation': 'this',
        'geoid': geoid,
        'coverage': 100.0,
    })

    sumlevel = geoid_parts[0][:3]
    id_part = geoid_parts[1]

    if sumlevel in ('140', '150', '160', '310', '330', '350', '860', '950', '960', '970'):
        result = db.session.execute(
            """SELECT * FROM tiger2014.census_geo_containment
               WHERE child_geoid=:geoid
               ORDER BY percent_covered ASC
            """,
            {'geoid': geoid},
        )
        for row in result:
            parent_sumlevel_name = SUMLEV_NAMES.get(row['parent_geoid'][:3])['name']

            levels.append({
                'relation': parent_sumlevel_name,
                'geoid': row['parent_geoid'],
                'coverage': row['percent_covered'],
            })

    if sumlevel in ('060', '140', '150'):
        levels.append({
            'relation': 'county',
            'geoid': '05000US' + id_part[:5],
            'coverage': 100.0,
        })

    if sumlevel in ('050', '060', '140', '150', '160', '500', '610', '620', '795', '950', '960', '970'):
        levels.append({
            'relation': 'state',
            'geoid': '04000US' + id_part[:2],
            'coverage': 100.0,
        })

    if sumlevel != '010':
        levels.append({
            'relation': 'nation',
            'geoid': '01000US',
            'coverage': 100.0,
        })

    levels = special_case_parents(geoid, levels)

    return levels


def geo_profile(acs, geoid):
    acs_default = acs

    item_levels = compute_profile_item_levels(geoid)
    comparison_geoids = [level['geoid'] for level in item_levels]

    doc = OrderedDict([('geography', OrderedDict()),
                       ('demographics', dict()),
                       ('economics', dict()),
                       ('families', dict()),
                       ('housing', dict()),
                       ('social', dict())])

    # Demographics: Age
    # multiple data points, suitable for visualization
    data, acs = get_data_fallback('B01001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')
    doc['geography']['census_release'] = acs_name

    result = db.session.execute(
        """SELECT DISTINCT full_geoid,sumlevel,display_name,simple_name,aland
           FROM tiger2014.census_name_lookup
           WHERE full_geoid IN :geoids;""",
        {'geoids': tuple(comparison_geoids)}
    )

    def convert_geography_data(row):
        return dict(full_name=row['display_name'],
                    short_name=row['simple_name'],
                    sumlevel=row['sumlevel'],
                    land_area=row['aland'],
                    full_geoid=row['full_geoid'])

    lookup_data = {}
    doc['geography']['parents'] = OrderedDict()
    for row in result:
        lookup_data[row['full_geoid']] = row

    for item_level in item_levels:
        name = item_level['relation']
        the_geoid = item_level['geoid']
        if name == 'this':
            doc['geography'][name] = convert_geography_data(lookup_data[the_geoid])
            doc['geography'][name]['total_population'] = maybe_int(data[the_geoid]['b01001001'])
        else:
            doc['geography']['parents'][name] = convert_geography_data(lookup_data[the_geoid])
            doc['geography']['parents'][name]['total_population'] = maybe_int(data[the_geoid]['b01001001'])

    age_dict = dict()
    doc['demographics']['age'] = age_dict

    cat_dict = OrderedDict()
    age_dict['distribution_by_category'] = cat_dict
    add_metadata(age_dict['distribution_by_category'], 'b01001', 'Total population', acs_name)

    cat_dict['percent_under_18'] = build_item('Under 18', data, item_levels,
        'b01001003 b01001004 + b01001005 + b01001006 + b01001027 + b01001028 + b01001029 + b01001030 + b01001001 / %')
    cat_dict['percent_18_to_64'] = build_item('18 to 64', data, item_levels,
        'b01001007 b01001008 + b01001009 + b01001010 + b01001011 + b01001012 + b01001013 + b01001014 + b01001015 + b01001016 + b01001017 + b01001018 + b01001019 + b01001031 + b01001032 + b01001033 + b01001034 + b01001035 + b01001036 + b01001037 + b01001038 + b01001039 + b01001040 + b01001041 + b01001042 + b01001043 + b01001001 / %')
    cat_dict['percent_over_65'] = build_item('65 and over', data, item_levels,
        'b01001020 b01001021 + b01001022 + b01001023 + b01001024 + b01001025 + b01001044 + b01001045 + b01001046 + b01001047 + b01001048 + b01001049 + b01001001 / %')

    pop_dict = dict()
    age_dict['distribution_by_decade'] = pop_dict
    population_by_age_total = OrderedDict()
    population_by_age_male = OrderedDict()
    population_by_age_female = OrderedDict()
    pop_dict['total'] = population_by_age_total
    add_metadata(pop_dict['total'], 'b01001', 'Total population', acs_name)
    pop_dict['male'] = population_by_age_male
    add_metadata(pop_dict['male'], 'b01001', 'Total population', acs_name)
    pop_dict['female'] = population_by_age_female
    add_metadata(pop_dict['female'], 'b01001', 'Total population', acs_name)

    population_by_age_male['0-9'] = build_item('0-9', data, item_levels,
        'b01001003 b01001004 + b01001002 / %')
    population_by_age_female['0-9'] = build_item('0-9', data, item_levels,
        'b01001027 b01001028 + b01001026 / %')
    population_by_age_total['0-9'] = build_item('0-9', data, item_levels,
        'b01001003 b01001004 + b01001027 + b01001028 + b01001001 / %')

    population_by_age_male['10-19'] = build_item('10-19', data, item_levels,
        'b01001005 b01001006 + b01001007 + b01001002 / %')
    population_by_age_female['10-19'] = build_item('10-19', data, item_levels,
        'b01001029 b01001030 + b01001031 + b01001026 / %')
    population_by_age_total['10-19'] = build_item('10-19', data, item_levels,
        'b01001005 b01001006 + b01001007 + b01001029 + b01001030 + b01001031 + b01001001 / %')

    population_by_age_male['20-29'] = build_item('20-29', data, item_levels,
        'b01001008 b01001009 + b01001010 + b01001011 + b01001002 / %')
    population_by_age_female['20-29'] = build_item('20-29', data, item_levels,
        'b01001032 b01001033 + b01001034 + b01001035 + b01001026 / %')
    population_by_age_total['20-29'] = build_item('20-29', data, item_levels,
        'b01001008 b01001009 + b01001010 + b01001011 + b01001032 + b01001033 + b01001034 + b01001035 + b01001001 / %')

    population_by_age_male['30-39'] = build_item('30-39', data, item_levels,
        'b01001012 b01001013 + b01001002 / %')
    population_by_age_female['30-39'] = build_item('30-39', data, item_levels,
        'b01001036 b01001037 + b01001026 / %')
    population_by_age_total['30-39'] = build_item('30-39', data, item_levels,
        'b01001012 b01001013 + b01001036 + b01001037 + b01001001 / %')

    population_by_age_male['40-49'] = build_item('40-49', data, item_levels,
        'b01001014 b01001015 + b01001002 / %')
    population_by_age_female['40-49'] = build_item('40-49', data, item_levels,
        'b01001038 b01001039 + b01001026 / %')
    population_by_age_total['40-49'] = build_item('40-49', data, item_levels,
        'b01001014 b01001015 + b01001038 + b01001039 + b01001001 / %')

    population_by_age_male['50-59'] = build_item('50-59', data, item_levels,
        'b01001016 b01001017 + b01001002 / %')
    population_by_age_female['50-59'] = build_item('50-59', data, item_levels,
        'b01001040 b01001041 + b01001026 / %')
    population_by_age_total['50-59'] = build_item('50-59', data, item_levels,
        'b01001016 b01001017 + b01001040 + b01001041 + b01001001 / %')

    population_by_age_male['60-69'] = build_item('60-69', data, item_levels,
        'b01001018 b01001019 + b01001020 + b01001021 + b01001002 / %')
    population_by_age_female['60-69'] = build_item('60-69', data, item_levels,
        'b01001042 b01001043 + b01001044 + b01001045 + b01001026 / %')
    population_by_age_total['60-69'] = build_item('60-69', data, item_levels,
        'b01001018 b01001019 + b01001020 + b01001021 + b01001042 + b01001043 + b01001044 + b01001045 + b01001001 / %')

    population_by_age_male['70-79'] = build_item('70-79', data, item_levels,
        'b01001022 b01001023 + b01001002 / %')
    population_by_age_female['70-79'] = build_item('70-79', data, item_levels,
        'b01001046 b01001047 + b01001026 / %')
    population_by_age_total['70-79'] = build_item('70-79', data, item_levels,
        'b01001022 b01001023 + b01001046 + b01001047 + b01001001 / %')

    population_by_age_male['80+'] = build_item('80+', data, item_levels,
        'b01001024 b01001025 + b01001002 / %')
    population_by_age_female['80+'] = build_item('80+', data, item_levels,
        'b01001048 b01001049 + b01001026 / %')
    population_by_age_total['80+'] = build_item('80+', data, item_levels,
        'b01001024 b01001025 + b01001048 + b01001049 + b01001001 / %')

    # Demographics: Sex
    # multiple data points, suitable for visualization
    sex_dict = OrderedDict()
    doc['demographics']['sex'] = sex_dict
    add_metadata(sex_dict, 'b01001', 'Total population', acs_name)
    sex_dict['percent_male'] = build_item('Male', data, item_levels,
        'b01001002 b01001001 / %')
    sex_dict['percent_female'] = build_item('Female', data, item_levels,
        'b01001026 b01001001 / %')

    data, acs = get_data_fallback('B01002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    median_age_dict = dict()
    age_dict['median_age'] = median_age_dict
    median_age_dict['total'] = build_item('Median age', data, item_levels,
        'b01002001')
    add_metadata(median_age_dict['total'], 'b01001', 'Total population', acs_name)
    median_age_dict['male'] = build_item('Median age male', data, item_levels,
        'b01002002')
    add_metadata(median_age_dict['male'], 'b01001', 'Total population', acs_name)
    median_age_dict['female'] = build_item('Median age female', data, item_levels,
        'b01002003')
    add_metadata(median_age_dict['female'], 'b01001', 'Total population', acs_name)

    # Demographics: Race
    # multiple data points, suitable for visualization
    # uses Table B03002 (HISPANIC OR LATINO ORIGIN BY RACE), pulling race numbers from "Not Hispanic or Latino" columns
    # also collapses smaller groups into "Other"
    data, acs = get_data_fallback('B03002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    race_dict = OrderedDict()
    doc['demographics']['race'] = race_dict
    add_metadata(race_dict, 'b03002', 'Total population', acs_name)

    race_dict['percent_white'] = build_item('White', data, item_levels,
        'b03002003 b03002001 / %')

    race_dict['percent_black'] = build_item('Black', data, item_levels,
        'b03002004 b03002001 / %')

    race_dict['percent_native'] = build_item('Native', data, item_levels,
        'b03002005 b03002001 / %')

    race_dict['percent_asian'] = build_item('Asian', data, item_levels,
        'b03002006 b03002001 / %')

    race_dict['percent_islander'] = build_item('Islander', data, item_levels,
        'b03002007 b03002001 / %')

    race_dict['percent_other'] = build_item('Other', data, item_levels,
        'b03002008 b03002001 / %')

    race_dict['percent_two_or_more'] = build_item('Two+', data, item_levels,
        'b03002009 b03002001 / %')

#    # collapsed version of "other"
#    race_dict['percent_other'] = build_item('Other', data, item_levels,
#        'b03002005 b03002007 + b03002008 + b03002009 + b03002001 / %')

    race_dict['percent_hispanic'] = build_item('Hispanic', data, item_levels,
        'b03002012 b03002001 / %')

    # Economics: Per-Capita Income
    # single data point
    data, acs = get_data_fallback('B19301', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    income_dict = dict()
    doc['economics']['income'] = income_dict

    income_dict['per_capita_income_in_the_last_12_months'] = build_item('Per capita income', data, item_levels,
        'b19301001')
    add_metadata(income_dict['per_capita_income_in_the_last_12_months'], 'b19301', 'Total population', acs_name)

    # Economics: Median Household Income
    # single data point
    data, acs = get_data_fallback('B19013', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    income_dict['median_household_income'] = build_item('Median household income', data, item_levels,
        'b19013001')
    add_metadata(income_dict['median_household_income'], 'b19013', 'Households', acs_name)

    # Economics: Household Income Distribution
    # multiple data points, suitable for visualization
    data, acs = get_data_fallback('B19001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    income_distribution = OrderedDict()
    income_dict['household_distribution'] = income_distribution
    add_metadata(income_dict['household_distribution'], 'b19001', 'Households', acs_name)

    income_distribution['under_50'] = build_item('Under $50K', data, item_levels,
        'b19001002 b19001003 + b19001004 + b19001005 + b19001006 + b19001007 + b19001008 + b19001009 + b19001010 + b19001001 / %')
    income_distribution['50_to_100'] = build_item('$50K - $100K', data, item_levels,
        'b19001011 b19001012 + b19001013 + b19001001 / %')
    income_distribution['100_to_200'] = build_item('$100K - $200K', data, item_levels,
        'b19001014 b19001015 + b19001016 + b19001001 / %')
    income_distribution['over_200'] = build_item('Over $200K', data, item_levels,
        'b19001017 b19001001 / %')

    # Economics: Poverty Rate
    # provides separate dicts for children and seniors, with multiple data points, suitable for visualization
    data, acs = get_data_fallback('B17001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    poverty_dict = dict()
    doc['economics']['poverty'] = poverty_dict

    poverty_dict['percent_below_poverty_line'] = build_item('Persons below poverty line', data, item_levels,
        'b17001002 b17001001 / %')
    add_metadata(poverty_dict['percent_below_poverty_line'], 'b17001', 'Population for whom poverty status is determined', acs_name)

    poverty_children = OrderedDict()
    poverty_seniors = OrderedDict()
    poverty_dict['children'] = poverty_children
    add_metadata(poverty_dict['children'], 'b17001', 'Population for whom poverty status is determined', acs_name)
    poverty_dict['seniors'] = poverty_seniors
    add_metadata(poverty_dict['seniors'], 'b17001', 'Population for whom poverty status is determined', acs_name)

    poverty_children['below'] = build_item('Poverty', data, item_levels,
        'b17001004 b17001005 + b17001006 + b17001007 + b17001008 + b17001009 + b17001018 + b17001019 + b17001020 + b17001021 + b17001022 + b17001023 + b17001004 b17001005 + b17001006 + b17001007 + b17001008 + b17001009 + b17001018 + b17001019 + b17001020 + b17001021 + b17001022 + b17001023 + b17001033 + b17001034 + b17001035 + b17001036 + b17001037 + b17001038 + b17001047 + b17001048 + b17001049 + b17001050 + b17001051 + b17001052 + / %')
    poverty_children['above'] = build_item('Non-poverty', data, item_levels,
        'b17001033 b17001034 + b17001035 + b17001036 + b17001037 + b17001038 + b17001047 + b17001048 + b17001049 + b17001050 + b17001051 + b17001052 + b17001004 b17001005 + b17001006 + b17001007 + b17001008 + b17001009 + b17001018 + b17001019 + b17001020 + b17001021 + b17001022 + b17001023 + b17001033 + b17001034 + b17001035 + b17001036 + b17001037 + b17001038 + b17001047 + b17001048 + b17001049 + b17001050 + b17001051 + b17001052 + / %')

    poverty_seniors['below'] = build_item('Poverty', data, item_levels,
        'b17001015 b17001016 + b17001029 + b17001030 + b17001015 b17001016 + b17001029 + b17001030 + b17001044 + b17001045 + b17001058 + b17001059 + / %')
    poverty_seniors['above'] = build_item('Non-poverty', data, item_levels,
        'b17001044 b17001045 + b17001058 + b17001059 + b17001015 b17001016 + b17001029 + b17001030 + b17001044 + b17001045 + b17001058 + b17001059 + / %')

    # Economics: Mean Travel Time to Work, Means of Transportation to Work
    # uses two different tables for calculation, so make sure they draw from same ACS release
    data, acs = get_data_fallback(['B08006', 'B08013'], comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    employment_dict = dict()
    doc['economics']['employment'] = employment_dict

    employment_dict['mean_travel_time'] = build_item('Mean travel time to work', data, item_levels,
        'b08013001 b08006001 b08006017 - /')
    add_metadata(employment_dict['mean_travel_time'], 'b08006, b08013', 'Workers 16 years and over who did not work at home', acs_name)

    data, acs = get_data_fallback('B08006', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    transportation_dict = OrderedDict()
    employment_dict['transportation_distribution'] = transportation_dict
    add_metadata(employment_dict['transportation_distribution'], 'b08006', 'Workers 16 years and over', acs_name)

    transportation_dict['drove_alone'] = build_item('Drove alone', data, item_levels,
        'b08006003 b08006001 / %')
    transportation_dict['carpooled'] = build_item('Carpooled', data, item_levels,
        'b08006004 b08006001 / %')
    transportation_dict['public_transit'] = build_item('Public transit', data, item_levels,
        'b08006008 b08006001 / %')
    transportation_dict['bicycle'] = build_item('Bicycle', data, item_levels,
        'b08006014 b08006001 / %')
    transportation_dict['walked'] = build_item('Walked', data, item_levels,
        'b08006015 b08006001 / %')
    transportation_dict['other'] = build_item('Other', data, item_levels,
        'b08006016 b08006001 / %')
    transportation_dict['worked_at_home'] = build_item('Worked at home', data, item_levels,
        'b08006017 b08006001 / %')

    # Families: Marital Status by Sex
    data, acs = get_data_fallback('B12001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    marital_status = OrderedDict()
    doc['families']['marital_status'] = marital_status
    add_metadata(marital_status, 'b12001', 'Population 15 years and over', acs_name)

    marital_status['married'] = build_item('Married', data, item_levels,
        'b12001004 b12001013 + b12001001 / %')
    marital_status['single'] = build_item('Single', data, item_levels,
        'b12001003 b12001009 + b12001010 + b12001012 + b12001018 + b12001019 + b12001001 / %')

    marital_status_grouped = OrderedDict()
    doc['families']['marital_status_grouped'] = marital_status_grouped
    add_metadata(marital_status_grouped, 'b12001', 'Population 15 years and over', acs_name)

    # repeating data temporarily to develop grouped column chart format
    marital_status_grouped['never_married'] = OrderedDict()
    marital_status_grouped['never_married']['acs_release'] = acs_name
    marital_status_grouped['never_married']['metadata'] = {
        'universe': 'Population 15 years and over',
        'table_id': 'b12001',
        'name': 'Never married'
    }
    marital_status_grouped['never_married']['male'] = build_item('Male', data, item_levels,
        'b12001003 b12001002 / %')
    marital_status_grouped['never_married']['female'] = build_item('Female', data, item_levels,
        'b12001012 b12001011 / %')

    marital_status_grouped['married'] = OrderedDict()
    marital_status_grouped['married']['acs_release'] = acs_name
    marital_status_grouped['married']['metadata'] = {
        'universe': 'Population 15 years and over',
        'table_id': 'b12001',
        'name': 'Now married'
    }
    marital_status_grouped['married']['male'] = build_item('Male', data, item_levels,
        'b12001004 b12001002 / %')
    marital_status_grouped['married']['female'] = build_item('Female', data, item_levels,
        'b12001013 b12001011 / %')

    marital_status_grouped['divorced'] = OrderedDict()
    marital_status_grouped['divorced']['acs_release'] = acs_name
    marital_status_grouped['divorced']['metadata'] = {
        'universe': 'Population 15 years and over',
        'table_id': 'b12001',
        'name': 'Divorced'
    }
    marital_status_grouped['divorced']['male'] = build_item('Male', data, item_levels,
        'b12001010 b12001002 / %')
    marital_status_grouped['divorced']['female'] = build_item('Female', data, item_levels,
        'b12001019 b12001011 / %')

    marital_status_grouped['widowed'] = OrderedDict()
    marital_status_grouped['widowed']['acs_release'] = acs_name
    marital_status_grouped['widowed']['metadata'] = {
        'universe': 'Population 15 years and over',
        'table_id': 'b12001',
        'name': 'Widowed'
    }
    marital_status_grouped['widowed']['male'] = build_item('Male', data, item_levels,
        'b12001009 b12001002 / %')
    marital_status_grouped['widowed']['female'] = build_item('Female', data, item_levels,
        'b12001018 b12001011 / %')


    # Families: Family Types with Children
    data, acs = get_data_fallback('B09002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    family_types = dict()
    doc['families']['family_types'] = family_types

    children_family_type_dict = OrderedDict()
    family_types['children'] = children_family_type_dict
    add_metadata(children_family_type_dict, 'b09002', 'Own children under 18 years', acs_name)

    children_family_type_dict['married_couple'] = build_item('Married couple', data, item_levels,
        'b09002002 b09002001 / %')
    children_family_type_dict['male_householder'] = build_item('Male householder', data, item_levels,
        'b09002009 b09002001 / %')
    children_family_type_dict['female_householder'] = build_item('Female householder', data, item_levels,
        'b09002015 b09002001 / %')

    # Families: Birth Rate by Women's Age
    data, acs = get_data_fallback('B13016', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    fertility = dict()
    doc['families']['fertility'] = fertility

    fertility['total'] = build_item('Women 15-50 who gave birth during past year', data, item_levels,
        'b13016002 b13016001 / %')
    add_metadata(fertility['total'], 'b13016', 'Women 15 to 50 years', acs_name)

    fertility_by_age_dict = OrderedDict()
    fertility['by_age'] = fertility_by_age_dict
    add_metadata(fertility['by_age'], 'b13016', 'Women 15 to 50 years', acs_name)

    fertility_by_age_dict['15_to_19'] = build_item('15-19', data, item_levels,
        'b13016003 b13016003 b13016011 + / %')
    fertility_by_age_dict['20_to_24'] = build_item('20-24', data, item_levels,
        'b13016004 b13016004 b13016012 + / %')
    fertility_by_age_dict['25_to_29'] = build_item('25-29', data, item_levels,
        'b13016005 b13016005 b13016013 + / %')
    fertility_by_age_dict['30_to_34'] = build_item('30-35', data, item_levels,
        'b13016006 b13016006 b13016014 + / %')
    fertility_by_age_dict['35_to_39'] = build_item('35-39', data, item_levels,
        'b13016007 b13016007 b13016015 + / %')
    fertility_by_age_dict['40_to_44'] = build_item('40-44', data, item_levels,
        'b13016008 b13016008 b13016016 + / %')
    fertility_by_age_dict['45_to_50'] = build_item('45-50', data, item_levels,
        'b13016009 b13016009 b13016017 + / %')

    # Families: Number of Households, Persons per Household, Household type distribution
    data, acs = get_data_fallback(['B11001', 'B11002'], comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    households_dict = dict()
    doc['families']['households'] = households_dict

    households_dict['number_of_households'] = build_item('Number of households', data, item_levels,
        'b11001001')
    add_metadata(households_dict['number_of_households'], 'b11001', 'Households', acs_name)

    households_dict['persons_per_household'] = build_item('Persons per household', data, item_levels,
        'b11002001 b11001001 /')
    add_metadata(households_dict['persons_per_household'], 'b11001,b11002', 'Households', acs_name)

    households_distribution_dict = OrderedDict()
    households_dict['distribution'] = households_distribution_dict
    add_metadata(households_dict['distribution'], 'b11001', 'Households', acs_name)

    households_distribution_dict['married_couples'] = build_item('Married couples', data, item_levels,
        'b11002003 b11002001 / %')

    households_distribution_dict['male_householder'] = build_item('Male householder', data, item_levels,
        'b11002006 b11002001 / %')

    households_distribution_dict['female_householder'] = build_item('Female householder', data, item_levels,
        'b11002009 b11002001 / %')

    households_distribution_dict['nonfamily'] = build_item('Non-family', data, item_levels,
        'b11002012 b11002001 / %')


    # Housing: Number of Housing Units, Occupancy Distribution, Vacancy Distribution
    data, acs = get_data_fallback('B25002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    units_dict = dict()
    doc['housing']['units'] = units_dict

    units_dict['number'] = build_item('Number of housing units', data, item_levels,
        'b25002001')
    add_metadata(units_dict['number'], 'b25002', 'Housing units', acs_name)

    occupancy_distribution_dict = OrderedDict()
    units_dict['occupancy_distribution'] = occupancy_distribution_dict
    add_metadata(units_dict['occupancy_distribution'], 'b25002', 'Housing units', acs_name)

    occupancy_distribution_dict['occupied'] = build_item('Occupied', data, item_levels,
        'b25002002 b25002001 / %')
    occupancy_distribution_dict['vacant'] = build_item('Vacant', data, item_levels,
        'b25002003 b25002001 / %')

    # Housing: Structure Distribution
    data, acs = get_data_fallback('B25024', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    structure_distribution_dict = OrderedDict()
    units_dict['structure_distribution'] = structure_distribution_dict
    add_metadata(units_dict['structure_distribution'], 'b25024', 'Housing units', acs_name)

    structure_distribution_dict['single_unit'] = build_item('Single unit', data, item_levels,
        'b25024002 b25024003 + b25024001 / %')
    structure_distribution_dict['multi_unit'] = build_item('Multi-unit', data, item_levels,
        'b25024004 b25024005 + b25024006 + b25024007 + b25024008 + b25024009 + b25024001 / %')
    structure_distribution_dict['mobile_home'] = build_item('Mobile home', data, item_levels,
        'b25024010 b25024001 / %')
    structure_distribution_dict['vehicle'] = build_item('Boat, RV, van, etc.', data, item_levels,
        'b25024011 b25024001 / %')

    # Housing: Tenure
    data, acs = get_data_fallback('B25003', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    ownership_dict = dict()
    doc['housing']['ownership'] = ownership_dict

    ownership_distribution_dict = OrderedDict()
    ownership_dict['distribution'] = ownership_distribution_dict
    add_metadata(ownership_dict['distribution'], 'b25003', 'Occupied housing units', acs_name)

    ownership_distribution_dict['owner'] = build_item('Owner occupied', data, item_levels,
        'b25003002 b25003001 / %')
    ownership_distribution_dict['renter'] = build_item('Renter occupied', data, item_levels,
        'b25003003 b25003001 / %')

    data, acs = get_data_fallback('B25026', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    length_of_tenure_dict = OrderedDict()
    doc['housing']['length_of_tenure'] = length_of_tenure_dict
    add_metadata(length_of_tenure_dict, 'b25026', 'Total population in occupied housing units', acs_name)

    length_of_tenure_dict['before_1970'] = build_item('Before 1970', data, item_levels,
        'b25026008 b25026015 + b25026001 / %')
    length_of_tenure_dict['1970s'] = build_item('1970s', data, item_levels,
        'b25026007 b25026014 + b25026001 / %')
    length_of_tenure_dict['1980s'] = build_item('1980s', data, item_levels,
        'b25026006 b25026013 + b25026001 / %')
    length_of_tenure_dict['1990s'] = build_item('1990s', data, item_levels,
        'b25026005 b25026012 + b25026001 / %')
    length_of_tenure_dict['2000_to_2004'] = build_item('2000-2004', data, item_levels,
        'b25026004 b25026011 + b25026001 / %')
    length_of_tenure_dict['since_2005'] = build_item('Since 2005', data, item_levels,
        'b25026003 b25026010 + b25026001 / %')

    # Housing: Mobility
    data, acs = get_data_fallback('B07003', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    migration_dict = dict()
    doc['housing']['migration'] = migration_dict

    migration_dict['moved_since_previous_year'] = build_item('Moved since previous year', data, item_levels,
        'b07003007 b07003010 + b07003013 + b07003016 + b07003001 / %')
    add_metadata(migration_dict['moved_since_previous_year'], 'b07003', 'Population 1 year and over in the United States', acs_name)

    migration_distribution_dict = OrderedDict()
    doc['housing']['migration_distribution'] = migration_distribution_dict
    add_metadata(migration_distribution_dict, 'b07003', 'Population 1 year and over in the United States', acs_name)

    migration_distribution_dict['same_house_year_ago'] = build_item('Same house year ago', data, item_levels,
        'b07003004 b07003001 / %')
    migration_distribution_dict['moved_same_county'] = build_item('From same county', data, item_levels,
        'b07003007 b07003001 / %')
    migration_distribution_dict['moved_different_county'] = build_item('From different county', data, item_levels,
        'b07003010 b07003001 / %')
    migration_distribution_dict['moved_different_state'] = build_item('From different state', data, item_levels,
        'b07003013 b07003001 / %')
    migration_distribution_dict['moved_from_abroad'] = build_item('From abroad', data, item_levels,
        'b07003016 b07003001 / %')

    # Housing: Median Value and Distribution of Values
    data, acs = get_data_fallback('B25077', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    ownership_dict['median_value'] = build_item('Median value of owner-occupied housing units', data, item_levels,
        'b25077001')
    add_metadata(ownership_dict['median_value'], 'b25077', 'Owner-occupied housing units', acs_name)

    data, acs = get_data_fallback('B25075', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    value_distribution = OrderedDict()
    ownership_dict['value_distribution'] = value_distribution
    add_metadata(value_distribution, 'b25075', 'Owner-occupied housing units', acs_name)

    ownership_dict['total_value'] = build_item('Total value of owner-occupied housing units', data, item_levels,
        'b25075001')

    value_distribution['under_100'] = build_item('Under $100K', data, item_levels,
        'b25075002 b25075003 + b25075004 + b25075005 + b25075006 + b25075007 + b25075008 + b25075009 + b25075010 + b25075011 + b25075012 + b25075013 + b25075014 + b25075001 / %')
    value_distribution['100_to_200'] = build_item('$100K - $200K', data, item_levels,
        'b25075015 b25075016 + b25075017 + b25075018 + b25075001 / %')
    value_distribution['200_to_300'] = build_item('$200K - $300K', data, item_levels,
        'b25075019 b25075020 + b25075001 / %')
    value_distribution['300_to_400'] = build_item('$300K - $400K', data, item_levels,
        'b25075021 b25075001 / %')
    value_distribution['400_to_500'] = build_item('$400K - $500K', data, item_levels,
        'b25075022 b25075001 / %')
    value_distribution['500_to_1000000'] = build_item('$500K - $1M', data, item_levels,
        'b25075023 b25075024 + b25075001 / %')
    value_distribution['over_1000000'] = build_item('Over $1M', data, item_levels,
        'b25075025 b25075001 / %')


    # Social: Educational Attainment
    # Two aggregated data points for "high school and higher," "college degree and higher"
    # and distribution dict for chart
    data, acs = get_data_fallback('B15002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    attainment_dict = dict()
    doc['social']['educational_attainment'] = attainment_dict

    attainment_dict['percent_high_school_grad_or_higher'] = build_item('High school grad or higher', data, item_levels,
        'b15002011 b15002012 + b15002013 + b15002014 + b15002015 + b15002016 + b15002017 + b15002018 + b15002028 + b15002029 + b15002030 + b15002031 + b15002032 + b15002033 + b15002034 + b15002035 + b15002001 / %')
    add_metadata(attainment_dict['percent_high_school_grad_or_higher'], 'b15002', 'Population 25 years and over', acs_name)

    attainment_dict['percent_bachelor_degree_or_higher'] = build_item('Bachelor\'s degree or higher', data, item_levels,
        'b15002015 b15002016 + b15002017 + b15002018 + b15002032 + b15002033 + b15002034 + b15002035 + b15002001 / %')
    add_metadata(attainment_dict['percent_bachelor_degree_or_higher'], 'b15002', 'Population 25 years and over', acs_name)

    attainment_distribution_dict = OrderedDict()
    doc['social']['educational_attainment_distribution'] = attainment_distribution_dict
    add_metadata(attainment_distribution_dict, 'b15002', 'Population 25 years and over', acs_name)

    attainment_distribution_dict['non_high_school_grad'] = build_item('No degree', data, item_levels,
        'b15002003 b15002004 + b15002005 + b15002006 + b15002007 + b15002008 + b15002009 + b15002010 + b15002020 + b15002021 + b15002022 + b15002023 + b15002024 + b15002025 + b15002026 + b15002027 + b15002001 / %')

    attainment_distribution_dict['high_school_grad'] = build_item('High school', data, item_levels,
        'b15002011 b15002028 + b15002001 / %')

    attainment_distribution_dict['some_college'] = build_item('Some college', data, item_levels,
        'b15002012 b15002013 + b15002014 + b15002029 + b15002030 + b15002031 + b15002001 / %')

    attainment_distribution_dict['bachelor_degree'] = build_item('Bachelor\'s', data, item_levels,
        'b15002015 b15002032 + b15002001 / %')

    attainment_distribution_dict['post_grad_degree'] = build_item('Post-grad', data, item_levels,
        'b15002016 b15002017 + b15002018 + b15002033 + b15002034 + b15002035 + b15002001 / %')

    # Social: Place of Birth
    data, acs = get_data_fallback('B05002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    foreign_dict = dict()
    doc['social']['place_of_birth'] = foreign_dict

    foreign_dict['percent_foreign_born'] = build_item('Foreign-born population', data, item_levels,
        'b05002013 b05002001 / %')
    add_metadata(foreign_dict['percent_foreign_born'], 'b05002', 'Total population', acs_name)

    data, acs = get_data_fallback('B05006', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    place_of_birth_dict = OrderedDict()
    foreign_dict['distribution'] = place_of_birth_dict
    add_metadata(place_of_birth_dict, 'b05006', 'Foreign-born population', acs_name)

    place_of_birth_dict['europe'] = build_item('Europe', data, item_levels,
        'b05006002 b05006001 / %')
    place_of_birth_dict['asia'] = build_item('Asia', data, item_levels,
        'b05006047 b05006001 / %')
    place_of_birth_dict['africa'] = build_item('Africa', data, item_levels,
        'b05006091 b05006001 / %')
    place_of_birth_dict['oceania'] = build_item('Oceania', data, item_levels,
        'b05006116 b05006001 / %')
    place_of_birth_dict['latin_america'] = build_item('Latin America', data, item_levels,
        'b05006123 b05006001 / %')
    place_of_birth_dict['north_america'] = build_item('North America', data, item_levels,
        'b05006159 b05006001 / %')

    # Social: Percentage of Non-English Spoken at Home, Language Spoken at Home for Children, Adults
    data, acs = get_data_fallback('B16001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    language_dict = dict()
    doc['social']['language'] = language_dict

    language_dict['percent_non_english_at_home'] = build_item('Persons with language other than English spoken at home', data, item_levels,
        'b16001001 b16001002 - b16001001 / %')
    add_metadata(language_dict['percent_non_english_at_home'], 'b16001', 'Population 5 years and over', acs_name)


    data, acs = get_data_fallback('B16007', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    language_children = OrderedDict()
    language_adults = OrderedDict()
    language_dict['children'] = language_children
    add_metadata(language_dict['children'], 'b16007', 'Population 5 years and over', acs_name)
    language_dict['adults'] = language_adults
    add_metadata(language_dict['adults'], 'b16007', 'Population 5 years and over', acs_name)

    language_children['english'] = build_item('English only', data, item_levels,
        'b16007003 b16007002 / %')
    language_adults['english'] = build_item('English only', data, item_levels,
        'b16007009 b16007015 + b16007008 b16007014 + / %')

    language_children['spanish'] = build_item('Spanish', data, item_levels,
        'b16007004 b16007002 / %')
    language_adults['spanish'] = build_item('Spanish', data, item_levels,
        'b16007010 b16007016 + b16007008 b16007014 + / %')

    language_children['indoeuropean'] = build_item('Indo-European', data, item_levels,
        'b16007005 b16007002 / %')
    language_adults['indoeuropean'] = build_item('Indo-European', data, item_levels,
        'b16007011 b16007017 + b16007008 b16007014 + / %')

    language_children['asian_islander'] = build_item('Asian/Islander', data, item_levels,
        'b16007006 b16007002 / %')
    language_adults['asian_islander'] = build_item('Asian/Islander', data, item_levels,
        'b16007012 b16007018 + b16007008 b16007014 + / %')

    language_children['other'] = build_item('Other', data, item_levels,
        'b16007007 b16007002 / %')
    language_adults['other'] = build_item('Other', data, item_levels,
        'b16007013 b16007019 + b16007008 b16007014 + / %')


    # Social: Number of Veterans, Wartime Service, Sex of Veterans
    data, acs = get_data_fallback('B21002', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    veterans_dict = dict()
    doc['social']['veterans'] = veterans_dict

    veterans_service_dict = OrderedDict()
    veterans_dict['wartime_service'] = veterans_service_dict
    add_metadata(veterans_service_dict, 'b21002', 'Civilian veterans 18 years and over', acs_name)

    veterans_service_dict['wwii'] = build_item('WWII', data, item_levels,
        'b21002009 b21002011 + b21002012 +')
    veterans_service_dict['korea'] = build_item('Korea', data, item_levels,
        'b21002008 b21002009 + b21002010 + b21002011 +')
    veterans_service_dict['vietnam'] = build_item('Vietnam', data, item_levels,
        'b21002004 b21002006 + b21002007 + b21002008 + b21002009 +')
    veterans_service_dict['gulf_1990s'] = build_item('Gulf (1990s)', data, item_levels,
        'b21002003 b21002004 + b21002005 + b21002006 +')
    veterans_service_dict['gulf_2001'] = build_item('Gulf (2001-)', data, item_levels,
        'b21002002 b21002003 + b21002004 +')

    data, acs = get_data_fallback('B21001', comparison_geoids, acs_default)
    acs_name = ACS_NAMES.get(acs).get('name')

    veterans_sex_dict = OrderedDict()
    veterans_dict['sex'] = veterans_sex_dict

    veterans_sex_dict['male'] = build_item('Male', data, item_levels,
        'b21001005')
    add_metadata(veterans_sex_dict['male'], 'b21001', 'Civilian population 18 years and over', acs_name)
    veterans_sex_dict['female'] = build_item('Female', data, item_levels,
        'b21001023')
    add_metadata(veterans_sex_dict['female'], 'b21001', 'Civilian population 18 years and over', acs_name)

    veterans_dict['number'] = build_item('Total veterans', data, item_levels,
        'b21001002')
    add_metadata(veterans_dict['number'], 'b21001', 'Civilian population 18 years and over', acs_name)

    veterans_dict['percentage'] = build_item('Population with veteran status', data, item_levels,
        'b21001002 b21001001 / %')
    add_metadata(veterans_dict['percentage'], 'b21001', 'Civilian population 18 years and over', acs_name)

    def default(obj):
        if type(obj) == decimal.Decimal:
            return int(obj)

    return json.dumps(doc, default=default)

def get_acs_name(acs_slug):
    if acs_slug in ACS_NAMES:
        acs_name = ACS_NAMES[acs_slug]['name']
    else:
        acs_name = acs_slug
    return acs_name

@app.route("/1.0/<acs>/<geoid>/profile")
def acs_geo_profile(acs, geoid):
    valid_acs, valid_geoid = find_geoid(geoid, acs)

    if not valid_acs:
        abort(404, 'GeoID %s isn\'t included in the %s release.' % (geoid, get_acs_name(acs)))

    return geo_profile(valid_acs, valid_geoid)


@app.route("/1.0/latest/<geoid>/profile")
def latest_geo_profile(geoid):
    valid_acs, valid_geoid = find_geoid(geoid)

    if not valid_acs:
        abort(404, 'None of the supported ACS releases include GeoID %s.' % (geoid))

    return geo_profile("latest", valid_geoid)


## GEO LOOKUPS ##

def convert_row(row):
    data = dict()
    data['sumlevel'] = row['sumlevel']
    data['full_geoid'] = row['full_geoid']
    data['full_name'] = row['display_name']
    if 'geom' in row and row['geom']:
        data['geom'] = json.loads(row['geom'])
    return data

# Example: /1.0/geo/search?q=spok
# Example: /1.0/geo/search?q=spok&sumlevs=050,160
@app.route("/1.0/geo/search")
@qwarg_validate({
    'lat': {'valid': FloatRange(-90.0, 90.0)},
    'lon': {'valid': FloatRange(-180.0, 180.0)},
    'q': {'valid': NonemptyString()},
    'sumlevs': {'valid': StringList(item_validator=OneOf(SUMLEV_NAMES))},
    'geom': {'valid': Bool()}
})
@crossdomain(origin='*')
def geo_search():
    lat = request.qwargs.lat
    lon = request.qwargs.lon
    q = request.qwargs.q
    sumlevs = request.qwargs.sumlevs
    with_geom = request.qwargs.geom

    if lat and lon:
        where = "ST_Intersects(geom, ST_SetSRID(ST_Point(:lon, :lat),4326))"
        where_args = {'lon': lon, 'lat': lat}
    elif q:
        q = re.sub(r'[^a-zA-Z\,\.\-0-9]', ' ', q)
        q = re.sub(r'\s+', ' ', q)
        where = "lower(prefix_match_name) LIKE lower(:q)"
        q += '%'
        where_args = {'q': q}
    else:
        abort(400, "Must provide either a lat/lon OR a query term.")

    where += " AND lower(display_name) not like '%%not defined%%' "

    if sumlevs:
        where += " AND sumlevel IN :sumlevs"
        where_args['sumlevs'] = tuple(sumlevs)

    if with_geom:
        sql = """SELECT DISTINCT geoid,sumlevel,population,display_name,full_geoid,priority,ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,0.001), 5) as geom
            FROM tiger2014.census_name_lookup
            WHERE %s
            ORDER BY priority, population DESC NULLS LAST
            LIMIT 25;""" % (where)
    else:
        sql = """SELECT DISTINCT geoid,sumlevel,population,display_name,full_geoid,priority
            FROM tiger2014.census_name_lookup
            WHERE %s
            ORDER BY priority, population DESC NULLS LAST
            LIMIT 25;""" % (where)
    result = db.session.execute(sql, where_args)

    return jsonify(results=[convert_row(row) for row in result])


def num2deg(xtile, ytile, zoom):
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return (lat_deg, lon_deg)


# Example: /1.0/geo/tiger2014/tiles/160/10/261/373.geojson
# Example: /1.0/geo/tiger2013/tiles/160/10/261/373.geojson
@app.route("/1.0/geo/<release>/tiles/<sumlevel>/<int:zoom>/<int:x>/<int:y>.geojson")
@crossdomain(origin='*')
def geo_tiles(release, sumlevel, zoom, x, y):
    if release not in allowed_tiger:
        abort(404, "Unknown TIGER release")
    if sumlevel not in SUMLEV_NAMES:
        abort(404, "Unknown sumlevel")
    if sumlevel == '010':
        abort(400, "Don't support US tiles")

    cache_key = str('1.0/geo/%s/tiles/%s/%s/%s/%s.geojson' % (release, sumlevel, zoom, x, y))
    cached = get_from_cache(cache_key)
    if cached:
        resp = make_response(cached)
    else:
        (miny, minx) = num2deg(x, y, zoom)
        (maxy, maxx) = num2deg(x + 1, y + 1, zoom)

        result = db.session.execute(
            """SELECT
                ST_AsGeoJSON(ST_SimplifyPreserveTopology(
                    ST_Intersection(ST_Buffer(ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326), 0.09, 'endcap=square'), geom),
                    ST_Perimeter(geom) / 2500), 6) as geom,
                full_geoid,
                display_name
               FROM %s.census_name_lookup
               WHERE sumlevel=:sumlev AND ST_Intersects(ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326), geom)""" % (release,),
            {'minx': minx, 'miny': miny, 'maxx': maxx, 'maxy': maxy, 'sumlev': sumlevel}
        )

        results = []
        for row in result:
            results.append({
                "type": "Feature",
                "properties": {
                    "geoid": row['full_geoid'],
                    "name": row['display_name']
                },
                "geometry": json.loads(row['geom'])
            })

        result = json.dumps(dict(type="FeatureCollection", features=results))

        resp = make_response(result)
        try:
            put_in_cache(cache_key, result, memcache=False)
        except Exception as e:
            app.logger.warn('Skipping cache set for {} because {}'.format(cache_key, e.message))

    resp.headers.set('Content-Type', 'application/json')
    resp.headers.set('Cache-Control', 'public,max-age=%d' % int(3600*4))
    return resp


# Example: /1.0/geo/tiger2014/04000US53
# Example: /1.0/geo/tiger2013/04000US53
@app.route("/1.0/geo/<release>/<geoid>")
@qwarg_validate({
    'geom': {'valid': Bool(), 'default': False}
})
@crossdomain(origin='*')
def geo_lookup(release, geoid):
    if release not in allowed_tiger:
        abort(404, "Unknown TIGER release")

    geoid = geoid.upper() if geoid else geoid
    geoid_parts = geoid.split('US')
    if len(geoid_parts) is not 2:
        abort(404, 'Invalid GeoID')

    cache_key = str('1.0/geo/%s/show/%s.json?geom=%s' % (release, geoid, request.qwargs.geom))
    cached = get_from_cache(cache_key)
    if cached:
        resp = make_response(cached)
    else:
        if request.qwargs.geom:
            result = db.session.execute(
                """SELECT display_name,simple_name,sumlevel,full_geoid,population,aland,awater,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,ST_Perimeter(geom) / 1700)) as geom
                   FROM %s.census_name_lookup
                   WHERE full_geoid=:geoid
                   LIMIT 1""" % (release,),
                {'geoid': geoid}
            )
        else:
            result = db.session.execute(
                """SELECT display_name,simple_name,sumlevel,full_geoid,population,aland,awater
                   FROM %s.census_name_lookup
                   WHERE full_geoid=:geoid
                   LIMIT 1""" % (release,),
                {'geoid': geoid}
            )

        result = result.fetchone()

        if not result:
            abort(404, 'Unknown GeoID')

        result = dict(result)
        geom = result.pop('geom', None)
        if geom:
            geom = json.loads(geom)

        result = json.dumps(dict(type="Feature", properties=result, geometry=geom))

        resp = make_response(result)
        put_in_cache(cache_key, result)

    resp.headers.set('Content-Type', 'application/json')
    resp.headers.set('Cache-Control', 'public,max-age=%d' % int(3600*4))

    return resp


# Example: /1.0/geo/tiger2014/04000US53/parents
# Example: /1.0/geo/tiger2013/04000US53/parents
@app.route("/1.0/geo/<release>/<geoid>/parents")
@crossdomain(origin='*')
def geo_parent(release, geoid):
    if release not in allowed_tiger:
        abort(404, "Unknown TIGER release")

    geoid = geoid.upper()

    cache_key = str('%s/show/%s.parents.json' % (release, geoid))
    cached = get_from_cache(cache_key)
    if cached:
        resp = make_response(cached)
    else:
        try:
            parents = compute_profile_item_levels(geoid)
        except Exception, e:
            abort(400, "Could not compute parents: " + e.message)
        parent_geoids = [p['geoid'] for p in parents]

        def build_item(p):
            return (p['full_geoid'], {
                "display_name": p['display_name'],
                "sumlevel": p['sumlevel'],
                "geoid": p['full_geoid'],
            })

        if parent_geoids:
            result = db.session.execute(
                """SELECT display_name,sumlevel,full_geoid
                   FROM %s.census_name_lookup
                   WHERE full_geoid IN :geoids
                   ORDER BY sumlevel DESC""" % (release,),
                {'geoids': tuple(parent_geoids)}
            )
            parent_list = dict([build_item(p) for p in result])

            for parent in parents:
                parent.update(parent_list.get(parent['geoid'], {}))

        result = json.dumps(dict(parents=parents))

        resp = make_response(result)
        put_in_cache(cache_key, result)

    resp.headers.set('Content-Type', 'application/json')
    resp.headers.set('Cache-Control', 'public,max-age=%d' % int(3600*4))

    return resp


# Example: /1.0/geo/show/tiger2014?geo_ids=04000US55,04000US56
# Example: /1.0/geo/show/tiger2014?geo_ids=160|04000US17,04000US56
@app.route("/1.0/geo/show/<release>")
@qwarg_validate({
    'geo_ids': {'valid': StringList(), 'required': True},
})
@crossdomain(origin='*')
def show_specified_geo_data(release):
    if release not in allowed_tiger:
        abort(404, "Unknown TIGER release")
    geo_ids, child_parent_map = expand_geoids(request.qwargs.geo_ids, release_to_expand_with)

    max_geoids = current_app.config.get('MAX_GEOIDS_TO_SHOW', 3000)
    if len(geo_ids) > max_geoids:
        abort(400, 'You requested %s geoids. The maximum is %s. Please contact us for bulk data.' % (len(geo_ids), max_geoids))

    result = db.session.execute(
        """SELECT full_geoid,
            display_name,
            aland,
            awater,
            population,
            ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,ST_Perimeter(geom) / 2500)) as geom
           FROM %s.census_name_lookup
           WHERE geom is not null and full_geoid IN :geoids;""" % (release,),
        {'geoids': tuple(geo_ids)}
    )

    results = []
    valid_geo_ids = []
    for row in result:
        valid_geo_ids.append(row['full_geoid'])
        results.append({
            "type": "Feature",
            "properties": {
                "geoid": row['full_geoid'],
                "name": row['display_name'],
                "aland": row['aland'],
                "awater": row['awater'],
                "2013_population_estimate": row['population'],
            },
            "geometry": json.loads(row['geom'])
        })

    invalid_geo_ids = set(geo_ids) - set(valid_geo_ids)
    if invalid_geo_ids:
        abort(404, "GeoID(s) %s are not valid." % (','.join(invalid_geo_ids)))

    resp_data = json.dumps({
        'type': 'FeatureCollection',
        'features': results
    })

    resp = make_response(resp_data)
    resp.headers['Content-Type'] = 'application/json'
    return resp


## TABLE LOOKUPS ##

def format_table_search_result(obj, obj_type):
    '''internal util for formatting each object in `table_search` API response'''
    result = {
        'type': obj_type,
        'table_id': obj['table_id'],
        'table_name': obj['table_title'],
        'simple_table_name': obj['simple_table_title'],
        'topics': obj['topics'],
        'universe': obj['universe'],
    }

    if obj_type == 'table':
        result.update({
            'id': obj['table_id'],
            'unique_key': obj['table_id'],
        })
    elif obj_type == 'column':
        result.update({
            'id': obj['column_id'],
            'unique_key': '%s|%s' % (obj['table_id'], obj['column_id']),
            'column_id': obj['column_id'],
            'column_name': obj['column_title'],
        })

    return result


# Example: /1.0/table/search?q=norweg
# Example: /1.0/table/search?q=norweg&topics=age,sex
# Example: /1.0/table/search?topics=housing,poverty
@app.route("/1.0/table/search")
@qwarg_validate({
    'acs': {'valid': OneOf(allowed_acs), 'default': default_table_search_release},
    'q':   {'valid': NonemptyString()},
    'topics': {'valid': StringList()}
})
@crossdomain(origin='*')
def table_search():
    # allow choice of release, default to allowed_acs[0]
    acs = request.qwargs.acs
    q = request.qwargs.q
    topics = request.qwargs.topics

    if not (q or topics):
        abort(400, "Must provide a query term or topics for filtering.")

    data = []

    if re.match(r'^\w\d{2,}$', q, flags=re.IGNORECASE):
        # we need to search 'em all because not every table is in every release...
        # might be better to have a shared table like census_tabulation_metadata?
        table_id_acs = acs
        acs_to_search = allowed_acs[:]
        acs_to_search.remove(table_id_acs)
        ids_found = set()
        while table_id_acs:
            # Matching for table id
            db.session.execute("SET search_path=:acs, public;", {'acs': table_id_acs})
            result = db.session.execute(
                """SELECT tab.table_id,
                          tab.table_title,
                          tab.simple_table_title,
                          tab.universe,
                          tab.topics
                   FROM census_table_metadata tab
                   WHERE lower(table_id) like lower(:table_id)""",
                {'table_id': '{}%'.format(q)}
            )
            for row in result:
                if row['table_id'] not in ids_found:
                    data.append(format_table_search_result(row, 'table'))
                    ids_found.add(row['table_id'])
            try:
                table_id_acs = acs_to_search.pop(0)
            except IndexError:
                table_id_acs = None
        if data:
            data.sort(key=lambda x: x['unique_key'])
            return json.dumps(data)

    db.session.execute("SET search_path=:acs, public;", {'acs': acs})
    table_where_parts = []
    table_where_args = {}
    column_where_parts = []
    column_where_args = {}

    if q and q != '*':
        q = '%%%s%%' % q
        table_where_parts.append("lower(tab.table_title) LIKE lower(:query)")
        table_where_args['query'] = q
        column_where_parts.append("lower(col.column_title) LIKE lower(:query)")
        column_where_args['query'] = q

    if topics:
        table_where_parts.append('tab.topics @> :topics')
        table_where_args['topics'] = topics
        column_where_parts.append('tab.topics @> :topics')
        column_where_args['topics'] = topics

    if table_where_parts:
        table_where = ' AND '.join(table_where_parts)
        column_where = ' AND '.join(column_where_parts)
    else:
        table_where = 'TRUE'
        column_where = 'TRUE'

    # retrieve matching tables.
    result = db.session.execute(
        """SELECT tab.tabulation_code,
                  tab.table_title,
                  tab.simple_table_title,
                  tab.universe,
                  tab.topics,
                  tab.tables_in_one_yr,
                  tab.tables_in_three_yr,
                  tab.tables_in_five_yr
           FROM census_tabulation_metadata tab
           WHERE %s
           ORDER BY tab.weight DESC""" % (table_where),
        table_where_args
    )
    for tabulation in result:
        tabulation = dict(tabulation)
        for tables_for_release_col in ('tables_in_one_yr', 'tables_in_three_yr', 'tables_in_five_yr'):
            if tabulation[tables_for_release_col]:
                tabulation['table_id'] = tabulation[tables_for_release_col][0]
            else:
                continue
            break
        data.append(format_table_search_result(tabulation, 'table'))

    # retrieve matching columns.
    if q != '*':
        # Special case for when we want ALL the tables (but not all the columns)
        result = db.session.execute(
            """SELECT col.column_id,
                      col.column_title,
                      tab.table_id,
                      tab.table_title,
                      tab.simple_table_title,
                      tab.universe,
                      tab.topics
               FROM census_column_metadata col
               LEFT OUTER JOIN census_table_metadata tab USING (table_id)
               WHERE %s
               ORDER BY char_length(tab.table_id), tab.table_id""" % (column_where),
            column_where_args
        )
        data.extend([format_table_search_result(column, 'column') for column in result])

    text = json.dumps(data)
    resp = make_response(text)
    resp.headers.set('Content-Type', 'application/json')

    return resp


# Example: /1.0/tabulation/01001
@app.route("/1.0/tabulation/<tabulation_id>")
@crossdomain(origin='*')
def tabulation_details(tabulation_id):
    result = db.session.execute(
        """SELECT *
           FROM census_tabulation_metadata
           WHERE tabulation_code=:tabulation""",
        {'tabulation': tabulation_id}
    )

    row = result.fetchone()

    if not row:
        abort(404, "Tabulation %s not found." % tabulation_id)

    row = dict(row)

    row['tables_by_release'] = {
        'one_yr': row.pop('tables_in_one_yr', []),
        'three_yr': row.pop('tables_in_three_yr', []),
        'five_yr': row.pop('tables_in_five_yr', []),
    }

    row.pop('weight', None)

    text = json.dumps(row)
    resp = make_response(text)
    resp.headers.set('Content-Type', 'application/json')

    return resp


# Example: /1.0/table/B28001?release=acs2013_1yr
@app.route("/1.0/table/<table_id>")
@qwarg_validate({
    'acs': {'valid': OneOf(allowed_acs), 'default': default_table_search_release}
})
@crossdomain(origin='*')
def table_details(table_id):
    release = request.qwargs.acs

    table_id = table_id.upper() if table_id else table_id

    cache_key = str('tables/%s/%s.json' % (release, table_id))
    cached = get_from_cache(cache_key)
    if cached:
        resp = make_response(cached)
    else:
        db.session.execute("SET search_path=:acs, public;", {'acs': request.qwargs.acs})

        result = db.session.execute(
            """SELECT *
               FROM census_table_metadata tab
               WHERE table_id=:table_id""",
            {'table_id': table_id}
        )
        row = result.fetchone()

        if not row:
            abort(404, "Table %s not found in release %s. Try specifying another release." % (table_id.upper(), release))

        data = OrderedDict([
            ("table_id", row['table_id']),
            ("table_title", row['table_title']),
            ("simple_table_title", row['simple_table_title']),
            ("subject_area", row['subject_area']),
            ("universe", row['universe']),
            ("denominator_column_id", row['denominator_column_id']),
            ("topics", row['topics'])
        ])

        result = db.session.execute(
            """SELECT *
               FROM census_column_metadata
               WHERE table_id=:table_id""",
            {'table_id': row['table_id']}
        )

        rows = []
        for row in result:
            rows.append((row['column_id'], dict(
                column_title=row['column_title'],
                indent=row['indent'],
                parent_column_id=row['parent_column_id']
            )))
        data['columns'] = OrderedDict(rows)

        result = json.dumps(data)

        resp = make_response(result)
        put_in_cache(cache_key, result)

    resp.headers.set('Content-Type', 'application/json')
    resp.headers.set('Cache-Control', 'public,max-age=%d' % int(3600*4))

    return resp


# Example: /2.0/table/latest/B28001
@app.route("/2.0/table/<release>/<table_id>")
@crossdomain(origin='*')
def table_details_with_release(release, table_id):
    if release in allowed_acs:
        acs_to_try = [release]
    elif release == 'latest':
        acs_to_try = list(allowed_acs)
    else:
        abort(404, 'The %s release isn\'t supported.' % get_acs_name(release))

    table_id = table_id.upper() if table_id else table_id

    for release in acs_to_try:
        cache_key = str('tables/%s/%s.json' % (release, table_id))
        cached = get_from_cache(cache_key)
        if cached:
            resp = make_response(cached)
        else:
            db.session.execute("SET search_path=:acs, public;", {'acs': release})

            result = db.session.execute(
                """SELECT *
                   FROM census_table_metadata tab
                   WHERE table_id=:table_id""",
                {'table_id': table_id}
            )
            row = result.fetchone()

            if not row:
                continue

            data = OrderedDict([
                ("table_id", row['table_id']),
                ("table_title", row['table_title']),
                ("simple_table_title", row['simple_table_title']),
                ("subject_area", row['subject_area']),
                ("universe", row['universe']),
                ("denominator_column_id", row['denominator_column_id']),
                ("topics", row['topics'])
            ])

            result = db.session.execute(
                """SELECT *
                   FROM census_column_metadata
                   WHERE table_id=:table_id""",
                {'table_id': row['table_id']}
            )

            rows = []
            for row in result:
                rows.append((row['column_id'], dict(
                    column_title=row['column_title'],
                    indent=row['indent'],
                    parent_column_id=row['parent_column_id']
                )))
            data['columns'] = OrderedDict(rows)

            result = json.dumps(data)

            resp = make_response(result)
            put_in_cache(cache_key, result)

        resp.headers.set('Content-Type', 'application/json')
        resp.headers.set('Cache-Control', 'public,max-age=%d' % int(3600*4))

        return resp

    abort(404, "Table %s not found in releases %s. Try specifying another release." % (table_id, ', '.join(acs_to_try)))


# Example: /1.0/table/compare/rowcounts/B01001?year=2011&sumlevel=050&within=04000US53
@app.route("/1.0/table/compare/rowcounts/<table_id>")
@qwarg_validate({
    'year': {'valid': NonemptyString()},
    'sumlevel': {'valid': OneOf(SUMLEV_NAMES), 'required': True},
    'within': {'valid': NonemptyString(), 'required': True},
    'topics': {'valid': StringList()}
})
@crossdomain(origin='*')
def table_geo_comparison_rowcount(table_id):
    years = request.qwargs.year.split(',')
    child_summary_level = request.qwargs.sumlevel
    parent_geoid = request.qwargs.within
    parent_sumlevel = parent_geoid[:3]

    data = OrderedDict()

    releases = []
    for year in years:
        releases += [name for name in allowed_acs if year in name]
    releases = sorted(releases)

    for acs in releases:
        db.session.execute("SET search_path=:acs, public;", {'acs': acs})
        release = OrderedDict()
        release['release_name'] = ACS_NAMES[acs]['name']
        release['release_slug'] = acs
        release['results'] = 0

        result = db.session.execute(
            """SELECT *
               FROM census_table_metadata
               WHERE table_id=:table_id;""",
            {'table_id': table_id}
        )
        table_record = result.fetchone()
        if table_record:
            validated_table_id = table_record['table_id']
            release['table_name'] = table_record['table_title']
            release['table_universe'] = table_record['universe']

            child_geoheaders = get_child_geoids(parent_geoid, child_summary_level)

            if child_geoheaders:
                child_geoids = [child['geoid'] for child in child_geoheaders]
                result = db.session.execute(
                    """SELECT COUNT(*)
                       FROM %s.%s
                       WHERE geoid IN :geoids""" % (acs, validated_table_id),
                    {'geoids': tuple(child_geoids)}
                )
                acs_rowcount = result.fetchone()
                release['results'] = acs_rowcount['count']

        data[acs] = release

    text = json.dumps(data)
    resp = make_response(text)
    resp.headers.set('Content-Type', 'application/json')

    return resp

## COMBINED LOOKUPS ##

@app.route("/2.1/full-text/search")
@qwarg_validate({
    'q':   {'valid': NonemptyString()},
    'type': {'valid': OneOf(allowed_searches), 'default': allowed_searches[3]},
})
@crossdomain(origin='*')
def full_text_search():

    def do_search(db, q, object_type):
        """ Search for objects (profiles, tables, topics) matching query q.

        Return a list, because it's easier to work with than a SQLAlchemy
        ResultProxy object (notably, the latter does not support indexing).
        """

        if object_type == 'profile':
            query = """SELECT text1 AS display_name,
                              text2 AS sumlevel,
                              text3 AS sumlevel_name,
                              text4 AS full_geoid,
                              text5 AS population,
                              text6 AS priority,
                              ts_rank(document, to_tsquery('simple', :search_term)) AS relevance,
                              type
                       FROM search_metadata
                       WHERE document @@ to_tsquery('simple', :search_term)
                       AND type = 'profile'
                       ORDER BY CAST(text6 as INT) ASC,
                                   CAST(text5 as INT) DESC,
                                   relevance DESC;"""

        elif object_type == 'table':
            query = """SELECT text1 AS tabulation_code,
                              text2 AS table_title,
                              text3 AS topics,
                              text4 AS simple_table_title,
                              text5 AS tables,
                              ts_rank(document, to_tsquery(:search_term), 2|8|32) AS relevance,
                              type
                       FROM search_metadata
                       WHERE document @@ to_tsquery(:search_term)
                       AND type = 'table'
                       ORDER BY relevance DESC;"""

        elif object_type == 'topic':
            query = """SELECT text1 as topic_name,
                              text3 as url,
                              ts_rank(document, to_tsquery(:search_term)) AS relevance,
                              type
                       FROM search_metadata
                       WHERE document @@ to_tsquery(:search_term)
                       AND type = 'topic'
                       ORDER BY relevance DESC;"""

        objects = db.session.execute(query, {"search_term": q})
        return [row for row in objects]

    def compute_score(row):
        """ Compute a ranking score in range [0, 1] from a row result.

        params: row - SQLAlchemy RowProxy object, which is returned by queries
        return: score in range [0, 1]
        """

        object_type = row['type']

        # Topics; set somewhat-arbitrary cutoff for PSQL relevance, above which
        # the result should appear first, and below which it should simply be
        # multiplied by some amount to make it appear slightly higher

        if object_type == 'topic':
            relevance = row['relevance']

            if relevance > 0.4:
                return 1

            else:
                return relevance * 2

        # Tables; take the PSQL relevance score, which (from our testing)
        # appears to always be in the range [1E-8, 1E-2]. For safety, we
        # generalize that to [1E-9, 1E-1] (factor of 10 on each side).
        #
        # The log sends [1E-9, 1E-1] to [-9, -1]; add 9 to send it to [0, 8];
        # divide by 8 to send it to [0, 1].

        elif object_type == 'table':
            relevance = row['relevance']
            return (log10(relevance) + 9) / 8.0

        # Profiles; compute score based off priority and population. In
        # general, larger, more populous areas should be returned first.

        elif object_type == 'profile':
            priority = row['priority']
            population = row['population']

            # Priority bounds are 5 (nation) to 320 (whatever the smallest one
            # is), so the actual range is the difference, 315.
            PRIORITY_RANGE = 320.0 - 5

            # Approximate value, but realistically it shouldn't matter much.
            POP_US = 318857056.0

            # Make population nonzero (catch both empty string and string '0')
            if not population or not int(population):
                population = 1

            priority, population = int(priority), int(population)

            # Decrement priority by 5, to map [5, 320] to [0, 315].
            priority -= 5

            # Since priority is now in [0, 315], and PRIORITY_RANGE = 315, the
            # function (1 - priority / PRIORITY_RANGE) sends 0 -> 0, 315 -> 1.
            # Similarly, the second line incorporating population maps the range
            # [0, max population] to [0, 1].
            #
            # We weight priority more than population, because from testing it
            # gives the most relevant results; the 0.8 and 0.2 can be tweaked
            # so long as they add up to 1.
            return ((1 - priority / PRIORITY_RANGE) * 0.8 +
                    (1 + log(population / POP_US) / log(POP_US)) * 0.2)

    def choose_table(tables):
        """ Choose a representative table for a list of table_ids.

        In the case where a tabulation has multiple iterations / subtables, we
        want one that is representative of all of them. The preferred order is:
            'C' table with no iterations
          > 'B' table with no iterationks
          > 'C' table with iterations (arbitrarily choosing 'A' iteration)
          > 'B' table with iterations (arbitrarily choosing 'A' iteration)
        since, generally, simpler, more complete tables are more useful. This
        function selects the most relevant table based on the hierarchy above.

        Table IDs are in the format [B/C]#####[A-I]. The first character is
        'B' or 'C', followed by five digits (the tabulation code), optionally
        ending with a character representing that this is a race iteration.
        If any iteration is present, all of them are (e.g., if B10001A is
        present, so are B10001B, ... , B10001I.)
        """

        tabulation_code = tables[0][1:6]

        # 'C' table with no iterations, e.g., C10001
        if 'C' + tabulation_code in tables:
            return 'C' + tabulation_code

        # 'B' table with no iterations, e.g., B10001
        if 'B' + tabulation_code in tables:
            return 'B' + tabulation_code

        # 'C' table with iterations, choosing 'A' iteration, e.g., C10001A
        if 'C' + tabulation_code + 'A' in tables:
            return 'C' + tabulation_code + 'A'

        # 'B' table with iterations, choosing 'A' iteration, e.g., B10001A
        if 'B' + tabulation_code + 'A' in tables:
            return 'B' + tabulation_code + 'A'

        else:
            return ''

    def process_result(row):
        """ Converts a SQLAlchemy RowProxy to a dictionary.

        params: row - row object returned from a query
        return: dictionary with either profile or table attributes """

        row = dict(row)

        if row['type'] == 'profile':
            result = {
                'type': 'profile',
                'full_geoid': row['full_geoid'],
                'full_name': row['display_name'],
                'sumlevel': row['sumlevel'],
                'sumlevel_name': row['sumlevel_name'] if row['sumlevel_name'] else '',
                'url': build_profile_url(row['full_geoid']),
                'relevance': compute_score(row) #TODO remove this
            }

        elif row['type'] == 'table':
            table_id = choose_table(row['tables'].split())

            result = {
                'type': 'table',
                'table_id': table_id,
                'tabulation_code': row['tabulation_code'],
                'table_name': row['table_title'],
                'simple_table_name': row['simple_table_title'],
                'topics': row['topics'].split(', '),
                'unique_key': row['tabulation_code'],
                'subtables': row['tables'].split(),
                'url': build_table_url(table_id),
                'relevance': compute_score(row) #TODO remove this

            }

        elif row['type'] == 'topic':
            result = {
                'type': 'topic',
                'topic_name': row['topic_name'],
                'url': row['url'],
                'relevance': compute_score(row) #TODO remove this
            }

        return result

    def build_profile_url(full_geoid):
        ''' Builds the censusreporter URL out of the geoid.

        Format: https://censusreporter.org/profiles/full_geoid
        Note that this format is a valid link, and will redirect to the
        "proper" URL with geoid and display name.

        >>> build_profile_url("31000US18020")
        "https://censusreporter.org/profiles/31000US18020/"

        '''

        return "https://censusreporter.org/profiles/" + full_geoid + "/"

    def build_table_url(table_id):
        ''' Builds the CensusReporter URL out of table_id.

        Format: https://censusreporter.org/tables/table_id/"

        >>> build_table_url("B06009")
        "http://censusreporter.org/tables/B06009/"
        '''

        return "https://censusreporter.org/tables/" + table_id + "/"


    # Build query by replacing apostrophes with spaces, separating words
    # with '&', and adding a wildcard character to support prefix matching.
    q = request.qwargs.q
    q = ' & '.join(q.split())
    q += ':*'

    search_type = request.qwargs.type

    # Support choice of 'search type' as returning table results, profile
    # results, topic results, or all. Only the needed queries will get
    # executed; e.g., for a profile search, the profiles list will be filled
    # but tables and topics will be empty.
    profiles, tables, topics = [], [], []

    if search_type == 'profile' or search_type == 'all':
        profiles = do_search(db, q, 'profile')

    if search_type == 'table' or search_type == 'all':
        tables = do_search(db, q, 'table')

    if search_type == 'topic' or search_type == 'all':
        topics = do_search(db, q, 'topic')

    # Compute ranking scores of each object that we want to return
    results = []

    for row in profiles + tables + topics:
        results.append((row, compute_score(row)))

    # Sort by second entry (score), descending; the lambda pulls the second
    # element of a tuple.
    results = sorted(results, key = lambda x: x[1], reverse = True)

    # Format of results is a list of tuples, with each tuple being a profile
    # or table followed by its score. The profile or table is then result[0].
    prepared_result = []

    for result in results:
        prepared_result.append(process_result(result[0]))

    return jsonify(results = prepared_result)



## DATA RETRIEVAL ##

# get geoheader data for children at the requested summary level
def get_child_geoids(release, parent_geoid, child_summary_level):
    parent_sumlevel = parent_geoid[0:3]
    if parent_sumlevel == '010':
        return get_all_child_geoids(release, child_summary_level)
    elif parent_sumlevel in PARENT_CHILD_CONTAINMENT and child_summary_level in PARENT_CHILD_CONTAINMENT[parent_sumlevel]:
        return get_child_geoids_by_prefix(release, parent_geoid, child_summary_level)
    elif parent_sumlevel == '160' and child_summary_level in ('140', '150'):
        return get_child_geoids_by_coverage(release, parent_geoid, child_summary_level)
    elif parent_sumlevel == '310' and child_summary_level in ('160', '860'):
        return get_child_geoids_by_coverage(release, parent_geoid, child_summary_level)
    elif parent_sumlevel == '040' and child_summary_level in ('310', '860'):
        return get_child_geoids_by_coverage(release, parent_geoid, child_summary_level)
    elif parent_sumlevel == '050' and child_summary_level in ('160', '860', '950', '960', '970'):
        return get_child_geoids_by_coverage(release, parent_geoid, child_summary_level)
    else:
        return get_child_geoids_by_gis(release, parent_geoid, child_summary_level)


def get_all_child_geoids(release, child_summary_level):
    db.session.execute("SET search_path=:acs,public;", {'acs': release})
    result = db.session.execute(
        """SELECT geoid,name
           FROM geoheader
           WHERE sumlevel=:sumlev AND component='00' AND geoid NOT IN ('04000US72')
           ORDER BY name""",
        {'sumlev': int(child_summary_level)}
    )

    return result.fetchall()


def get_child_geoids_by_coverage(release, parent_geoid, child_summary_level):
    # Use the "worst"/biggest ACS to find all child geoids
    db.session.execute("SET search_path=:acs,public;", {'acs': release})
    result = db.session.execute(
        """SELECT geoid, name
           FROM tiger2014.census_geo_containment, geoheader
           WHERE geoheader.geoid = census_geo_containment.child_geoid
             AND census_geo_containment.parent_geoid = :parent_geoid
             AND census_geo_containment.child_geoid LIKE :child_geoids""",
        {'parent_geoid': parent_geoid, 'child_geoids': child_summary_level+'%'}
    )

    rowdicts = []
    seen_geoids = set()
    for row in result:
        if not row['geoid'] in seen_geoids:
            rowdicts.append(row)
            seen_geoids.add(row['geoid'])

    return rowdicts


def get_child_geoids_by_gis(release, parent_geoid, child_summary_level):
    parent_sumlevel = parent_geoid[0:3]
    child_geoids = []
    result = db.session.execute(
        """SELECT child.full_geoid
           FROM tiger2014.census_name_lookup parent
           JOIN tiger2014.census_name_lookup child ON ST_Intersects(parent.geom, child.geom) AND child.sumlevel=:child_sumlevel
           WHERE parent.full_geoid=:parent_geoid AND parent.sumlevel=:parent_sumlevel""",
        {'child_sumlevel': child_summary_level, 'parent_geoid': parent_geoid, 'parent_sumlevel': parent_sumlevel}
    )
    child_geoids = [r['full_geoid'] for r in result]

    if child_geoids:
        # Use the "worst"/biggest ACS to find all child geoids
        db.session.execute("SET search_path=:acs,public;", {'acs': release})
        result = db.session.execute(
            """SELECT geoid,name
               FROM geoheader
               WHERE geoid IN :child_geoids
               ORDER BY name""",
            {'child_geoids': tuple(child_geoids)}
        )
        return result.fetchall()
    else:
        return []


def get_child_geoids_by_prefix(release, parent_geoid, child_summary_level):
    child_geoid_prefix = '%s00US%s%%' % (child_summary_level, parent_geoid.upper().split('US')[1])

    # Use the "worst"/biggest ACS to find all child geoids
    db.session.execute("SET search_path=:acs,public;", {'acs': release})
    result = db.session.execute(
        """SELECT geoid,name
           FROM geoheader
           WHERE geoid LIKE :geoid_prefix
             AND name NOT LIKE :not_name
           ORDER BY geoid""",
        {'geoid_prefix': child_geoid_prefix, 'not_name': '%%not defined%%'}
    )
    return result.fetchall()


def expand_geoids(geoid_list, release=None):
    if not release:
        release = expand_geoids_with

    # Look for geoid "groups" of the form `child_sumlevel|parent_geoid`.
    # These will expand into a list of geoids like the old comparison endpoint used to
    expanded_geoids = []
    explicit_geoids = []
    child_parent_map = {}
    for geoid_str in geoid_list:
        geoid_split = geoid_str.split('|')
        if len(geoid_split) == 2 and len(geoid_split[0]) == 3:
            (child_summary_level, parent_geoid) = geoid_split
            child_geoid_list = [child_geoid['geoid'] for child_geoid in get_child_geoids(release, parent_geoid, child_summary_level)]
            expanded_geoids.extend(child_geoid_list)
            for child_geoid in child_geoid_list:
                child_parent_map[child_geoid] = parent_geoid
        else:
            explicit_geoids.append(geoid_str)

    # Since the expanded geoids were sourced from the database they don't need to be checked
    valid_geo_ids = []
    valid_geo_ids.extend(expanded_geoids)

    # Check to make sure the geo ids the user entered are valid
    if explicit_geoids:
        db.session.execute("SET search_path=:acs,public;", {'acs': release})
        result = db.session.execute(
            """SELECT geoid
               FROM geoheader
               WHERE geoid IN :geoids;""",
            {'geoids': tuple(explicit_geoids)}
        )
        valid_geo_ids.extend([geo['geoid'] for geo in result])

    invalid_geo_ids = set(expanded_geoids + explicit_geoids) - set(valid_geo_ids)
    if invalid_geo_ids:
        raise ShowDataException("The %s release doesn't include GeoID(s) %s." % (get_acs_name(release), ','.join(invalid_geo_ids)))

    return set(valid_geo_ids), child_parent_map


class ShowDataException(Exception):
    pass


# Example: /1.0/data/show/acs2012_5yr?table_ids=B01001,B01003&geo_ids=04000US55,04000US56
# Example: /1.0/data/show/latest?table_ids=B01001&geo_ids=160|04000US17,04000US56
@app.route("/1.0/data/show/<acs>")
@qwarg_validate({
    'table_ids': {'valid': StringList(), 'required': True},
    'geo_ids': {'valid': StringList(), 'required': True},
})
@crossdomain(origin='*')
def show_specified_data(acs):
    if acs in allowed_acs:
        acs_to_try = [acs]
        expand_geoids_with = acs
    elif acs == 'latest':
        acs_to_try = allowed_acs[:3]  # The first three releases
        expand_geoids_with = release_to_expand_with
    else:
        abort(404, 'The %s release isn\'t supported.' % get_acs_name(acs))

    # valid_geo_ids only contains geos for which we want data
    requested_geo_ids = request.qwargs.geo_ids
    try:
        valid_geo_ids, child_parent_map = expand_geoids(requested_geo_ids, release=expand_geoids_with)
    except ShowDataException, e:
        abort(400, e.message)

    if not valid_geo_ids:
        abort(404, 'None of the geo_ids specified were valid: %s' % ', '.join(requested_geo_ids))

    max_geoids = current_app.config.get('MAX_GEOIDS_TO_SHOW', 1000)
    if len(valid_geo_ids) > max_geoids:
        abort(400, 'You requested %s geoids. The maximum is %s. Please contact us for bulk data.' % (len(valid_geo_ids), max_geoids))

    # expand_geoids has validated parents of groups by getting children;
    # this will include those parent names in the reponse `geography` list
    # but leave them out of the response `data` list
    grouped_geo_ids = [item for item in requested_geo_ids if "|" in item]
    parents_of_groups = set([item_group.split('|')[1] for item_group in grouped_geo_ids])
    named_geo_ids = valid_geo_ids | parents_of_groups

    # Fill in the display name for the geos
    result = db.session.execute(
        """SELECT full_geoid,population,display_name
           FROM tiger2014.census_name_lookup
           WHERE full_geoid IN :geoids;""",
        {'geoids': tuple(named_geo_ids)}
    )

    geo_metadata = OrderedDict()
    for geo in result:
        geo_metadata[geo['full_geoid']] = {
            'name': geo['display_name'],
        }
        # let children know who their parents are to distinguish between
        # groups at the same summary level
        if geo['full_geoid'] in child_parent_map:
            geo_metadata[geo['full_geoid']]['parent_geoid'] = child_parent_map[geo['full_geoid']]

    for acs in acs_to_try:
        try:
            db.session.execute("SET search_path=:acs, public;", {'acs': acs})

            # Check to make sure the tables requested are valid
            result = db.session.execute(
                """SELECT tab.table_id,
                          tab.table_title,
                          tab.universe,
                          tab.denominator_column_id,
                          col.column_id,
                          col.column_title,
                          col.indent
                   FROM census_column_metadata col
                   LEFT JOIN census_table_metadata tab USING (table_id)
                   WHERE table_id IN :table_ids
                   ORDER BY column_id;""",
                {'table_ids': tuple(request.qwargs.table_ids)}
            )

            valid_table_ids = []
            table_metadata = OrderedDict()
            for table, columns in groupby(result, lambda x: (x['table_id'], x['table_title'], x['universe'], x['denominator_column_id'])):
                valid_table_ids.append(table[0])
                table_metadata[table[0]] = OrderedDict([
                    ("title", table[1]),
                    ("universe", table[2]),
                    ("denominator_column_id", table[3]),
                    ("columns", OrderedDict([(
                        column['column_id'],
                        OrderedDict([
                            ("name", column['column_title']),
                            ("indent", column['indent'])
                        ])
                    ) for column in columns]))
                ])

            invalid_table_ids = set(request.qwargs.table_ids) - set(valid_table_ids)
            if invalid_table_ids:
                raise ShowDataException("The %s release doesn't include table(s) %s." % (get_acs_name(acs), ','.join(invalid_table_ids)))

            # Now fetch the actual data
            from_stmt = '%s_moe' % (valid_table_ids[0])
            if len(valid_table_ids) > 1:
                from_stmt += ' '
                from_stmt += ' '.join(['JOIN %s_moe USING (geoid)' % (table_id) for table_id in valid_table_ids[1:]])

            sql = 'SELECT * FROM %s WHERE geoid IN :geoids;' % (from_stmt,)

            result = db.session.execute(sql, {'geoids': tuple(valid_geo_ids)})
            data = OrderedDict()

            if result.rowcount != len(valid_geo_ids):
                returned_geo_ids = set([row['geoid'] for row in result])
                raise ShowDataException("The %s release doesn't include GeoID(s) %s." % (get_acs_name(acs), ','.join(set(valid_geo_ids) - returned_geo_ids)))

            for row in result:
                row = dict(row)
                geoid = row.pop('geoid')
                data_for_geoid = OrderedDict()

                # If we end up at the 'most complete' release, we should include every bit of
                # data we can instead of erroring out on the user.
                # See https://www.pivotaltracker.com/story/show/70906084
                this_geo_has_data = False or acs == allowed_acs[1]

                cols_iter = iter(sorted(row.items(), key=lambda tup: tup[0]))
                for table_id, data_iter in groupby(cols_iter, lambda x: x[0][:-3].upper()):
                    table_for_geoid = OrderedDict()
                    table_for_geoid['estimate'] = OrderedDict()
                    table_for_geoid['error'] = OrderedDict()

                    for (col_name, value) in data_iter:
                        col_name = col_name.upper()
                        (moe_name, moe_value) = next(cols_iter)

                        if value is not None and moe_value is not None:
                            this_geo_has_data = True

                        table_for_geoid['estimate'][col_name] = value
                        table_for_geoid['error'][col_name] = moe_value

                    if this_geo_has_data:
                        data_for_geoid[table_id] = table_for_geoid
                    else:
                        raise ShowDataException("The %s release doesn't have data for table %s, geoid %s." % (get_acs_name(acs), table_id, geoid))

                data[geoid] = data_for_geoid

            resp_data = json.dumps({
                'tables': table_metadata,
                'geography': geo_metadata,
                'data': data,
                'release': {
                    'id': acs,
                    'years': ACS_NAMES[acs]['years'],
                    'name': ACS_NAMES[acs]['name']
                }
            })
            resp = make_response(resp_data)
            resp.headers['Content-Type'] = 'application/json'
            return resp
        except ShowDataException, e:
            continue
    abort(400, str(e))


# Example: /1.0/data/download/acs2012_5yr?format=shp&table_ids=B01001,B01003&geo_ids=04000US55,04000US56
# Example: /1.0/data/download/latest?table_ids=B01001&geo_ids=160|04000US17,04000US56
@app.route("/1.0/data/download/<acs>")
@qwarg_validate({
    'table_ids': {'valid': StringList(), 'required': True},
    'geo_ids': {'valid': StringList(), 'required': True},
    'format': {'valid': OneOf(supported_formats), 'required': True},
})
@crossdomain(origin='*')
def download_specified_data(acs):
    if acs in allowed_acs:
        acs_to_try = [acs]
        expand_geoids_with = acs
    elif acs == 'latest':
        acs_to_try = list(allowed_acs)
        expand_geoids_with = release_to_expand_with
    else:
        abort(404, 'The %s release isn\'t supported.' % get_acs_name(acs))

    try:
        valid_geo_ids, child_parent_map = expand_geoids(request.qwargs.geo_ids, release=expand_geoids_with)
    except ShowDataException, e:
        abort(400, e.message)

    max_geoids = current_app.config.get('MAX_GEOIDS_TO_DOWNLOAD', 1000)
    if len(valid_geo_ids) > max_geoids:
        abort(400, 'You requested %s geoids. The maximum is %s. Please contact us for bulk data.' % (len(valid_geo_ids), max_geoids))

    # Fill in the display name for the geos
    result = db.session.execute(
        """SELECT full_geoid,
                  population,
                  display_name
           FROM tiger2014.census_name_lookup
           WHERE full_geoid IN :geo_ids;""",
        {'geo_ids': tuple(valid_geo_ids)}
    )

    geo_metadata = OrderedDict()
    for geo in result:
        geo_metadata[geo['full_geoid']] = {
            "name": geo['display_name'],
        }

    for acs in acs_to_try:
        try:
            db.session.execute("SET search_path=:acs, public;", {'acs': acs})

            # Check to make sure the tables requested are valid
            result = db.session.execute(
                """SELECT tab.table_id,
                          tab.table_title,
                          tab.universe,
                          tab.denominator_column_id,
                          col.column_id,
                          col.column_title,
                          col.indent
                   FROM census_column_metadata col
                   LEFT JOIN census_table_metadata tab USING (table_id)
                   WHERE table_id IN :table_ids
                   ORDER BY column_id;""",
                {'table_ids': tuple(request.qwargs.table_ids)}
            )

            valid_table_ids = []
            table_metadata = OrderedDict()
            for table, columns in groupby(result, lambda x: (x['table_id'], x['table_title'], x['universe'], x['denominator_column_id'])):
                valid_table_ids.append(table[0])
                table_metadata[table[0]] = OrderedDict([
                    ("title", table[1]),
                    ("universe", table[2]),
                    ("denominator_column_id", table[3]),
                    ("columns", OrderedDict([(
                        column['column_id'],
                        OrderedDict([
                            ("name", column['column_title']),
                            ("indent", column['indent'])
                        ])
                    ) for column in columns]))
                ])

            invalid_table_ids = set(request.qwargs.table_ids) - set(valid_table_ids)
            if invalid_table_ids:
                raise ShowDataException("The %s release doesn't include table(s) %s." % (get_acs_name(acs), ','.join(invalid_table_ids)))

            # Now fetch the actual data
            from_stmt = '%s_moe' % (valid_table_ids[0])
            if len(valid_table_ids) > 1:
                from_stmt += ' '
                from_stmt += ' '.join(['JOIN %s_moe USING (geoid)' % (table_id) for table_id in valid_table_ids[1:]])

            sql = 'SELECT * FROM %s WHERE geoid IN :geo_ids;' % (from_stmt,)

            result = db.session.execute(sql, {'geo_ids': tuple(valid_geo_ids)})
            data = OrderedDict()

            if result.rowcount != len(valid_geo_ids):
                returned_geo_ids = set([row['geoid'] for row in result])
                raise ShowDataException("The %s release doesn't include GeoID(s) %s." % (get_acs_name(acs), ','.join(set(valid_geo_ids) - returned_geo_ids)))

            for row in result.fetchall():
                row = dict(row)
                geoid = row.pop('geoid')
                data_for_geoid = OrderedDict()

                # If we end up at the 'most complete' release, we should include every bit of
                # data we can instead of erroring out on the user.
                # See https://www.pivotaltracker.com/story/show/70906084
                this_geo_has_data = False or acs == allowed_acs[-1]

                cols_iter = iter(sorted(row.items(), key=lambda tup: tup[0]))
                for table_id, data_iter in groupby(cols_iter, lambda x: x[0][:-3].upper()):
                    table_for_geoid = OrderedDict()
                    table_for_geoid['estimate'] = OrderedDict()
                    table_for_geoid['error'] = OrderedDict()

                    for (col_name, value) in data_iter:
                        col_name = col_name.upper()
                        (moe_name, moe_value) = next(cols_iter)

                        if value is not None and moe_value is not None:
                            this_geo_has_data = True

                        table_for_geoid['estimate'][col_name] = value
                        table_for_geoid['error'][col_name] = moe_value

                    if this_geo_has_data:
                        data_for_geoid[table_id] = table_for_geoid
                    else:
                        raise ShowDataException("The %s release doesn't have data for table %s, geoid %s." % (get_acs_name(acs), table_id, geoid))

                data[geoid] = data_for_geoid

            temp_path = tempfile.mkdtemp()
            file_ident = "%s_%s_%s" % (acs, next(iter(valid_table_ids)), next(iter(valid_geo_ids)))
            inner_path = os.path.join(temp_path, file_ident)
            os.mkdir(inner_path)
            out_filename = os.path.join(inner_path, '%s.%s' % (file_ident, request.qwargs.format))
            format_info = supported_formats.get(request.qwargs.format)
            builder_func = format_info['function']
            builder_func(app.config['SQLALCHEMY_DATABASE_URI'], data, table_metadata, valid_geo_ids, file_ident, out_filename, request.qwargs.format)

            metadata_dict = {
                'release': {
                    'id': acs,
                    'years': ACS_NAMES[acs]['years'],
                    'name': ACS_NAMES[acs]['name']
                },
                'tables': table_metadata
            }
            json.dump(metadata_dict, open(os.path.join(inner_path, 'metadata.json'), 'w'), indent=4)

            zfile_path = os.path.join(temp_path, file_ident + '.zip')
            zfile = zipfile.ZipFile(zfile_path, 'w', zipfile.ZIP_DEFLATED)
            for root, dirs, files in os.walk(inner_path):
                for f in files:
                    zfile.write(os.path.join(root, f), os.path.join(file_ident, f))
            zfile.close()

            resp = send_file(zfile_path, as_attachment=True, attachment_filename=file_ident + '.zip')

            shutil.rmtree(temp_path)

            return resp
        except ShowDataException, e:
            continue
    abort(400, str(e))


# Example: /1.0/data/compare/acs2012_5yr/B01001?sumlevel=050&within=04000US53
@app.route("/1.0/data/compare/<acs>/<table_id>")
@qwarg_validate({
    'within': {'valid': NonemptyString(), 'required': True},
    'sumlevel': {'valid': OneOf(SUMLEV_NAMES), 'required': True},
    'geom': {'valid': Bool(), 'default': False}
})
@crossdomain(origin='*')
def data_compare_geographies_within_parent(acs, table_id):
    # make sure we support the requested ACS release
    if acs not in allowed_acs:
        abort(404, 'The %s release isn\'t supported.' % get_acs_name(acs))
    db.session.execute("SET search_path=:acs, public;", {'acs': acs})

    parent_geoid = request.qwargs.within
    child_summary_level = request.qwargs.sumlevel

    # create the containers we need for our response
    comparison = OrderedDict()
    table = OrderedDict()
    parent_geography = OrderedDict()
    child_geographies = OrderedDict()

    # add some basic metadata about the comparison and data table requested.
    comparison['child_summary_level'] = child_summary_level
    comparison['child_geography_name'] = SUMLEV_NAMES.get(child_summary_level, {}).get('name')
    comparison['child_geography_name_plural'] = SUMLEV_NAMES.get(child_summary_level, {}).get('plural')

    result = db.session.execute(
        """SELECT tab.table_id,
                  tab.table_title,
                  tab.universe,
                  tab.denominator_column_id,
                  col.column_id,
                  col.column_title,
                  col.indent
           FROM census_column_metadata col
           LEFT JOIN census_table_metadata tab USING (table_id)
           WHERE table_id=:table_ids
           ORDER BY column_id;""",
        {'table_ids': table_id}
    )
    table_metadata = result.fetchall()

    if not table_metadata:
        abort(404, 'Table %s isn\'t available in the %s release.' % (table_id.upper(), get_acs_name(acs)))

    validated_table_id = table_metadata[0]['table_id']

    # get the basic table record, and add a map of columnID -> column name
    table_record = table_metadata[0]
    column_map = OrderedDict()
    for record in table_metadata:
        if record['column_id']:
            column_map[record['column_id']] = OrderedDict()
            column_map[record['column_id']]['name'] = record['column_title']
            column_map[record['column_id']]['indent'] = record['indent']

    table['census_release'] = ACS_NAMES.get(acs).get('name')
    table['table_id'] = validated_table_id
    table['table_name'] = table_record['table_title']
    table['table_universe'] = table_record['universe']
    table['denominator_column_id'] = table_record['denominator_column_id']
    table['columns'] = column_map

    # add some data about the parent geography
    result = db.session.execute("SELECT * FROM geoheader WHERE geoid=:geoid;", {'geoid': parent_geoid})
    parent_geoheader = result.fetchone()
    parent_sumlevel = '%03d' % parent_geoheader['sumlevel']

    parent_geography['geography'] = OrderedDict()
    parent_geography['geography']['name'] = parent_geoheader['name']
    parent_geography['geography']['summary_level'] = parent_sumlevel

    comparison['parent_summary_level'] = parent_sumlevel
    comparison['parent_geography_name'] = SUMLEV_NAMES.get(parent_sumlevel, {}).get('name')
    comparison['parent_name'] = parent_geoheader['name']
    comparison['parent_geoid'] = parent_geoid

    child_geoheaders = get_child_geoids(parent_geoid, child_summary_level)

    # start compiling child data for our response
    child_geoid_list = [geoheader['geoid'] for geoheader in child_geoheaders]
    child_geoid_names = dict([(geoheader['geoid'], geoheader['name']) for geoheader in child_geoheaders])

    # get geographical data if requested
    child_geodata_map = {}
    if request.qwargs.geom:
        # get the parent geometry and add to API response
        result = db.session.execute(
            """SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,0.001), 5) as geometry
               FROM tiger2014.census_name_lookup
               WHERE full_geoid=:geo_ids;""",
            {'geo_ids': parent_geoid}
        )
        parent_geometry = result.fetchone()
        try:
            parent_geography['geography']['geometry'] = json.loads(parent_geometry['geometry'])
        except:
            # we may not have geometries for all sumlevs
            pass

        # get the child geometries and store for later
        result = db.session.execute(
            """SELECT geoid, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom,0.001), 5) as geometry
               FROM tiger2014.census_name_lookup
               WHERE full_geoid IN :geo_ids
               ORDER BY full_geoid;""",
            {'geo_ids': tuple(child_geoid_list)}
        )
        child_geodata = result.fetchall()
        child_geodata_map = dict([(record['geoid'], json.loads(record['geometry'])) for record in child_geodata])

    # make the where clause and query the requested census data table
    # get parent data first...
    result = db.session.execute("SELECT * FROM %s_moe WHERE geoid=:geoid" % (validated_table_id), {'geoid': parent_geoheader['geoid']})
    parent_data = result.fetchone()
    parent_data.pop('geoid', None)
    column_data = []
    column_moe = []
    sorted_data = iter(sorted(parent_data.items(), key=lambda tup: tup[0]))
    for (k, v) in sorted_data:
        (moe_k, moe_v) = next(sorted_data)
        column_data.append((k.upper(), v))
        column_moe.append((k.upper(), moe_v))
    parent_geography['data'] = OrderedDict(column_data)
    parent_geography['error'] = OrderedDict(column_moe)

    if child_geoheaders:
        # ... and then children so we can loop through with cursor
        child_geoids = [child['geoid'] for child in child_geoheaders]
        result = db.session.execute("SELECT * FROM %s_moe WHERE geoid IN :geo_ids" % (validated_table_id), {'geo_ids': tuple(child_geoids)})

        # grab one row at a time
        for record in result:
            child_geoid = record.pop('geoid')

            child_data = OrderedDict()
            this_geo_has_data = False

            # build the child item
            child_data['geography'] = OrderedDict()
            child_data['geography']['name'] = child_geoid_names[child_geoid]
            child_data['geography']['summary_level'] = child_summary_level

            column_data = []
            column_moe = []
            sorted_data = iter(sorted(record.items(), key=lambda tup: tup[0]))
            for (k, v) in sorted_data:

                if v is not None and moe_v is not None:
                    this_geo_has_data = True

                (moe_k, moe_v) = next(sorted_data)
                column_data.append((k.upper(), v))
                column_moe.append((k.upper(), moe_v))
            child_data['data'] = OrderedDict(column_data)
            child_data['error'] = OrderedDict(column_moe)

            if child_geodata_map:
                try:
                    child_data['geography']['geometry'] = child_geodata_map[child_geoid.split('US')[1]]
                except:
                    # we may not have geometries for all sumlevs
                    pass

            if this_geo_has_data:
                child_geographies[child_geoid] = child_data

            # TODO Do we really need this?
            comparison['results'] = len(child_geographies)
    else:
        comparison['results'] = 0

    return jsonify(comparison=comparison, table=table, parent_geography=parent_geography, child_geographies=child_geographies)


@app.route('/healthcheck')
def healthcheck():
    return 'OK'

@app.route('/robots.txt')
def robots_txt():
    response = make_response('User-agent: *\nDisallow: /\n')
    response.headers["Content-type"] = "text/plain"
    return response

@app.route('/')
def index():
    return redirect('https://github.com/censusreporter/census-api/blob/master/API.md')

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
