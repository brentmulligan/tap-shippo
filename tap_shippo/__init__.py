#!/usr/bin/env python3

'''Tap for Shippo.

The state has 0-3 fields:

  next - If defined, the URL we are currently syncing. This is obtained
         from the "next" field of the response from Shippo. It allows us to resume
         pagination across different invocations of the tap.

  last_sync_date - The datetime the last successful sync started.

  this_sync_date - The datetime this sync started.

Shippo does not provide a way to query for records that have been updated
after a specific time, so we always have to get all the records from
Shippo. Together, last_sync_date and this_sync_date allow us to avoid
emitting messages for records that have not been updated since the last
successful sync. We pad that timestamp by 2 days in order to avoid
skipping records due to clock skew.

'''

import copy
import os
import re
import time

import backoff
import pendulum
import requests
import singer
from singer import utils, metrics

REQUIRED_CONFIG_KEYS = ['start_date', 'token']
BASE_URL = "https://api.goshippo.com/"
URL_PATTERN = r'https://api.goshippo.com/(\w+).*'
CONFIG = {}
SESSION = requests.Session()
LOGGER = singer.get_logger()

SLIDING_WINDOW_DAYS = 7
SLIDING_WINDOW_STREAMS = {
    "shipments": "object_created"
}

# Field names, for the results we get from Shippo, and for the state map
LAST_START_DATE = 'last_start_date'
THIS_START_DATE = 'this_start_date'
OBJECT_UPDATED = 'object_updated'
START_DATE = 'start_date'
NEXT = 'next'

# List of all the endpoints we'll sync.
ENDPOINTS = [
    BASE_URL + "transactions?results=1000",
    BASE_URL + "refunds?results=1000",
    BASE_URL + "shipments?results=1000&{0}_gte={1}&{0}_lt={2}",
    BASE_URL + "parcels?results=1000",
    BASE_URL + "addresses?results=1000",
]


def load_schema(stream):
    '''Returns the schema for the specified stream'''
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        "schemas/{}.json".format(stream))
    return utils.load_json(path)


def client_error(exc):
    '''Indicates whether the given RequestException is a 4xx response'''
    return exc.response is not None and 400 <= exc.response.status_code < 500


def parse_stream_from_url(url):
    '''Ensure URL uses https, Shippo sometimes provides http urls in the NEXT field which raises ValueError'''
    if isinstance(url, str):
        url = url.replace("http://", "https://")

    '''Given a Shippo URL, extract the stream name (e.g. "addresses")'''
    match = re.match(URL_PATTERN, url)
    if not match:
        raise ValueError("Can't determine stream from URL " + url)
    return match.group(1)


@backoff.on_exception(backoff.expo,
                      (requests.exceptions.RequestException),
                      max_tries=5,
                      giveup=client_error,
                      factor=2)
def request(url):
    '''Make a request to the given Shippo URL.

    Handles retrying, status checking. Logs request duration and records
    per second

    '''
    headers = {'Authorization': 'ShippoToken ' + CONFIG['token']}
    headers['Shippo-API-Version'] = '2018-02-08'

    if 'user_agent' in CONFIG:
        headers['User-Agent'] = CONFIG['user_agent']

    LOGGER.info("GET %s", url)
    with metrics.http_request_timer(parse_stream_from_url(url)) as timer:
        req = requests.Request("GET", url, headers=headers).prepare()
        resp = SESSION.send(req)
        timer.tags[metrics.Tag.http_status_code] = resp.status_code
        resp.raise_for_status()
        return resp.json()


# Although the Shippo docs specify that `extra` is a map, sometimes they
# return an empty array. When this happens, we'll coerce it to an empty
# map so that it obeys the schema
def fix_extra_map(row):
    if row.get('extra') == []:
        row['extra'] = {}
    return row

def get_start(state):
    if LAST_START_DATE in state:
        return pendulum.parse(state[LAST_START_DATE]).subtract(days=2)
    return  pendulum.parse(CONFIG[START_DATE])


