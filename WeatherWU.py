import csv
import os
import shutil
from datetime import datetime, timedelta, timezone, time
from sys import stderr

import requests

from DB import DictCursor

DOWNLOAD_DIR = 'tmp/wunderground/'
URL = "https://www.wunderground.com/history/airport/CYKF/{year}/{month}/{day}/DailyHistory.html?format=1"


def insert_navlost(connection):
    print('Loading navlost data')
    with connection.cursor(DictCursor) as cursor:
        with open('tmp/f0b74520-f7df-45e4-a596-f4392296296a.csv', 'rt') as f:
            reader = csv.reader(f, delimiter='\t')
            count = 0
            for row in reader:
                stamp = datetime.strptime(row[2], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                print('Inserting {}: {}'.format(stamp, row[3]))
                count += cursor.execute(
                    "REPLACE INTO webike_sfink.weather_metar (stamp, metar, source) VALUES (%s, %s, 'navlost')",
                    [stamp, 'METAR ' + row[3]])
                if count % 1000 == 0:
                    print('\t{} rows inserted'.format(count))
            print('{} rows inserted'.format(count))


def select_missing_dates(connection):
    print('Selecting dates with missing data (this could take a few secs)...')
    with connection.cursor(DictCursor) as cursor:
        cursor.execute("""SELECT
              selected_date,
              COUNT(stamp) AS count,
              MIN(stamp) AS min,
              MAX(stamp) AS max
            FROM webike_sfink.datest
              LEFT OUTER JOIN webike_sfink.weather_metar ON selected_date = DATE(stamp)
            WHERE selected_date >=
                  (SELECT MIN(stamp)
                   FROM webike_sfink.weather_metar) AND
                  selected_date <=
                  DATE(NOW())
            GROUP BY selected_date
            HAVING count < 24 OR min > ADDTIME(selected_date, '00:00:00') OR max < ADDTIME(selected_date, '23:00:00')""")
        dates = cursor.fetchall()
        print('{} dates having too few data'.format(len(dates)))
        return dates


def download_wunderg(connection, dates):
    print('Downloading weather underground data using cache directory ' + DOWNLOAD_DIR)
    if not os.path.exists(DOWNLOAD_DIR):
        print('Created cache directory')
        os.makedirs(DOWNLOAD_DIR)

    with connection.cursor(DictCursor) as cursor:
        for entry in dates:
            if entry['min'] is None or entry['min'].time() > time():
                __download_wunderg_metar(cursor, entry['selected_date'] - timedelta(days=1))
            __download_wunderg_metar(cursor, entry['selected_date'])


def __download_wunderg_metar(cursor, date):
    print("Downloading METAR data for {}".format(date))
    file = "{}{year}-{month}-{day}.csv".format(DOWNLOAD_DIR, year=date.year, month=date.month, day=date.day)

    if os.path.exists(file):
        mtime = datetime.fromtimestamp(os.path.getmtime(file))
        if mtime < (datetime.fromordinal(date.toordinal()) + timedelta(days=2)):
            print('\tRemoving outdated version of ' + file)
            os.remove(file)
        else:
            print('\tFile already exists, no new data available')
            return
    if not os.path.exists(file):
        res = requests.get(URL.format(year=date.year, month=date.month, day=date.day), stream=True,
                           cookies={"Prefs": "SHOWMETAR:1"})
        assert res.ok
        with open(file, 'wb') as f:
            res.raw.decode_content = True
            shutil.copyfileobj(res.raw, f)

    with open(file, 'rt') as f:
        text = f.read()
        if "No daily or hourly history data available" in text:
            print("\tNo daily or hourly history data available from {}".format(file), file=stderr)
            return
        text = text.strip().replace("<br />", "").splitlines()
        reader = csv.DictReader(text, )
        count = 0
        for row in reader:
            time = datetime.strptime(row['DateUTC'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            metar = row['FullMetar']
            if metar.startswith('METAR') or metar.startswith('SPECI'):
                count += cursor.execute(
                    "REPLACE INTO webike_sfink.weather_metar (stamp, metar, source) VALUES (%s, %s, 'wunderg')",
                    [time, metar])
        print('\t{} rows inserted'.format(count))
