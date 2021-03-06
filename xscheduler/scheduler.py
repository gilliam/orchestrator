# Copyright 2013 Johan Rydberg.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from .executor import DispatchError
from .util import RecurringTask, TokenBucketRateLimiter


_DEFAULT_RANK = '-ncont'


def _is_running(inst):
    return (inst.state == inst.STATE_PENDING or 
            inst.state == inst.STATE_RUNNING or
            inst.state == inst.STATE_MIGRATING)


class RequirementRankPlacementPolicy(object):

    def select(self, executors, options):
        """Given a set of executors and placement options, select a
        executor where the instance should be placed.
        """
        e = self._rank_executors(
                self._filter_out_executors_that_do_not_match_requirements(
                    executors, options), options)
        return next(iter(e), None)

    def _eval_requirement(self, requirement, executor):
        vars = {'tags': executor.tags, 'host': executor.host,
                'domain': executor.domain}
        return eval(requirement, vars, {})

    def _filter_out_executors_that_do_not_match_requirements(
            self, executors, options):
        requirements = options.get('requirements', [])
        if not requirements:
            return executors
        return [
            executor
            for requirement in requirements
            for executor in executors
            if self._eval_requirement(requirement, executor)]

    def _collect_vars(self, executor):
        """Collect rank variables."""
        return {'ncont': len(executor.containers())}

    def _eval_rank(self, rank, vars):
        return eval(rank, vars, {})

    def _rank_executors(self, executors, options):
        rank = options.get('rank') or _DEFAULT_RANK
        executors.sort(key=lambda executor: self._eval_rank(rank,
            self._collect_vars(executor)))
        return executors


class Scheduler(object):

    def __init__(self, clock, store_query, manager, policy):
        self._runner = RecurringTask(3, self._do_schedule)
        self.clock = clock
        self.store_query = store_query
        self.manager = manager
        self.policy = policy
        self._limiter = TokenBucketRateLimiter(clock, 10, 30)
        self.start = self._runner.start
        self.stop = self._runner.stop
        
    def _do_schedule(self):
        for instance in self.store_query.unassigned():
            if not self._limiter.check():
                break
            try:
                if instance.assigned_to:
                    instance.dispatch(self.manager, instance.assigned_to)
                else:
                    executor = self.policy.select(self.manager.clients(),
                                                  instance.placement or {})
                    if executor is not None:
                        instance.dispatch(self.manager, executor.name)
            except DispatchError:
                print "error"


class Updater(object):
    log = logging.getLogger('scheduler.updater')

    def __init__(self, clock, store_query, manager):
        self._runner = RecurringTask(3, self._do_update)
        self.store_query = store_query
        self.manager = manager
        self._limiter = TokenBucketRateLimiter(clock, 10, 30)
        self.start = self._runner.start
        self.stop = self._runner.stop

    def _equal_instance_container(self, inst, cont):
        inst_env = inst.env or {}
        cont_env = cont.env or {}
        inst_ports = inst.ports or []
        cont_ports = cont.ports or []
        return (inst.image == cont.image
                and inst.command == cont.command
                and inst_env == cont_env
                and inst_ports == cont_ports)

    def _do_update(self):
        instances = list(self.store_query.index())
        for instance, container in zip(
                instances, self.manager.containers(instances)):
            if container is None:
                continue
            if not _is_running(instance):
                continue
            if not self._equal_instance_container(instance, container):
                if not self._limiter.check():
                    break
                self.log.info("restarting %s/%s because of config change" % (
                        instance.formation, instance.name))
                try:
                    instance.restart(self.manager)
                except DispatchError:
                    print "ERROR"
            elif instance.state == instance.STATE_MIGRATING:
                if not self._limiter.check():
                    break
                # XXX: special case for instances that are stuck in
                # migrating but migration has happened, but it has not
                # been recoreded. this can happen when we're migrating
                # outselves.
                self.log.info("setting merging instance %s/%s to running" % (
                        instance.formation, instance.name))
                try:
                    instance.update(state=instance.STATE_RUNNING)
                except DispatchError:
                    print "ERROR"


class Terminator(object):
    """Process responsible for moving instances from "shutting down"
    into "terminated" by killing them off.
    """

    def __init__(self, clock, store_query, manager):
        self._runner = RecurringTask(3, self._do_terminate)
        self.store_query = store_query
        self.manager = manager
        self._limiter = TokenBucketRateLimiter(clock, 10, 30)
        self.start = self._runner.start
        self.stop = self._runner.stop

    def _do_terminate(self):
        for instance in self.store_query.shutting_down():
            if not self._limiter.check():
                break
            try:
                instance.terminate(self.manager)
            except DispatchError:
                print "ERROR"
