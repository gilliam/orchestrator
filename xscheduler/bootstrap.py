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

from gevent import monkey
monkey.patch_all()

from optparse import OptionParser
import os
import time
import logging

import etcd
from gilliam.service_registry import ServiceRegistryClient, Resolver
import shortuuid
import yaml

from .cache import make_client as make_cache_client
from .executor import ExecutorManager
from .release import ReleaseStore
from . import store, util


_DEPLOY_TIMEOUT = 10 * 60
_INITIAL_RELEASE_NAME = '1'


def _create_formation(store_command, insts):
    for inst in insts:
        store_command.create(**inst.to_json())


def _create(store_command, formation, service, release, template):
    instance = shortuuid.uuid()
    return store.Instance(store_command, formation=formation, service=service,
                          name='%s.%s' % (service, instance), release=release,
                          instance=instance,
                          image=template['image'],
                          command=template.get('command'),
                          env=template.get('env', {}),
                          ports=template.get('ports', []))


def _deploy_instance(executor_manager, inst, name):
    logging.info("dispatching instance {0} to {1}".format(inst.name, name))
    executor_manager.dispatch(inst, name)
    state = executor_manager.wait(inst, name, timeout=_DEPLOY_TIMEOUT)
    if state != 'running':
        logging.error("failed to start instance {0}: state {1}".format(
            inst.name, state))
    assert state == 'running'


def _select_executor(registry_client):
    name, data = next(registry_client.query_formation('executor'))
    return data['instance']


def _bootstrap0(registry_client, executor_manager, store_client,
                store_command, release_store, formation):
    """Bootstrap the scheduler.

    The steps (and hops) we need to go through to get this up and
    running:

    1. read the release manifest (release.yml)
    2. create instances based on the release manifest.
    3. deploy the _store instance.
    4. when up and running, create formation in the _store.
    5. deploy the rest of the instances.
    6. hope for the best.
    """
    data = os.getenv('RELEASE')
    if data:
        release = yaml.load(data)
    else:
        with open(os.path.join(os.path.dirname(__file__),
                               '../release.yml')) as fp:
            release = yaml.load(fp)
    release['name'] = _INITIAL_RELEASE_NAME

    services = release['services']
    insts = {name: _create(store_command, formation, name,
                           _INITIAL_RELEASE_NAME, services[name])
             for name in services if name != '_bootstrap'}
    executor = _select_executor(registry_client)
    _deploy_instance(executor_manager, insts['_store'], executor)
    # the instance is now up and running, so now we can do a proper
    # "assign".
    logging.info("waiting for _store to start ...")
    time.sleep(4)
    store_client.start()

    insts['_store'].update(state=store.Instance.STATE_RUNNING,
                           assigned_to=executor)

    # write our release to the store.
    release_store.create(formation, _INITIAL_RELEASE_NAME, release)

    leader_lock = util.Lock(store_client, 'leader', 'bootstrapper')
    with leader_lock:
        _create_formation(store_command, insts.values())
        for name, inst in insts.items():
            if name != '_store':
                executor = _select_executor(registry_client)
                _deploy_instance(executor_manager, inst, executor)
                inst.update(state=store.Instance.STATE_RUNNING,
                            assigned_to=executor)
    logging.info("done! scheduler should be up and running!")


def main():
    parser = OptionParser()
    parser.add_option("-D", "--debug", dest="debug", action="store_true")
    parser.add_option("-s", "--service-registry", dest="service_registry",
                      default=os.getenv('GILLIAM_SERVICE_REGISTRY', ''),
                      help="service registry nodes", metavar="HOSTS")
    (options, args) = parser.parse_args()

    format = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
    logging.basicConfig(
        level=logging.DEBUG if options.debug else logging.INFO,
        format=format)

    formation = os.getenv('GILLIAM_FORMATION', 'scheduler')

    store_client = etcd.Etcd(host='_store.%s.service' % (formation,),
                             autostart=False)
    store_command = store.InstanceStoreCommand(store_client)
    store_query = store.InstanceStoreQuery(store_client, store_command)

    release_store = ReleaseStore(store_client)

    registry_client = ServiceRegistryClient(time, options.service_registry.split(','))
    registry_resolver = Resolver(registry_client)
    state_cache = make_cache_client(registry_resolver,
                                    '_cache.{0}.service'.format(formation))

    executor_manager = ExecutorManager(time, registry_client, store_query,
                                       state_cache, 5)
    executor_manager.start()
    _bootstrap0(registry_client, executor_manager, store_client, store_command,
                release_store, formation)
