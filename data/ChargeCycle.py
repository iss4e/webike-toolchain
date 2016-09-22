import collections
import logging
from datetime import timedelta, datetime

import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from dateutil.relativedelta import relativedelta

from util import DB
from util.DB import DictCursor
from util.Logging import BraceMessage as __

__author__ = "Niko Fink"
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)-3.3s %(name)-12.12s - %(message)s")

CYCLE_TYPE_COLORS = {'A': 'm', 'B': '#550055', 'S': 'c', 'T': '#005555', 'M': '#999999'}


# TODO start/end time, duration, Initial/Final State of Charge

def daterange(start, stop=datetime.now(), step=timedelta(days=1)):
    """Similar to :py:func:`builtins.range`, but for dates"""
    if start < stop:
        cmp = lambda a, b: a < b
        inc = lambda a: a + step
    else:
        cmp = lambda a, b: a > b
        inc = lambda a: a - step
    yield start
    start = inc(start)
    while cmp(start, stop):
        yield start
        start = inc(start)


def smooth(samples, label, label_smooth=None, alpha=.95, default_value=None):
    """Smooth values using the formula
    `samples[n][label_smooth] = alpha * samples[n-1][label_smooth] + (1 - alpha) * samples[n][label]`
    If a value isn't available, the previous smoothed value is used.
    If none of these exist, default_value is used
    :param samples: a list of dicts
    :param label:
    :param label_smooth:
    :param alpha:
    :param default_value:
    :return:
    """
    if not label_smooth:
        label_smooth = label + '_smooth'

    last_sample = None
    for sample in samples:
        if not (sample and label in sample and sample[label]):
            if not (last_sample and label_smooth in last_sample and
                        last_sample[label_smooth]):
                # don't have any values yet, use default
                sample[label_smooth] = default_value
            else:
                # don't have a value for this sample, keep previous smoothed values
                sample[label_smooth] = last_sample[label_smooth]
        else:
            if not (last_sample and label_smooth in last_sample and
                        last_sample[label_smooth]):
                # 1nd sensible value in the list, use it as starting point for the smoothing
                sample[label_smooth] = sample[label]
            else:
                # current and previous value available, apply the smoothing function
                sample[label_smooth] = alpha * last_sample[label_smooth] \
                                       + (1 - alpha) * sample[label]
        last_sample = sample


def discharge_curr_to_ampere(val):
    """Convert DischargeCurr from the DB from the raw sensor value to amperes"""
    return (val - 504) * 0.033 if val else 0


def can_merge(last_cycle, new_cycle, merge_within):
    # if last_cycle is actually a list, use the last value
    if isinstance(last_cycle, collections.Sequence) and not isinstance(last_cycle, tuple):
        if len(last_cycle) < 1: return False
        last_cycle = last_cycle[-1]

    last_start = last_cycle[0]['Stamp']
    last_end = last_cycle[1]['Stamp']
    new_start = new_cycle[0]['Stamp']
    new_end = new_cycle[1]['Stamp']

    # if both ranges intersect, they can for sure be merged
    if max(last_start, new_start) <= min(last_end, new_end): return True

    gap = new_start - last_end
    assert gap > timedelta(seconds=0), "new_cycle ({}) must start after last_cycle ({})".format(last_cycle, new_cycle)
    # only merge if the time gap between the two cycles is less than merge_within
    if gap > merge_within: return False
    # don't merge small samples with a big gap between them
    if new_end - new_start < gap: return False
    if last_end - last_start < gap: return False
    return True


def merge_cycles(cycles_a, cycles_b, merge_within=timedelta(minutes=30)):
    merged = [None] * (len(cycles_a) + len(cycles_b))
    a = b = m = 0
    while a < len(cycles_a) and b < len(cycles_b):
        first = cycles_a[a]
        second = cycles_b[b]
        if first[0]['Stamp'] < second[0]['Stamp']:
            a += 1
        else:
            first, second = second, first
            b += 1

        if can_merge(first, second, merge_within):
            start_time = first[0]['Stamp']  # start_time = first.start_time
            end_time = second[1]['Stamp']  # end_time = second.end_time
            sample_count = first[2] + second[2]  # samples_count ~= sum(samples_counts)
            thresh_value = second[0]['Stamp'] - first[1]['Stamp']  # thresh_value = time gap
            merged[m] = (start_time, end_time, sample_count, thresh_value, 'M')  # type = merged
        else:
            merged[m] = first
        m += 1
    while a < len(cycles_a):
        merged[m] = cycles_a[a]
        a += 1
        m += 1
    while b < len(cycles_b):
        merged[m] = cycles_b[b]
        b += 1
        m += 1
    return merged[0:m]


