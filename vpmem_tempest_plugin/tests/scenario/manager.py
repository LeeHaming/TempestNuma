# Copyright 2012 OpenStack Foundation
# Copyright 2013 IBM Corp.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import subprocess

import netaddr
from oslo_log import log
from oslo_serialization import jsonutils as json
from oslo_utils import netutils

from tempest.common import compute
from tempest.common import image as common_image
from tempest.common.utils.linux import remote_client
from tempest.common.utils import net_utils
from tempest.common import waiters
from tempest import config
from tempest import exceptions
from tempest.lib.common.utils import data_utils
from tempest.lib.common.utils import test_utils
from tempest.lib import exceptions as lib_exc
import tempest.test

CONF = config.CONF

LOG = log.getLogger(__name__)


class ScenarioTest(tempest.test.BaseTestCase):
    """Base class for scenario tests. Uses tempest own clients. """

    credentials = ['primary']

    @classmethod
    def setup_clients(cls):
        super(ScenarioTest, cls).setup_clients()
        # Clients (in alphabetical order)
        cls.flavors_client = cls.os_primary.flavors_client
        cls.compute_floating_ips_client = (
            cls.os_primary.compute_floating_ips_client)
        if CONF.service_available.glance:
            # Check if glance v1 is available to determine which client to use.
            if CONF.image_feature_enabled.api_v1:
                cls.image_client = cls.os_primary.image_client
            elif CONF.image_feature_enabled.api_v2:
                cls.image_client = cls.os_primary.image_client_v2
            else:
                raise lib_exc.InvalidConfiguration(
                    'Either api_v1 or api_v2 must be True in '
                    '[image-feature-enabled].')
        # Compute image client
        cls.compute_images_client = cls.os_primary.compute_images_client
        cls.keypairs_client = cls.os_primary.keypairs_client
        # Nova security groups client
        cls.compute_security_groups_client = (
            cls.os_primary.compute_security_groups_client)
        cls.compute_security_group_rules_client = (
            cls.os_primary.compute_security_group_rules_client)
        cls.servers_client = cls.os_primary.servers_client
        cls.interface_client = cls.os_primary.interfaces_client
        # Neutron network client
        cls.networks_client = cls.os_primary.networks_client
        cls.ports_client = cls.os_primary.ports_client
        cls.routers_client = cls.os_primary.routers_client
        cls.subnets_client = cls.os_primary.subnets_client
        cls.floating_ips_client = cls.os_primary.floating_ips_client
        cls.security_groups_client = cls.os_primary.security_groups_client
        cls.security_group_rules_client = (
            cls.os_primary.security_group_rules_client)
        # Use the latest available volume clients
        if CONF.service_available.cinder:
            cls.volumes_client = cls.os_primary.volumes_client_latest
            cls.snapshots_client = cls.os_primary.snapshots_client_latest
            cls.backups_client = cls.os_primary.backups_client_latest

    # ## Test functions library
    #
    # The create_[resource] functions only return body and discard the
    # resp part which is not used in scenario tests

    def create_port(self, network_id, client=None, **kwargs):
        if not client:
            client = self.ports_client
        name = data_utils.rand_name(self.__class__.__name__)
        if CONF.network.port_vnic_type and 'binding:vnic_type' not in kwargs:
            kwargs['binding:vnic_type'] = CONF.network.port_vnic_type
        if CONF.network.port_profile and 'binding:profile' not in kwargs:
            kwargs['binding:profile'] = CONF.network.port_profile
        result = client.create_port(
            name=name,
            network_id=network_id,
            **kwargs)
        port = result['port']
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        client.delete_port, port['id'])
        return port

    def create_keypair(self, client=None):
        if not client:
            client = self.keypairs_client
        name = data_utils.rand_name(self.__class__.__name__)
        # We don't need to create a keypair by pubkey in scenario
        body = client.create_keypair(name=name)
        self.addCleanup(client.delete_keypair, name)
        return body['keypair']

    def create_server(self, name=None, image_id=None, flavor=None,
                      validatable=False, wait_until='ACTIVE',
                      clients=None, **kwargs):
        """Wrapper utility that returns a test server.

        This wrapper utility calls the common create test server and
        returns a test server. The purpose of this wrapper is to minimize
        the impact on the code of the tests already using this
        function.
        """

        # NOTE(jlanoux): As a first step, ssh checks in the scenario
        # tests need to be run regardless of the run_validation and
        # validatable parameters and thus until the ssh validation job
        # becomes voting in CI. The test resources management and IP
        # association are taken care of in the scenario tests.
        # Therefore, the validatable parameter is set to false in all
        # those tests. In this way create_server just return a standard
        # server and the scenario tests always perform ssh checks.

        # Needed for the cross_tenant_traffic test:
        if clients is None:
            clients = self.os_primary

        if name is None:
            name = data_utils.rand_name(self.__class__.__name__ + "-server")

        vnic_type = CONF.network.port_vnic_type
        profile = CONF.network.port_profile

        # If vnic_type or profile are configured create port for
        # every network
        if vnic_type or profile:
            ports = []
            create_port_body = {}

            if vnic_type:
                create_port_body['binding:vnic_type'] = vnic_type

            if profile:
                create_port_body['binding:profile'] = profile

            if kwargs:
                # Convert security group names to security group ids
                # to pass to create_port
                if 'security_groups' in kwargs:
                    security_groups = \
                        clients.security_groups_client.list_security_groups(
                        ).get('security_groups')
                    sec_dict = dict([(s['name'], s['id'])
                                    for s in security_groups])

                    sec_groups_names = [s['name'] for s in kwargs.pop(
                        'security_groups')]
                    security_groups_ids = [sec_dict[s]
                                           for s in sec_groups_names]

                    if security_groups_ids:
                        create_port_body[
                            'security_groups'] = security_groups_ids
                networks = kwargs.pop('networks', [])
            else:
                networks = []

            # If there are no networks passed to us we look up
            # for the project's private networks and create a port.
            # The same behaviour as we would expect when passing
            # the call to the clients with no networks
            if not networks:
                networks = clients.networks_client.list_networks(
                    **{'router:external': False, 'fields': 'id'})['networks']

            # It's net['uuid'] if networks come from kwargs
            # and net['id'] if they come from
            # clients.networks_client.list_networks
            for net in networks:
                net_id = net.get('uuid', net.get('id'))
                if 'port' not in net:
                    port = self.create_port(network_id=net_id,
                                            client=clients.ports_client,
                                            **create_port_body)
                    ports.append({'port': port['id']})
                else:
                    ports.append({'port': net['port']})
            if ports:
                kwargs['networks'] = ports
            self.ports = ports

        tenant_network = self.get_tenant_network()

        body, _ = compute.create_test_server(
            clients,
            tenant_network=tenant_network,
            wait_until=wait_until,
            name=name, flavor=flavor,
            image_id=image_id, **kwargs)

        self.addCleanup(waiters.wait_for_server_termination,
                        clients.servers_client, body['id'])
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        clients.servers_client.delete_server, body['id'])
        server = clients.servers_client.show_server(body['id'])['server']
        return server

    def create_volume(self, size=None, name=None, snapshot_id=None,
                      imageRef=None, volume_type=None):
        if size is None:
            size = CONF.volume.volume_size
        if imageRef:
            if CONF.image_feature_enabled.api_v1:
                resp = self.image_client.check_image(imageRef)
                image = common_image.get_image_meta_from_headers(resp)
            else:
                image = self.image_client.show_image(imageRef)
            min_disk = image.get('min_disk')
            size = max(size, min_disk)
        if name is None:
            name = data_utils.rand_name(self.__class__.__name__ + "-volume")
        kwargs = {'display_name': name,
                  'snapshot_id': snapshot_id,
                  'imageRef': imageRef,
                  'volume_type': volume_type,
                  'size': size}
        volume = self.volumes_client.create_volume(**kwargs)['volume']

        self.addCleanup(self.volumes_client.wait_for_resource_deletion,
                        volume['id'])
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        self.volumes_client.delete_volume, volume['id'])
        self.assertEqual(name, volume['name'])
        waiters.wait_for_volume_resource_status(self.volumes_client,
                                                volume['id'], 'available')
        # The volume retrieved on creation has a non-up-to-date status.
        # Retrieval after it becomes active ensures correct details.
        volume = self.volumes_client.show_volume(volume['id'])['volume']
        return volume

    def create_backup(self, volume_id, name=None, description=None,
                      force=False, snapshot_id=None, incremental=False,
                      container=None):

        name = name or data_utils.rand_name(
            self.__class__.__name__ + "-backup")
        kwargs = {'name': name,
                  'description': description,
                  'force': force,
                  'snapshot_id': snapshot_id,
                  'incremental': incremental,
                  'container': container}
        backup = self.backups_client.create_backup(volume_id=volume_id,
                                                   **kwargs)['backup']
        self.addCleanup(self.backups_client.delete_backup, backup['id'])
        waiters.wait_for_volume_resource_status(self.backups_client,
                                                backup['id'], 'available')
        return backup

    def restore_backup(self, backup_id):
        restore = self.backups_client.restore_backup(backup_id)['restore']
        self.addCleanup(self.volumes_client.delete_volume,
                        restore['volume_id'])
        waiters.wait_for_volume_resource_status(self.backups_client,
                                                backup_id, 'available')
        waiters.wait_for_volume_resource_status(self.volumes_client,
                                                restore['volume_id'],
                                                'available')
        self.assertEqual(backup_id, restore['backup_id'])
        return restore

    def create_volume_snapshot(self, volume_id, name=None, description=None,
                               metadata=None, force=False):
        name = name or data_utils.rand_name(
            self.__class__.__name__ + '-snapshot')
        snapshot = self.snapshots_client.create_snapshot(
            volume_id=volume_id,
            force=force,
            display_name=name,
            description=description,
            metadata=metadata)['snapshot']
        self.addCleanup(self.snapshots_client.wait_for_resource_deletion,
                        snapshot['id'])
        self.addCleanup(self.snapshots_client.delete_snapshot, snapshot['id'])
        waiters.wait_for_volume_resource_status(self.snapshots_client,
                                                snapshot['id'], 'available')
        snapshot = self.snapshots_client.show_snapshot(
            snapshot['id'])['snapshot']
        return snapshot

    def create_volume_type(self, client=None, name=None, backend_name=None):
        if not client:
            client = self.os_admin.volume_types_client_latest
        randomized_name = name or data_utils.rand_name(
            'volume-type-' + self.__class__.__name__)

        LOG.debug("Creating a volume type: %s on backend %s",
                  randomized_name, backend_name)
        extra_specs = {}
        if backend_name:
            extra_specs = {"volume_backend_name": backend_name}

        volume_type = client.create_volume_type(
            name=randomized_name, extra_specs=extra_specs)['volume_type']
        self.addCleanup(client.delete_volume_type, volume_type['id'])
        return volume_type

    def _create_loginable_secgroup_rule(self, secgroup_id=None):
        _client = self.compute_security_groups_client
        _client_rules = self.compute_security_group_rules_client
        if secgroup_id is None:
            sgs = _client.list_security_groups()['security_groups']
            for sg in sgs:
                if sg['name'] == 'default':
                    secgroup_id = sg['id']

        # These rules are intended to permit inbound ssh and icmp
        # traffic from all sources, so no group_id is provided.
        # Setting a group_id would only permit traffic from ports
        # belonging to the same security group.
        rulesets = [
            {
                # ssh
                'ip_protocol': 'tcp',
                'from_port': 22,
                'to_port': 22,
                'cidr': '0.0.0.0/0',
            },
            {
                # ping
                'ip_protocol': 'icmp',
                'from_port': -1,
                'to_port': -1,
                'cidr': '0.0.0.0/0',
            }
        ]
        rules = list()
        for ruleset in rulesets:
            sg_rule = _client_rules.create_security_group_rule(
                parent_group_id=secgroup_id, **ruleset)['security_group_rule']
            rules.append(sg_rule)
        return rules

    def _create_security_group(self):
        # Create security group
        sg_name = data_utils.rand_name(self.__class__.__name__)
        sg_desc = sg_name + " description"
        secgroup = self.compute_security_groups_client.create_security_group(
            name=sg_name, description=sg_desc)['security_group']
        self.assertEqual(secgroup['name'], sg_name)
        self.assertEqual(secgroup['description'], sg_desc)
        self.addCleanup(
            test_utils.call_and_ignore_notfound_exc,
            self.compute_security_groups_client.delete_security_group,
            secgroup['id'])

        # Add rules to the security group
        self._create_loginable_secgroup_rule(secgroup['id'])

        return secgroup

    def get_remote_client(self, ip_address, username=None, private_key=None,
                          server=None):
        """Get a SSH client to a remote server

        :param ip_address: the server floating or fixed IP address to use
                           for ssh validation
        :param username: name of the Linux account on the remote server
        :param private_key: the SSH private key to use
        :param server: server dict, used for debugging purposes
        :return: a RemoteClient object
        """

        if username is None:
            username = CONF.validation.image_ssh_user
        # Set this with 'keypair' or others to log in with keypair or
        # username/password.
        if CONF.validation.auth_method == 'keypair':
            password = None
            if private_key is None:
                private_key = self.keypair['private_key']
        else:
            password = CONF.validation.image_ssh_password
            private_key = None
        linux_client = remote_client.RemoteClient(
            ip_address, username, pkey=private_key, password=password,
            server=server, servers_client=self.servers_client)
        linux_client.validate_authentication()
        return linux_client

    def _image_create(self, name, fmt, path,
                      disk_format=None, properties=None):
        if properties is None:
            properties = {}
        name = data_utils.rand_name('%s-' % name)
        params = {
            'name': name,
            'container_format': fmt,
            'disk_format': disk_format or fmt,
        }
        if CONF.image_feature_enabled.api_v1:
            params['is_public'] = 'False'
            params['properties'] = properties
            params = {'headers': common_image.image_meta_to_headers(**params)}
        else:
            params['visibility'] = 'private'
            # Additional properties are flattened out in the v2 API.
            params.update(properties)
        body = self.image_client.create_image(**params)
        image = body['image'] if 'image' in body else body
        self.addCleanup(self.image_client.delete_image, image['id'])
        self.assertEqual("queued", image['status'])
        with open(path, 'rb') as image_file:
            if CONF.image_feature_enabled.api_v1:
                self.image_client.update_image(image['id'], data=image_file)
            else:
                self.image_client.store_image_file(image['id'], image_file)
        return image['id']

    def glance_image_create(self):
        img_path = CONF.scenario.img_dir + "/" + CONF.scenario.img_file
        aki_img_path = CONF.scenario.img_dir + "/" + CONF.scenario.aki_img_file
        ari_img_path = CONF.scenario.img_dir + "/" + CONF.scenario.ari_img_file
        ami_img_path = CONF.scenario.img_dir + "/" + CONF.scenario.ami_img_file
        img_container_format = CONF.scenario.img_container_format
        img_disk_format = CONF.scenario.img_disk_format
        img_properties = CONF.scenario.img_properties
        LOG.debug("paths: img: %s, container_format: %s, disk_format: %s, "
                  "properties: %s, ami: %s, ari: %s, aki: %s",
                  img_path, img_container_format, img_disk_format,
                  img_properties, ami_img_path, ari_img_path, aki_img_path)
        try:
            image = self._image_create('scenario-img',
                                       img_container_format,
                                       img_path,
                                       disk_format=img_disk_format,
                                       properties=img_properties)
        except IOError:
            LOG.warning(
                "A(n) %s image was not found. Retrying with uec image.",
                img_disk_format)
            kernel = self._image_create('scenario-aki', 'aki', aki_img_path)
            ramdisk = self._image_create('scenario-ari', 'ari', ari_img_path)
            properties = {'kernel_id': kernel, 'ramdisk_id': ramdisk}
            image = self._image_create('scenario-ami', 'ami',
                                       path=ami_img_path,
                                       properties=properties)
        LOG.debug("image:%s", image)

        return image

    def _log_console_output(self, servers=None, client=None):
        if not CONF.compute_feature_enabled.console_output:
            LOG.debug('Console output not supported, cannot log')
            return
        client = client or self.servers_client
        if not servers:
            servers = client.list_servers()
            servers = servers['servers']
        for server in servers:
            try:
                console_output = client.get_console_output(
                    server['id'])['output']
                LOG.debug('Console output for %s\nbody=\n%s',
                          server['id'], console_output)
            except lib_exc.NotFound:
                LOG.debug("Server %s disappeared(deleted) while looking "
                          "for the console log", server['id'])

    def _log_net_info(self, exc):
        # network debug is called as part of ssh init
        if not isinstance(exc, lib_exc.SSHTimeout):
            LOG.debug('Network information on a devstack host')

    def create_server_snapshot(self, server, name=None):
        # Glance client
        _image_client = self.image_client
        # Compute client
        _images_client = self.compute_images_client
        if name is None:
            name = data_utils.rand_name(self.__class__.__name__ + 'snapshot')
        LOG.debug("Creating a snapshot image for server: %s", server['name'])
        image = _images_client.create_image(server['id'], name=name)
        image_id = image.response['location'].split('images/')[1]
        waiters.wait_for_image_status(_image_client, image_id, 'active')

        self.addCleanup(_image_client.wait_for_resource_deletion,
                        image_id)
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        _image_client.delete_image, image_id)

        if CONF.image_feature_enabled.api_v1:
            # In glance v1 the additional properties are stored in the headers.
            resp = _image_client.check_image(image_id)
            snapshot_image = common_image.get_image_meta_from_headers(resp)
            image_props = snapshot_image.get('properties', {})
        else:
            # In glance v2 the additional properties are flattened.
            snapshot_image = _image_client.show_image(image_id)
            image_props = snapshot_image

        bdm = image_props.get('block_device_mapping')
        if bdm:
            bdm = json.loads(bdm)
            if bdm and 'snapshot_id' in bdm[0]:
                snapshot_id = bdm[0]['snapshot_id']
                self.addCleanup(
                    self.snapshots_client.wait_for_resource_deletion,
                    snapshot_id)
                self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                                self.snapshots_client.delete_snapshot,
                                snapshot_id)
                waiters.wait_for_volume_resource_status(self.snapshots_client,
                                                        snapshot_id,
                                                        'available')
        image_name = snapshot_image['name']
        self.assertEqual(name, image_name)
        LOG.debug("Created snapshot image %s for server %s",
                  image_name, server['name'])
        return snapshot_image

    def nova_volume_attach(self, server, volume_to_attach):
        volume = self.servers_client.attach_volume(
            server['id'], volumeId=volume_to_attach['id'], device='/dev/%s'
            % CONF.compute.volume_device_name)['volumeAttachment']
        self.assertEqual(volume_to_attach['id'], volume['id'])
        waiters.wait_for_volume_resource_status(self.volumes_client,
                                                volume['id'], 'in-use')

        # Return the updated volume after the attachment
        return self.volumes_client.show_volume(volume['id'])['volume']

    def nova_volume_detach(self, server, volume):
        self.servers_client.detach_volume(server['id'], volume['id'])
        waiters.wait_for_volume_resource_status(self.volumes_client,
                                                volume['id'], 'available')

    def ping_ip_address(self, ip_address, should_succeed=True,
                        ping_timeout=None, mtu=None, server=None):
        timeout = ping_timeout or CONF.validation.ping_timeout
        cmd = ['ping', '-c1', '-w1']

        if mtu:
            cmd += [
                # don't fragment
                '-M', 'do',
                # ping receives just the size of ICMP payload
                '-s', str(net_utils.get_ping_payload_size(mtu, 4))
            ]
        cmd.append(ip_address)

        def ping():
            proc = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            proc.communicate()

            return (proc.returncode == 0) == should_succeed

        caller = test_utils.find_test_caller()
        LOG.debug('%(caller)s begins to ping %(ip)s in %(timeout)s sec and the'
                  ' expected result is %(should_succeed)s', {
                      'caller': caller, 'ip': ip_address, 'timeout': timeout,
                      'should_succeed':
                      'reachable' if should_succeed else 'unreachable'
                  })
        result = test_utils.call_until_true(ping, timeout, 1)
        LOG.debug('%(caller)s finishes ping %(ip)s in %(timeout)s sec and the '
                  'ping result is %(result)s', {
                      'caller': caller, 'ip': ip_address, 'timeout': timeout,
                      'result': 'expected' if result else 'unexpected'
                  })
        if server:
            self._log_console_output([server])
        return result

    def check_vm_connectivity(self, ip_address,
                              username=None,
                              private_key=None,
                              should_connect=True,
                              extra_msg="",
                              server=None,
                              mtu=None):
        """Check server connectivity

        :param ip_address: server to test against
        :param username: server's ssh username
        :param private_key: server's ssh private key to be used
        :param should_connect: True/False indicates positive/negative test
            positive - attempt ping and ssh
            negative - attempt ping and fail if succeed
        :param extra_msg: Message to help with debugging if ``ping_ip_address``
            fails
        :param server: The server whose console to log for debugging
        :param mtu: network MTU to use for connectivity validation

        :raises: AssertError if the result of the connectivity check does
            not match the value of the should_connect param
        """
        LOG.debug('checking network connections to IP %s with user: %s',
                  ip_address, username)
        if should_connect:
            msg = "Timed out waiting for %s to become reachable" % ip_address
        else:
            msg = "ip address %s is reachable" % ip_address
        if extra_msg:
            msg = "%s\n%s" % (extra_msg, msg)
        self.assertTrue(self.ping_ip_address(ip_address,
                                             should_succeed=should_connect,
                                             mtu=mtu, server=server),
                        msg=msg)
        if should_connect:
            # no need to check ssh for negative connectivity
            try:
                self.get_remote_client(ip_address, username, private_key,
                                       server=server)
            except Exception:
                if not extra_msg:
                    extra_msg = 'Failed to ssh to %s' % ip_address
                LOG.exception(extra_msg)
                raise

    def create_floating_ip(self, thing, pool_name=None):
        """Create a floating IP and associates to a server on Nova"""

        if not pool_name:
            pool_name = CONF.network.floating_network_name
        floating_ip = (self.compute_floating_ips_client.
                       create_floating_ip(pool=pool_name)['floating_ip'])
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        self.compute_floating_ips_client.delete_floating_ip,
                        floating_ip['id'])
        self.compute_floating_ips_client.associate_floating_ip_to_server(
            floating_ip['ip'], thing['id'])
        return floating_ip

    def create_timestamp(self, ip_address, dev_name=None, mount_path='/mnt',
                         private_key=None, server=None):
        ssh_client = self.get_remote_client(ip_address,
                                            private_key=private_key,
                                            server=server)
        if dev_name is not None:
            ssh_client.make_fs(dev_name)
            ssh_client.exec_command('sudo mount /dev/%s %s' % (dev_name,
                                                               mount_path))
        cmd_timestamp = 'sudo sh -c "date > %s/timestamp; sync"' % mount_path
        ssh_client.exec_command(cmd_timestamp)
        timestamp = ssh_client.exec_command('sudo cat %s/timestamp'
                                            % mount_path)
        if dev_name is not None:
            ssh_client.exec_command('sudo umount %s' % mount_path)
        return timestamp

    def get_timestamp(self, ip_address, dev_name=None, mount_path='/mnt',
                      private_key=None, server=None):
        ssh_client = self.get_remote_client(ip_address,
                                            private_key=private_key,
                                            server=server)
        if dev_name is not None:
            ssh_client.mount(dev_name, mount_path)
        timestamp = ssh_client.exec_command('sudo cat %s/timestamp'
                                            % mount_path)
        if dev_name is not None:
            ssh_client.exec_command('sudo umount %s' % mount_path)
        return timestamp

    def get_server_ip(self, server):
        """Get the server fixed or floating IP.

        Based on the configuration we're in, return a correct ip
        address for validating that a guest is up.
        """
        if CONF.validation.connect_method == 'floating':
            # The tests calling this method don't have a floating IP
            # and can't make use of the validation resources. So the
            # method is creating the floating IP there.
            return self.create_floating_ip(server)['ip']
        elif CONF.validation.connect_method == 'fixed':
            # Determine the network name to look for based on config or creds
            # provider network resources.
            if CONF.validation.network_for_ssh:
                addresses = server['addresses'][
                    CONF.validation.network_for_ssh]
            else:
                network = self.get_tenant_network()
                addresses = (server['addresses'][network['name']]
                             if network else [])
            for address in addresses:
                if (address['version'] == CONF.validation.ip_version_for_ssh and  # noqa
                        address['OS-EXT-IPS:type'] == 'fixed'):
                    return address['addr']
            raise exceptions.ServerUnreachable(server_id=server['id'])
        else:
            raise lib_exc.InvalidConfiguration()

    @classmethod
    def get_host_for_server(cls, server_id):
        server_details = cls.os_admin.servers_client.show_server(server_id)
        return server_details['server']['OS-EXT-SRV-ATTR:host']
