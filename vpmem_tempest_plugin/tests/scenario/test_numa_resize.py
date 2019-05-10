# Copyright 2014 IBM Corp.
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

import testtools

from tempest.common import utils
from tempest.common import waiters
from tempest import config
from tempest.lib import decorators
from tempest.scenario import manager
from tempest.lib.common.utils import data_utils
from tempest.api.compute import base


CONF = config.CONF


class TestNumaResize(base.BaseV2ComputeAdminTest):
    """
    * resize an instance
    """
    @classmethod
    def skip_checks(cls):
        super(TestNumaResize, cls).skip_checks()
        if not utils.is_extension_enabled('OS-FLV-EXT-DATA', 'compute'):
                msg = "OS-FLV-EXT-DATA extension not enabled."
                raise cls.skipException(msg)
    
    @classmethod
    def resource_setup(cls):
        super(TestNumaResize,cls).resource_setup()
        flavor_name_1=data_utils.rand_name('test_flavor_1')
        ram_1=4096
        vcpus_1=4
        disk_1=10
        ephemeral_1=10
        flavor_id_1=data_utils.rand_int_id(start=1000)
        swap_1=1024
        rxtx_1=1
    
        flavor_name_2=data_utils.rand_name('test_flavor_2')
        ram_2=8192
        flavor_id_2=data_utils.rand_int_id(start=1000)


        cls.flavor_1=cls.admin_flavors_client.create_flavor(
                name=flavor_name_1,
                ram=ram_1,
                vcpus=vcpus_1,
                disk=disk_1,
                id=flavor_id_1,
                ephemeral=ephemeral_1,
                swap=swap_1,
                rxtx_factor=rxtx_1)['flavor']



        cls.flavor_2=cls.admin_flavors_client.create_flavor(
                name=flavor_name_2,
                ram=ram_2,
                vcpus=vcpus_1,
                disk=disk_1,
                id=flavor_id_2,
                ephemeral=ephemeral_1,
                swap=swap_1,
                rxtx_factor=rxtx_1)['flavor']

        cls.addClassResourceCleanup(
                    cls.admin_flavors_client.wait_for_resource_deletion,
                    cls.flavor_1['id'])

        cls.addClassResourceCleanup(
                    cls.admin_flavors_client.delete_flavor,
                    cls.flavor_1['id'])

        cls.addClassResourceCleanup(
                    cls.admin_flavors_client.wait_for_resource_deletion,
                    cls.flavor_2['id'])

        cls.addClassResourceCleanup(
                    cls.admin_flavors_client.delete_flavor,
                    cls.flavor_2['id'])


   # def _setup_server(self, keypair):
   #     security_groups = []
   #     if utils.is_extension_enabled('security-group', 'network'):
   #         security_group = self._create_security_group()
   #         security_groups = [{'name': security_group['name']}]
   #     network, _, _ = self.create_networks()
   #     server = self.create_server(
   #         networks=[{'uuid': network['id']}],
   #         key_name=keypair['name'],
   #         security_groups=security_groups)
   #     return server

   # def _setup_network(self, server, keypair):
   #     public_network_id = CONF.network.public_network_id
   #     floating_ip = self.create_floating_ip(server, public_network_id)
   #     # Verify that we can indeed connect to the server before we mess with
   #     # it's state
   #     self._wait_server_status_and_check_network_connectivity(
   #         server, keypair, floating_ip)

   #     return floating_ip

   # def _check_network_connectivity(self, server, keypair, floating_ip,
   #                                 should_connect=True):
   #     username = CONF.validation.image_ssh_user
   #     private_key = keypair['private_key']
   #     self.check_tenant_network_connectivity(
   #         server, username, private_key,
   #         should_connect=should_connect,
   #         servers_for_debug=[server])
   #     floating_ip_addr = floating_ip['floating_ip_address']
   #     # Check FloatingIP status before checking the connectivity
   #     self.check_floating_ip_status(floating_ip, 'ACTIVE')
   #     self.check_vm_connectivity(floating_ip_addr, username,
   #                                private_key, should_connect,
   #                                'Public network connectivity check failed',
   #                                server)

   # def _wait_server_status_and_check_network_connectivity(self, server,
   #                                                        keypair,
   #                                                        floating_ip):
   #     waiters.wait_for_server_status(self.servers_client, server['id'],
   #                                    'ACTIVE')
   #     self._check_network_connectivity(server, keypair, floating_ip)


    @decorators.idempotent_id('719eb59d-2f42-4b66-b8b1-bb1254473967')
    @testtools.skipUnless(CONF.compute_feature_enabled.resize,
                          'Resize is not available.')
    @decorators.attr(type='slow')
    @utils.services('compute', 'network')
    def test_numa_server_resize(self):
        #resize_flavor = CONF.compute.flavor_ref_alt
        #create flavor1 and flavor2
        specs_1 = {"hw:numa_nodes": "2", "hw:numa_cpus.0": "0,1", "hw:numa_mem.0": "2048", "hw:numa_cpus.1": "2,3", "hw:numa_mem.1": "2048"}
         # SET extra specs to the flavor created in setUp
        set_body = self.admin_flavors_client.set_flavor_extra_spec(self.flavor_1['id'], **specs_1)['extra_specs']
        self.assertEqual(set_body, specs_1)
         # GET extra specs and verify
        get_body = (self.admin_flavors_client.list_flavor_extra_specs(self.flavor_1['id'])['extra_specs'])
        self.assertEqual(get_body, specs_1)
        flavor_id_1=self.flavor_1['id']
        
        specs_2 = {"hw:numa_nodes": "2", "hw:numa_cpus.0": "0,1", "hw:numa_mem.0": "4096", "hw:numa_cpus.1": "2,3", "hw:numa_mem.1": "4096"}
        # SET extra specs to the flavor created in setUp
        set_body = self.admin_flavors_client.set_flavor_extra_spec(self.flavor_2['id'], **specs_2)['extra_specs']
        self.assertEqual(set_body, specs_2)
        # GET extra specs and verify
        get_body = (self.admin_flavors_client.list_flavor_extra_specs(self.flavor_2['id'])['extra_specs'])
        self.assertEqual(get_body, specs_2)
        flavor_id_2=self.flavor_2['id']


        image_id="bda4c1ab-7ce1-4b73-8ed7-c7e2c4414017"

        # keypair = self.create_keypair()
        #server = self._setup_server(keypair)
        #create server based on flavor1
        self.instance=self.servers_client.create_server(
                                name="new_server",
                                flavorRef=flavor_id_1,
                                imageRef=image_id)

        waiters.wait_for_server_status(self.servers_client, self.instance['server']['id'],
                                                       'ACTIVE')

        #floating_ip = self._setup_network(server, keypair)
        #resize server based on flavor2
        self.servers_client.resize_server(self.instance['server']['id'],
                                          flavor_ref=flavor_id_2)
        waiters.wait_for_server_status(self.servers_client, self.instance['server']['id'],
                                       'VERIFY_RESIZE')
        self.servers_client.confirm_resize_server(self.instance['server']['id'])
        

        #verify resize is successful
        server = self.servers_client.show_server(self.instance['server']['id'])['server']
        # Nova API > 2.46 no longer includes flavor.id, and schema check
        # will cover whether 'id' should be in flavor
        if server['flavor'].get('id'):
            self.assertEqual(flavor_id_2, server['flavor']['id'])
        else:
            flavor = self.flavors_client.show_flavor(resize_flavor)['flavor']
            for key in ['original_name', 'ram', 'vcpus', 'disk']:
                self.assertEqual(flavor[key], server['flavor'][key])
       
       
       
       # self._wait_server_status_and_check_network_connectivity(
       #     server, keypair, floating_ip)