def sync_endpoint(initial_url, state):
    '''Syncs the url and paginates through until there are no more "next"
    urls. Yields schema, record, and state messages. Modifies state by
    setting the NEXT field every time we get a next url from Shippo. This
    allows us to resume paginating if we're terminated.

    '''
    stream = parse_stream_from_url(initial_url)
    yield singer.SchemaMessage(
        stream=stream,
        schema=load_schema(stream),
        key_properties=["object_id"])

    # The Shippo API does not return data from long ago, so we only try to
    # replicate the last 60 days
    # Some streams allow us to page by date, so we can request historical data for them
    sliding_window_key = SLIDING_WINDOW_STREAMS.get(stream)
    if sliding_window_key:
        bounded_start = get_start(state)
        sliding_query_start = bounded_start
        sliding_query_end = bounded_start.add(days=SLIDING_WINDOW_DAYS)
        url = initial_url.format(sliding_window_key,
                                 sliding_query_start.strftime("%Y-%m-%dT%I:%M:%SZ"),
                                 sliding_query_end.strftime("%Y-%m-%dT%I:%M:%SZ"))
    else:
        bounded_start = max(get_start(state), pendulum.now().subtract(days=60))
        url = initial_url
    LOGGER.info("Replicating all %s from %s", stream, bounded_start)

    rows_read = 0
    rows_written = 0

    with metrics.record_counter(parse_stream_from_url(url)) as counter:
        endpoint_start = pendulum.now()
        while url:
            state[NEXT] = url
            yield singer.StateMessage(value=state)

            data = request(url)

            for row in data['results']:
                counter.increment()
                rows_read += 1
                updated = pendulum.parse(row[OBJECT_UPDATED])
                if updated >= bounded_start:
                    row = fix_extra_map(row)
                    yield singer.RecordMessage(stream=stream, record=row)
                    rows_written += 1

            if data.get(NEXT):
                url = data.get(NEXT)
            elif sliding_window_key and sliding_query_end < endpoint_start:
                sliding_query_start = sliding_query_end
                sliding_query_end = sliding_query_start.add(days=SLIDING_WINDOW_DAYS)
                url = initial_url.format(sliding_window_key,
                                         sliding_query_start.strftime("%Y-%m-%dT%I:%M:%SZ"),
                                         sliding_query_end.strftime("%Y-%m-%dT%I:%M:%SZ"))
            else:
                url = None

    if rows_read:
        LOGGER.info("Done syncing %s. Read %d records, wrote %d (%.2f%%)",
                    stream, rows_read, rows_written, 100.0 * rows_written / float(rows_read))


def get_starting_urls(state):
    '''Returns the list of URLs to sync. Skips over any endpoints that appear
    before our "next" url, if next url exists in the state.

    '''
    next_url = state.get(NEXT)
    if next_url is None:
        return ENDPOINTS

    urls = []
    target_stream = parse_stream_from_url(next_url)
    LOGGER.info('Will pick up where we left off with URL %s (stream %s)',
                next_url, target_stream)
    for url in ENDPOINTS:
        if parse_stream_from_url(url) == target_stream:
            urls.append(next_url)
        elif urls:
            urls.append(url)
    if not urls:
        raise Exception('Unknown stream ' + target_stream)
    return urls


def do_sync(state):
    '''Main function for syncing'''
    LOGGER.info("Starting sync")
    urls = get_starting_urls(state)
    LOGGER.info('I will sync urls in this order: %s', urls)
    for url in urls:
        for msg in sync_endpoint(url, state):
            singer.write_message(msg)
    state[NEXT] = None
    state[LAST_START_DATE] = state[THIS_START_DATE]
    state[THIS_START_DATE] = None
    singer.write_state(state)
    LOGGER.info("Sync completed")


def main_impl():
    '''Entry point'''

    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    CONFIG.update(args.config)
    state = copy.deepcopy(args.state)
    if state.get(THIS_START_DATE) is None:
        state[THIS_START_DATE] = pendulum.now().to_datetime_string()
    do_sync(state)

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc
