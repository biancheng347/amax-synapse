# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import functools
import time
import gc
import platform
import attr

from prometheus_client import Gauge, Histogram, Counter
from prometheus_client.core import (
    GaugeMetricFamily, CounterMetricFamily, REGISTRY)

from twisted.internet import reactor


logger = logging.getLogger(__name__)

running_on_pypy = platform.python_implementation() == 'PyPy'
all_metrics = []
all_collectors = []
all_gauges = {}


class RegistryProxy(object):

    def collect(self):
        for metric in REGISTRY.collect():
            if not metric.name.startswith("__"):
                yield metric


@attr.s(hash=True)
class LaterGauge(object):

    name = attr.ib()
    desc = attr.ib()
    labels = attr.ib(hash=False)
    caller = attr.ib()

    def collect(self):

        g = GaugeMetricFamily(self.name, self.desc, labels=self.labels)

        try:
            calls = self.caller()
        except Exception as e:
            print(e)
            logger.err()
            yield g

        if isinstance(calls, dict):
            for k, v in calls.items():
                g.add_metric(k, v)
        else:
            g.add_metric([], calls)

        yield g

    def __attrs_post_init__(self):
        self._register()

    def _register(self):
        if self.name in all_gauges.keys():
            logger.warning("%s already registered, reregistering" % (self.name,))
            REGISTRY.unregister(all_gauges.pop(self.name))

        REGISTRY.register(self)
        all_gauges[self.name] = self


#
# Python GC metrics
#

gc_unreachable = Gauge("python_gc_unreachable_total", "Unreachable GC objects", ["gen"])
gc_time = Histogram("python_gc_time", "Time taken to GC (ms)", ["gen"], buckets=[1, 2, 5, 10, 25, 50, 100, 250, 500, 1000])

class GCCounts(object):
    def collect(self):
        gc_counts = gc.get_count()

        cm = GaugeMetricFamily("python_gc_counts", "GC cycle counts", labels=["gen"])
        for n, m in enumerate(gc.get_count()):
            cm.add_metric([str(n)], m)

        yield cm

REGISTRY.register(GCCounts())

#
# Twisted reactor metrics
#

tick_time = Histogram("python_twisted_reactor_tick_time", "Tick time of the Twisted reactor (ms)", buckets=[1, 2, 5, 10, 50, 100, 250, 500, 1000, 2000])
pending_calls_metric = Histogram("python_twisted_reactor_pending_calls", "Pending calls", buckets=[1, 2, 5, 10, 25, 50, 100, 250, 500, 1000])

#
# Federation Metrics
#

sent_edus_counter = Counter("synapse_federation_client_sent_edus", "")

sent_transactions_counter = Counter("synapse_federation_client_sent_transactions", "")

events_processed_counter = Counter("synapse_federation_client_events_processed", "")

# Used to track where various components have processed in the event stream,
# e.g. federation sending, appservice sending, etc.
event_processing_positions = Gauge("synapse_event_processing_positions", "", ["name"])

# Used to track the current max events stream position
event_persisted_position = Gauge("synapse_event_persisted_position", "")

# Used to track the received_ts of the last event processed by various
# components
event_processing_last_ts = Gauge("synapse_event_processing_last_ts", "", ["name"])

# Used to track the lag processing events. This is the time difference
# between the last processed event's received_ts and the time it was
# finished being processed.
event_processing_lag = Gauge("synapse_event_processing_lag", "", ["name"])

def runUntilCurrentTimer(func):

    @functools.wraps(func)
    def f(*args, **kwargs):
        now = reactor.seconds()
        num_pending = 0

        # _newTimedCalls is one long list of *all* pending calls. Below loop
        # is based off of impl of reactor.runUntilCurrent
        for delayed_call in reactor._newTimedCalls:
            if delayed_call.time > now:
                break

            if delayed_call.delayed_time > 0:
                continue

            num_pending += 1

        num_pending += len(reactor.threadCallQueue)
        start = time.time() * 1000
        ret = func(*args, **kwargs)
        end = time.time() * 1000

        # record the amount of wallclock time spent running pending calls.
        # This is a proxy for the actual amount of time between reactor polls,
        # since about 25% of time is actually spent running things triggered by
        # I/O events, but that is harder to capture without rewriting half the
        # reactor.
        tick_time.observe(end - start)
        pending_calls_metric.observe(num_pending)

        if running_on_pypy:
            return ret

        # Check if we need to do a manual GC (since its been disabled), and do
        # one if necessary.
        threshold = gc.get_threshold()
        counts = gc.get_count()
        for i in (2, 1, 0):
            if threshold[i] < counts[i]:
                logger.info("Collecting gc %d", i)

                start = time.time() * 1000
                unreachable = gc.collect(i)
                end = time.time() * 1000

                gc_time.labels(i).observe(end - start)
                gc_unreachable.labels(i).set(unreachable)

        return ret

    return f


try:
    # Ensure the reactor has all the attributes we expect
    reactor.runUntilCurrent
    reactor._newTimedCalls
    reactor.threadCallQueue

    # runUntilCurrent is called when we have pending calls. It is called once
    # per iteratation after fd polling.
    reactor.runUntilCurrent = runUntilCurrentTimer(reactor.runUntilCurrent)

    # We manually run the GC each reactor tick so that we can get some metrics
    # about time spent doing GC,
    if not running_on_pypy:
        gc.disable()
except AttributeError:
    pass
