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

import datetime

from xsnaga.model import Proc, App, Deploy


def transaction(f):
    def wrapper(self, *args, **kw):
        try:
            try:
                return f(self, *args, **kw)
            except Exception:
                self.store.rollback()
        finally:
            self.store.commit()
    return wrapper


def _datetime(clock):
    return datetime.datetime.utcfromtimestamp(clock.time())


class ProcStore(object):
    """Database facade for procs."""

    def __init__(self, clock, store):
        self.clock = clock
        self.store = store

    @transaction
    def create(self, app, name, deploy_id, proc_id,
               hypervisor, created_at):
        p = Proc()
        p.name = name
        p.app_id = app_id
        p.deploy = deploy_id
        p.proc_id = proc_id
        p.hypervisor = hypervisor
        p.changed_at = _datetime(self.clock.time())
        p.state = 'init'
        self.store.add(p)
        return p

    @transaction
    def remove(self, proc):
        self.store.remove(proc)

    @transaction
    def set_state(self, proc, state):
        """Set state."""
        proc.state = state
        proc.changed_at = _datetime(self.clock.time())

    def procs_for_app(self, app):
        """Return all processes for the given app.

        @return: an interator that will get you all the processes.
        """
        return self.store.find(Proc, Proc.app_id == app.id)

    def procs_for_hypervisor(self, hypervisor):
        """Return all processes for the given hypervisor.
        """
        return self.store.find(Proc, Proc.hypervisor_id == hypervisor.id)

    def expired_state_procs(self):
        """Return all processes that are 'expired' (has a state that is either
        abort or exit).
        """
        return self.store.find(Proc, (Proc.state == 'abort')
                                     | (Proc.state == 'exit'))

    def expired_deploy_procs(self):
        """Return all processes that have an outdated deploy."""
        states = ('init', 'boot', 'running')
        return self.store.find(Proc,
              Proc.state.is_in(states) & Proc.app_id == App.id
              & App.deploy == Deploy.id & Deploy.id != Proc.deploy)


class AppStore(object):
    """Application store."""

    def __init__(self, store):
        self.store = store

    @transaction
    def create(self, name, repository, text):
        """Create a new application."""
        app = App(name=name, repository=repository, text=text)
        self.store.add(app)
        return app

    def by_name(self, name):
        return self.store.find(App, App.name == name).one()

    def apps(self):
        """Return an iterable for all apps."""
        return self.store.find(App)


class DeployStore(object):

    def __init__(self, clock, store):
        self.clock = clock
        self.store = store

    @transaction
    def create(self, app, build, image, pstable, config, text):
        deploy = Deploy()
        deploy.app_id = app.id
        deploy.build = build
        deploy.image = image
        deploy.pstable = pstable
        deploy.config = config
        deploy.text = text
        deploy.timestamp = datetime.datetime.utcfromtimestamp(self.clock.time())
        self.store.add(deploy)
        return deploy

    def by_id_for_app(self, id, app):
        """Return a specific deploy."""
        return self.store.find(Deploy, (Deploy.app_id == app.id) & (
                Deploy.id == id)).one()


class HypervisorStore(object):

    def __init__(self, store):
        self.store = store

    @transaction
    def create(self, host):
        hypervisor = Hypervisor()
        hypervisor.host = host
        self.store.add(hypervisor)
        return deploy

    def by_host(self, host):
        """Return a specific hypervisor by host."""
        return self.store.find(Hypervisor, Hypervisor.host == host).one()

    @property
    def items(self):
        """."""
        return self.store.find(Hypervisor)

    @transaction
    def remove(self, hypervisor):
        self.store.remove(hypervisor)