def extract_cycles_curr(charge_samples,
                        charge_thresh_start=50, charge_thresh_end=50, min_charge_samples=100, min_charge_amount=0.05,
                        max_sample_delay=timedelta(minutes=10), min_charge_time=timedelta(minutes=10)):
    """Detect charging cycles based on the ChargingCurr."""
    cycles = []
    discarded_cycles = []
    charge_start = charge_end = None
    charge_sample_count = 0
    charge_avg_curr = 0

    last_sample = None
    for sample in charge_samples:
        # did charging start?
        if not charge_start:
            if sample['ChargingCurr'] > charge_thresh_start:
                # yes, because ChargingCurr is high
                charge_start = sample
                charge_sample_count = 1
                charge_avg_curr = sample['ChargingCurr']

        # did charging stop?
        else:
            if sample['ChargingCurr'] < charge_thresh_end:
                # yes, because ChargingCurr is back to normal
                charge_end = last_sample
            elif sample['Stamp'] - last_sample['Stamp'] > max_sample_delay:
                # yes, because we didn't get a sample for the last few mins
                charge_end = last_sample
            else:
                # nope, continue counting
                charge_sample_count += 1
                charge_avg_curr = (charge_avg_curr + sample['ChargingCurr']) / 2

            if charge_end:
                cycle = (charge_start, charge_end, charge_sample_count, charge_avg_curr)
                # only count as charging cycle if it lasts for more than a few mins, we got enough samples
                # and we actually increased the SoC
                if charge_end['Stamp'] - charge_start['Stamp'] > min_charge_time \
                        and charge_sample_count > min_charge_samples \
                        and charge_end['soc_smooth'] - charge_start['soc_smooth'] > min_charge_amount:
                    cycles.append(cycle)
                else:
                    discarded_cycles.append(cycle)
                charge_start = None
                charge_end = None
                charge_sample_count = 0
                charge_avg_curr = 0
        last_sample = sample
    return cycles, discarded_cycles


def extract_cycles_soc(charge_samples,
                       derivate_span=10, charge_thresh_start=0.001, charge_thresh_end=0.001, min_charge_samples=100,
                       max_sample_delay=timedelta(minutes=10), min_charge_time=timedelta(minutes=30),
                       min_charge_amount=0.05, merge_within=timedelta(minutes=30)):
    """Detect charging cycles based on an increasing state of charge."""
    cycles = []
    discarded_cycles = []
    charge_start = charge_end = None
    charge_sample_count = 0

    soc_history = collections.deque(maxlen=derivate_span)
    last_sample = None
    for sample in charge_samples:
        # estimate the derivation of SoC by comparing
        # the average of the first half of the last `derivate_span` samples with
        # the average of the second half
        soc_history.append(sample['soc_smooth'])
        l = len(soc_history)
        if l >= soc_history.maxlen:
            h = list(soc_history)
            old_avg = sum(h[0:l // 2]) / len(h[0:l // 2])
            new_avg = sum(h[l // 2:l]) / len(h[l // 2:l])
            sample['soc_diff'] = new_avg - old_avg
        else:
            sample['soc_diff'] = 0

        # did charging start?
        if not charge_start:
            if sample['soc_diff'] > charge_thresh_start:
                # yes, because SoC is increasing
                charge_start = sample
                charge_sample_count = 1

        # did charging stop?
        else:
            if sample['soc_diff'] < charge_thresh_end:
                # yes, because SoC isn't increasing anymore
                charge_end = sample
            elif sample['Stamp'] - last_sample['Stamp'] > max_sample_delay:
                # yes, because we didn't get a sample for the last few mins
                charge_end = last_sample
            else:
                # nope, continue counting
                charge_sample_count += 1

            if charge_end:
                if can_merge(cycles, (charge_start, charge_end), merge_within):
                    # merge with previous cycle if they are close together
                    charge_amount = charge_end['soc_smooth'] - cycles[-1][0]['soc_smooth']
                    cycles[-1] = (cycles[-1][0], charge_end, cycles[-1][3] + charge_sample_count, charge_amount)
                else:
                    if can_merge(discarded_cycles, (charge_start, charge_end), merge_within):
                        # merge with previous discarded cycle if they are close together
                        # and check again whether they should be added altogether
                        charge_start = discarded_cycles[-1][0]
                        charge_sample_count += discarded_cycles[-1][2]
                        del discarded_cycles[-1]

                    if charge_end['Stamp'] - charge_start['Stamp'] > min_charge_time \
                            and charge_sample_count > min_charge_samples \
                            and charge_end['soc_smooth'] - charge_start['soc_smooth'] > min_charge_amount:
                        # only count as charging cycle if it lasts for more than a few mins, we got enough samples
                        # and actually increased the SoC
                        cycles.append((charge_start, charge_end, charge_sample_count,
                                       charge_end['soc_smooth'] - charge_start['soc_smooth']))
                    else:
                        discarded_cycles.append((charge_start, charge_end, charge_sample_count,
                                                 charge_end['soc_smooth'] - charge_start['soc_smooth']))

                charge_start = None
                charge_end = None
                charge_sample_count = 0

        last_sample = sample
    return cycles, discarded_cycles


# TODO validate min delta time/value and threshold, check for collision with trips
def preprocess_cycles(connection):
    with connection.cursor(DictCursor) as cursor:
        for nr, imei in enumerate(['7710']):
            logger.info(__("Preprocessing charging cycles for {}", imei))
            cursor.execute(
                """SELECT Stamp, ChargingCurr, DischargeCurr, BatteryVoltage, soc_smooth FROM imei{imei}
                JOIN webike_sfink.soc ON Stamp = time AND imei = '{imei}'
                WHERE ChargingCurr IS NOT NULL AND ChargingCurr != 0
                ORDER BY Stamp ASC"""
                    .format(imei=imei))
            charge = cursor.fetchall()
            logger.info(__("Preparing the {} rows read from DB for processing", len(charge)))

            logger.info("Detecting charging cycles based on current")
            cycles_curr, cycles_curr_disc = extract_cycles_curr(charge)
            logger.info("Detecting charging cycles based on state of charge")
            cycles_soc, cycles_soc_disc = extract_cycles_soc(charge)

            # (start, end, sample count, threshold value, type)
            cycles_curr = [(s, e, x, y, 'A') for (s, e, x, y) in cycles_curr]
            cycles_curr_disc = [(s, e, x, y, 'B') for (s, e, x, y) in cycles_curr_disc]
            cycles_soc = [(s, e, x, y, 'S') for (s, e, x, y) in cycles_soc]
            cycles_soc_disc = [(s, e, x, y, 'T') for (s, e, x, y) in cycles_soc_disc]
            # and actually, after working on the thresholds for a long time, the cycles now have pretty nice curves ;D

            cycles_merged = merge_cycles(cycles_curr, cycles_soc)
            logger.info(__("Merged {} + {} = {} detected cycles to {} non-overlapping cycles",
                           len(cycles_curr), len(cycles_soc), len(cycles_curr) + len(cycles_soc), len(cycles_merged)))

            logger.info(__("Writing ({} + {} + {} + {} = {}) detected cycles to DB",
                           len(cycles_curr), len(cycles_curr_disc), len(cycles_soc), len(cycles_soc_disc),
                           len(cycles_curr) + len(cycles_curr_disc) + len(cycles_soc) + len(cycles_soc_disc)))

            for cycle in cycles_merged + cycles_curr_disc + cycles_soc_disc:
                cursor.execute(
                    """INSERT INTO webike_sfink.charge_cycles
                    (imei, start_time, end_time, sample_count, avg_thresh_val, type)
                    VALUES (%s, %s, %s, %s, %s, %s);""",
                    [imei, cycle[0]['Stamp'], cycle[1]['Stamp'], cycle[2], cycle[3], cycle[4]])


def plot_cycles(connection):
    with connection.cursor(DictCursor) as cursor:
        for nr, imei in enumerate(['7710']):
            logger.info(__("Plotting charging cycles for {}", imei))
            cursor.execute("SELECT * FROM webike_sfink.charge_cycles WHERE imei='{}' ORDER BY start_time".format(imei))
            charge_cycles = cursor.fetchall()

            cursor.execute("SELECT * FROM trip{} ORDER BY start_time ASC".format(imei))
            trips = cursor.fetchall()

            cursor.execute("SELECT MIN(Stamp) as min, MAX(Stamp) as max FROM imei{}".format(imei))
            limits = cursor.fetchone()
            if not limits['min']:
                # FIXME weird MySQL error, non-null column Stamp is null for some tables
                limits['min'] = datetime(year=2014, month=1, day=1)

            for month in daterange(limits['min'].date(), limits['max'].date() + timedelta(days=1),
                                   relativedelta(months=1)):
                min = month
                max = month + relativedelta(months=1) - timedelta(seconds=1)
                logger.info(__("Plotting {} -- {}-{} from {} to {}", imei, month.year, month.month, min, max))

                cursor.execute(
                    """SELECT Stamp, ChargingCurr, DischargeCurr, soc_smooth FROM imei{imei}
                    JOIN webike_sfink.soc ON Stamp = time AND imei = '{imei}'
                    WHERE Stamp >= '{min}' AND Stamp <= '{max}'
                    ORDER BY Stamp ASC"""
                        .format(imei=imei, min=min, max=max))
                charge_values = cursor.fetchall()
                logger.debug(__("Preparing the {} rows read from DB for plotting", len(charge_values)))

                logger.debug("Graphing data")
                plt.clf()
                plt.xlim(min, max)

                plt.plot(
                    list([x['Stamp'] for x in charge_values]),
                    list([x['soc_smooth'] or -2 for x in charge_values]),
                    'b-', label="State of Charge", alpha=0.9
                )
                plt.plot(
                    list([x['Stamp'] for x in charge_values]),
                    list([x['ChargingCurr'] / 200 if x['ChargingCurr'] else -2 for x in charge_values]),
                    'g-', label="Charging Current", alpha=0.9
                )
                plt.plot(
                    list([x['Stamp'] for x in charge_values]),
                    list([discharge_curr_to_ampere(x['DischargeCurr'])
                          if x['DischargeCurr'] else -2 for x in charge_values]),
                    'r-', label="Discharge Current", alpha=0.9
                )

                for trip in trips:
                    plt.axvspan(trip['start_time'], trip['end_time'], color='y', alpha=0.5, lw=0)
                for cycle in charge_cycles:
                    plt.axvspan(cycle['start_time'], cycle['end_time'], color=CYCLE_TYPE_COLORS[cycle['type']],
                                alpha=0.5, lw=0)

                handles = list(plt.gca().get_legend_handles_labels()[0])
                handles.append(mpatches.Patch(color='y', label='Trips'))
                handles.append(mpatches.Patch(color=CYCLE_TYPE_COLORS['A'], label='Charging Cycles (Current based)'))
                handles.append(mpatches.Patch(color=CYCLE_TYPE_COLORS['S'], label='Charging Cycles (SoC based)'))
                plt.legend(handles=handles, loc='best')

                file = "../out/cc/{}-{}-{}.png".format(imei, month.year, month.month)
                logger.debug(__("Writing graph to {}", file))
                plt.title("{} -- {}-{}".format(imei, month.year, month.month))
                plt.xlim(min, max)
                plt.ylim(-3, 5)
                plt.gcf().set_size_inches(24, 10)
                plt.tight_layout()
                plt.gca().xaxis.set_major_locator(mdates.DayLocator())
                plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%d'))
                plt.savefig(file, dpi=300, bbox_inches='tight')


with DB.connect() as mconnection:
    preprocess_cycles(mconnection)
    mconnection.commit()
    plot_cycles(mconnection)
