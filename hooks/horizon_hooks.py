#!/usr/bin/env python3
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# vim: set ts=4:et

from base64 import b64decode
import os
import sys


_path = os.path.dirname(os.path.realpath(__file__))
_root = os.path.abspath(os.path.join(_path, '..'))


def _add_path(path):
    if path not in sys.path:
        sys.path.insert(1, path)


_add_path(_root)

from charmhelpers.core.hookenv import (
    Hooks, UnregisteredHookError,
    log,
    open_port,
    config,
    relation_set,
    relation_get,
    relation_ids,
    related_units,
    unit_get,
    status_set,
    is_leader,
    local_unit,
    network_get,
)
from charmhelpers.fetch import (
    apt_update, apt_install,
    filter_installed_packages,
)
from charmhelpers.core.host import (
    lsb_release,
    service_reload,
    service_restart,
)
from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    openstack_upgrade_available,
    os_release,
    save_script_rc,
    sync_db_with_multi_ipv6_addresses,
    CompareOpenStackReleases,
    series_upgrade_prepare,
    series_upgrade_complete,
)
from charmhelpers.contrib.openstack.ha.utils import (
    generate_ha_relation_data,
)
from charmhelpers.contrib.network.ip import (
    get_relation_ip,
)
from charmhelpers.contrib.openstack.cert_utils import (
    get_certificate_request,
    process_certificates,
)
from charmhelpers.contrib.hahelpers.apache import install_ca_cert

from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.contrib.charmsupport import nrpe
from charmhelpers.contrib.hardening.harden import harden

from hooks.horizon_utils import (
    determine_packages,
    register_configs,
    restart_map,
    services,
    LOCAL_SETTINGS, HAPROXY_CONF,
    enable_ssl,
    do_openstack_upgrade,
    setup_ipv6,
    INSTALL_DIR,
    restart_on_change,
    assess_status,
    db_migration,
    check_custom_theme,
    pause_unit_helper,
    resume_unit_helper,
    remove_old_packages,
)

hooks = Hooks()
CONFIGS = register_configs()


@hooks.hook('install.real')
@harden()
def install():
    execd_preinstall()
    configure_installation_source(config('openstack-origin'))

    apt_update(fatal=True)
    packages = determine_packages()
    _os_release = os_release('openstack-dashboard')
    if CompareOpenStackReleases(_os_release) < 'icehouse':
        packages += ['nodejs', 'node-less']
    if lsb_release()['DISTRIB_CODENAME'] == 'precise':
        # Explicitly upgrade python-six Bug#1420708
        apt_install('python-six', fatal=True)
    packages = filter_installed_packages(packages)
    if packages:
        status_set('maintenance', 'Installing packages')
        apt_install(packages, fatal=True)


@hooks.hook('upgrade-charm')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
@harden()
def upgrade_charm():
    execd_preinstall()
    apt_install(filter_installed_packages(determine_packages()), fatal=True)
    packages_removed = remove_old_packages()
    update_nrpe_config()
    CONFIGS.write_all()
    if packages_removed:
        log("Package purge detected, restarting services", "INFO")
        for s in services():
            service_restart(s)
    check_custom_theme()


@hooks.hook('config-changed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
@harden()
def config_changed():
    if config('prefer-ipv6'):
        setup_ipv6()
        localhost = 'ip6-localhost'
    else:
        localhost = 'localhost'

    if (os_release('openstack-dashboard') == 'icehouse' and
            config('offline-compression') in ['no', 'False']):
        apt_install(filter_installed_packages(['python-lesscpy']),
                    fatal=True)

    # Ensure default role changes are propagated to keystone
    for relid in relation_ids('identity-service'):
        keystone_joined(relid)
    enable_ssl()

    if not config('action-managed-upgrade'):
        if openstack_upgrade_available('openstack-dashboard'):
            status_set('maintenance', 'Upgrading to new OpenStack release')
            do_openstack_upgrade(configs=CONFIGS)

    env_vars = {
        'OPENSTACK_URL_HORIZON':
        "http://{}:70{}|Login+-+OpenStack".format(
            localhost,
            config('webroot')
        ),
        'OPENSTACK_SERVICE_HORIZON': "apache2",
        'OPENSTACK_PORT_HORIZON_SSL': 433,
        'OPENSTACK_PORT_HORIZON': 70
    }
    save_script_rc(**env_vars)
    update_nrpe_config()
    CONFIGS.write_all()
    check_custom_theme()
    open_port(80)
    open_port(443)
    for relid in relation_ids('certificates'):
        for unit in related_units(relid):
            certs_changed(relation_id=relid, unit=unit)
    for relid in relation_ids('ha'):
        ha_relation_joined(relation_id=relid)

    websso_trusted_dashboard_changed()


@hooks.hook('identity-service-relation-joined')
def keystone_joined(rel_id=None):
    relation_set(relation_id=rel_id,
                 service="None",
                 region="None",
                 public_url="None",
                 admin_url="None",
                 internal_url="None",
                 requested_roles=config('default-role'))


@hooks.hook('identity-service-relation-changed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
def keystone_changed():
    CONFIGS.write_all()
    if relation_get('ca_cert'):
        install_ca_cert(b64decode(relation_get('ca_cert')))


@hooks.hook('cluster-relation-joined')
def cluster_joined(relation_id=None):
    private_addr = get_relation_ip('cluster')
    relation_set(relation_id=relation_id,
                 relation_settings={'private-address': private_addr})


@hooks.hook('cluster-relation-departed',
            'cluster-relation-changed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
def cluster_relation():
    CONFIGS.write(HAPROXY_CONF)


@hooks.hook('ha-relation-joined')
def ha_relation_joined(relation_id=None):
    settings = generate_ha_relation_data('horizon')
    relation_set(relation_id=relation_id, **settings)


@hooks.hook('website-relation-joined')
def website_relation_joined():
    relation_set(port=70,
                 hostname=unit_get('private-address'))


@hooks.hook('nrpe-external-master-relation-joined',
            'nrpe-external-master-relation-changed')
def update_nrpe_config():
    # python-dbus is used by check_upstart_job
    apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.copy_nrpe_checks()
    nrpe.add_init_service_checks(nrpe_setup, services(), current_unit)
    nrpe.add_haproxy_checks(nrpe_setup, current_unit)
    conf = nrpe_setup.config
    check_http_params = conf.get('nagios_check_http_params')
    if check_http_params:
        nrpe_setup.add_check(
            shortname='vhost',
            description='Check Virtual Host {%s}' % current_unit,
            check_cmd='check_http %s' % check_http_params
        )
    nrpe_setup.write()


@hooks.hook('dashboard-plugin-relation-joined')
def plugin_relation_joined(rel_id=None):
    bin_path = '/usr/bin'
    relation_set(release=os_release("openstack-dashboard"),
                 relation_id=rel_id,
                 bin_path=bin_path,
                 openstack_dir=INSTALL_DIR)


@hooks.hook('dashboard-plugin-relation-changed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
def update_plugin_config():
    CONFIGS.write(LOCAL_SETTINGS)


@hooks.hook('update-status')
@harden()
def update_status():
    log('Updating status.')


@hooks.hook('shared-db-relation-joined')
def db_joined():
    if config('prefer-ipv6'):
        sync_db_with_multi_ipv6_addresses(config('database'),
                                          config('database-user'))
    else:
        # Avoid churn check for access-network early
        access_network = None
        for unit in related_units():
            access_network = relation_get(unit=unit,
                                          attribute='access-network')
            if access_network:
                break
        host = get_relation_ip('shared-db', cidr_network=access_network)

        relation_set(database=config('database'),
                     username=config('database-user'),
                     hostname=host)


@hooks.hook('shared-db-relation-changed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
def db_changed():
    if 'shared-db' not in CONFIGS.complete_contexts():
        log('shared-db relation incomplete. Peer not ready?')
        return
    CONFIGS.write_all()
    if is_leader():
        allowed_units = relation_get('allowed_units')
        if allowed_units and local_unit() in allowed_units.split():
            db_migration()
        else:
            log('Not running neutron database migration, either no'
                ' allowed_units or this unit is not present')
            return
    else:
        log('Not running neutron database migration, not leader')


@hooks.hook('websso-fid-service-provider-relation-joined',
            'websso-fid-service-provider-relation-changed',
            'websso-fid-service-provider-relation-departed')
@restart_on_change(restart_map(), stopstart=True, sleep=3)
def websso_sp_changed():
    CONFIGS.write_all()


@hooks.hook('websso-trusted-dashboard-relation-joined',
            'websso-trusted-dashboard-relation-changed')
def websso_trusted_dashboard_changed():
    """
    Provide L7 endpoint details for the dashboard and also
    handle any config changes that may affect those.
    """
    relations = relation_ids('websso-trusted-dashboard')
    if not relations:
        return

    # TODO: check for vault relation in order to determine url scheme
    tls_configured = config('ssl-key') or config('enforce-ssl')
    scheme = 'https://' if tls_configured else 'http://'

    if config('dns-ha') or config('os-public-hostname'):
        hostname = config('os-public-hostname')
    elif config('vip'):
        hostname = config('vip')
    else:
        # use an ingress-address of a given unit as a fallback
        netinfo = network_get('websso-trusted-dashboard')
        hostname = netinfo['ingress-addresses'][0]

    # provide trusted dashboard URL details
    for rid in relations:
        relation_set(relation_id=rid, relation_settings={
            "scheme": scheme,
            "hostname": hostname,
            "path": "/auth/websso/"
        })


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)


@hooks.hook('certificates-relation-joined')
def certs_joined(relation_id=None):
    relation_set(
        relation_id=relation_id,
        relation_settings=get_certificate_request())


@hooks.hook('certificates-relation-changed')
def certs_changed(relation_id=None, unit=None):
    process_certificates('horizon', relation_id, unit)
    CONFIGS.write_all()
    service_reload('apache2')
    enable_ssl()


@hooks.hook('pre-series-upgrade')
def pre_series_upgrade():
    log("Running prepare series upgrade hook", "INFO")
    series_upgrade_prepare(
        pause_unit_helper, CONFIGS)


@hooks.hook('post-series-upgrade')
def post_series_upgrade():
    log("Running complete series upgrade hook", "INFO")
    series_upgrade_complete(
        resume_unit_helper, CONFIGS)


if __name__ == '__main__':
    main()
