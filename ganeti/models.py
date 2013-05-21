#
# -*- coding: utf-8 -*- vim:fileencoding=utf-8:
# Copyright © 2010-2012 Greek Research and Technology Network (GRNET S.A.)
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND ISC DISCLAIMS ALL WARRANTIES WITH REGARD
# TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
# FITNESS. IN NO EVENT SHALL ISC BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT,
# OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF
# USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER
# TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE
# OF THIS SOFTWARE.

from django.db import models
from django.http import Http404
from django.core.cache import cache
from django.contrib.auth.models import User, Group
from ganetimgr.apply.models import Organization, InstanceApplication
from django.utils.translation import ugettext_lazy as _
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from datetime import datetime
from socket import gethostbyname
from time import sleep

from util import vapclient
from util.ganeti_client import GanetiRapiClient, GanetiApiError
from ganetimgr.settings import RAPI_CONNECT_TIMEOUT, RAPI_RESPONSE_TIMEOUT, GANETI_TAG_PREFIX
import re
try:
    from ganetimgr.settings import BEANSTALK_TUBE
except ImportError:
    BEANSTALK_TUBE = None

from util import beanstalkc

try:
    import json
except ImportError:
    import simplejson as json

class InstanceManager(object):
    def all(self):
        results = []
        users, orgs, groups, instanceapps = preload_instance_data()
        for cluster in Cluster.objects.all():
            results.extend(cluster.get_instances())
        return results

    def filter(self, **kwargs):
        users, orgs, groups, instanceapps = preload_instance_data()
        if 'cluster' in kwargs:
            if isinstance(kwargs['cluster'], Cluster):
                cluster = kwargs['cluster']
            else:
                try:
                    cluster = Cluster.object.get(slug=kwargs['cluster'])
                except:
                    return []
            results = cluster.get_instances()
            del kwargs['cluster']
        else:
            results = self.all()

        for arg, val in kwargs.items():
            if arg == 'user':
                if not isinstance(val, User):
                    try:
                        val = users[val]
                    except:
                        return []
                results = [result for result in results if val in result.users]
            elif arg == 'group':
                if not isinstance(val, Group):
                    try:
                        val = group[val]
                    except:
                        return []
                results = [result for result in results if val in result.groups]
            elif arg == 'name':
                results = [result for result in results if result.name == val]
            elif arg == 'name__icontains':
                valre = re.compile(val)
                results = [result for result in results if valre.search(result.name) is not None]
            else:
                results = [result for result in results
                           if '%s:%s' % (arg, val) in result.tags] 
        return results

    def get(self, **kwargs):
        results = self.filter(**kwargs)
        if len(results) == 1:
            return results[0]
        elif len(results) > 1:
            raise MultipleObjectsReturned("Multiple instances found")
        else:
            raise ObjectDoesNotExist("Could not find an instance")


class Instance(object):
    objects = InstanceManager()

    def __init__(self, cluster, name, info=None, listusers=None, listorganizations=None, listgroups=None, listinstanceapplications=None):
        self.cluster = cluster
        self.name = name
        self.listusers = listusers
        self.listorganizations = listorganizations
        self.listgroups = listgroups
        self.listinstanceapplications = listinstanceapplications
        self.organization = None
        self.application = None
        self.services = []
        self.users = []
        self.groups = []
        self._update(info)
        self.admin_view_only = False

    def _update(self, info=None):
        user_cache = {}
        group_cache = {}
        org_cache = {}
        app_cache = {}
        serv_cache = {}
        if not info:
            info = self.cluster.get_instance_info(self.name)
        for attr in info:
            self.__dict__[attr.replace(".", "_")] = info[attr]
        for tag in self.tags:
            group_pfx = "%s:group:" % GANETI_TAG_PREFIX
            user_pfx = "%s:user:" % GANETI_TAG_PREFIX
            org_pfx = "%s:org:" % GANETI_TAG_PREFIX
            app_pfx = "%s:application:" % GANETI_TAG_PREFIX
            serv_pfx = "%s:service:" % GANETI_TAG_PREFIX
            if tag.startswith(group_pfx):
                group = tag.replace(group_pfx,'')
                if group not in group_cache:
                    try:
                        g = self.listgroups[group]
                    except:
                        g = None
                    group_cache[group] = g
                if group_cache[group] is not None:
                    self.groups.append(group_cache[group])
            elif tag.startswith(user_pfx):
                user = tag.replace(user_pfx,'')
                if user not in user_cache:
                    try:
                        u = self.listusers[user]
                    except:
                        u = None
                    user_cache[user] = u
                if user_cache[user] is not None:
                    self.users.append(user_cache[user])
            elif tag.startswith(org_pfx):
                org = tag.replace(org_pfx,'')
                if org not in org_cache:
                    try:
                        o = self.listorganizations[org]
                    except:
                        o = None
                    org_cache[org] = o
                if org_cache[org] is not None:
                    self.organization = org_cache[org]
            elif tag.startswith(app_pfx):
                app = tag.replace(app_pfx,'')
                if app not in app_cache:
                    try:
                        a = self.listinstanceapplications[app]
                    except:
                        a = None
                    app_cache[app] = a
                if app_cache[app] is not None:
                    self.application = app_cache[app]
            elif tag.startswith(serv_pfx):
                serv = tag.replace(serv_pfx,'')
                if serv not in serv_cache:
                    serv_cache[serv] = serv
                if serv_cache[serv] is not None:
                    self.services.append(serv_cache[serv])
        if getattr(self, 'ctime', None):
            self.ctime = datetime.fromtimestamp(self.ctime)
        if getattr(self, 'mtime', None):
            self.mtime = datetime.fromtimestamp(self.mtime)

    def set_params(self, **kwargs):
        job_id = self.cluster._client.ModifyInstance(self.name, **kwargs)
        self.lock(reason=_("modifying"), job_id=job_id)

    def __repr__(self):
        return "<Instance: '%s'>" % self.name

    def __unicode__(self):
        return self.name

    def get_lock_key(self):
        return self.cluster._instance_lock_key(self.name)

    def lock(self, reason="locked", timeout=30, job_id=None):
        self.cluster._lock_instance(self.name, reason, timeout, job_id)

    def is_locked(self):
        return cache.get(self.get_lock_key())

    def set_admin_view_only_True(self):
        self.admin_view_only = True

class Cluster(models.Model):
    hostname = models.CharField(max_length=128)
    slug = models.SlugField(max_length=50)
    port = models.PositiveIntegerField(default=5080)
    description = models.CharField(max_length=128, blank=True, null=True)
    username = models.CharField(max_length=64, blank=True, null=True)
    password = models.CharField(max_length=64, blank=True, null=True)
    fast_create = models.BooleanField(default=False,
                                      verbose_name="Enable fast instance"
                                                   " creation",
                                      help_text="Allow fast instance creations"
                                                " on this cluster using the"
                                                " admin interface")
    default_disk_template = models.CharField(max_length=255, default="plain")

    class Meta:
        permissions = (
            ("view_instances", "Can view all instances"),
        )

    def __init__(self, *args, **kwargs):
        models.Model.__init__(self, *args, **kwargs)
        self._client = GanetiRapiClient(host=self.hostname,
                                        username=self.username,
                                        password=self.password)

    def __unicode__(self):
        return self.hostname

    def _instance_cache_key(self, instance):
        return "cluster:%s:instance:%s" % (self.slug, instance)

    def _instance_lock_key(self, instance):
        return "cluster:%s:instance:%s:lock" % (self.slug, instance)

    def _lock_instance(self, instance, reason="locked",
                       timeout=30, job_id=None):
        lock_key = self._instance_lock_key(instance)
        cache.set(lock_key, reason, timeout)
        if job_id is not None:
            b = None
            for i in range(5):
                try:
                    b = beanstalkc.Connection()
                    break
                except Exception, err:
                    sleep(1)
            if b is None:
                return

            if BEANSTALK_TUBE:
                b.use(BEANSTALK_TUBE)
            b.put(json.dumps({"type": "JOB_LOCK",
                              "cluster": self.slug,
                              "job_id": job_id,
                              "lock_key": lock_key,
                              "flush_keys": [self._instance_cache_key(instance)]}))

    @classmethod
    def get_all_instances(cls):
        instances = []
        for cluster in cls.objects.all():
            instances.extend(cluster.get_instances())
        return instances

    def get_instance(self, name):
        info = self.get_instance_info(name)
        users, orgs, groups, instanceapps = preload_instance_data()
        return Instance(self, info["name"], info, listusers = users, listorganizations = orgs, listgroups = groups, listinstanceapplications = instanceapps)

    def get_instance_or_404(self, name):
        try:
            return self.get_instance(name)
        except GanetiApiError, err:
            if err.code == 404:
                raise Http404()
            else:
                raise

    def get_instances(self):
        instances_min = []
        retinstances = []
        instances = cache.get("cluster:%s:instances" % self.slug)
        if instances is None:
            instances = self._client.GetInstances(bulk=True)
            cache.set("cluster:%s:instances" % self.slug, instances, 45)
        users, orgs, groups, instanceapps = preload_instance_data()
        retinstances = [Instance(self, info['name'], info, listusers = users, listorganizations = orgs, listgroups = groups, listinstanceapplications = instanceapps) for info in instances]
        return retinstances
    

    def get_user_instances(self, user):
        instances = self.get_instances()
        if user.is_superuser:
            return instances
        elif user.has_perm('ganeti.view_instances'):
            user = User.objects.filter(pk=user.pk).select_related('groups').get()
            ugroups = []
            for ug in user.groups.all():
                ugroups.append(ug.pk)
            user_inst = [i for i in instances if (user in i.users or
                    len(list(set(ugroups).intersection(set([g.id for g in i.groups]))))> 0)]
            other_inst = [i for i in instances if (i not in user_inst)]
            map(lambda i: i.set_admin_view_only_True(), other_inst)
            return user_inst + other_inst
        else:
            user = User.objects.filter(pk=user.pk).select_related('groups').get()
            ugroups = []
            for ug in user.groups.all():
                ugroups.append(ug.pk)
            return [i for i in instances if (user in i.users or
                   len(list(set(ugroups).intersection(set([g.id for g in i.groups]))))> 0)]

    def get_cluster_info(self):
        info = cache.get("cluster:%s:info" % self.slug)

        if info is None:
            info = self._client.GetInfo()
            if 'ctime' in info and info['ctime']:
                info['ctime'] = datetime.fromtimestamp(info['ctime'])
            if 'mtime' in info and info['mtime']:
                info['mtime'] = datetime.fromtimestamp(info['mtime'])
            cache.set("cluster:%s:info" % self.slug, info, 180)

        return info

    def get_cluster_nodes(self):
        return self._client.GetNodes()

    def get_cluster_instances(self):
        return self._client.GetInstances()

    def get_cluster_instances_detail(self):
        return self._client.GetInstances(bulk=True)

    def get_node_info(self, node):
        return self._client.GetNode(node)

    def get_instance_info(self, instance):
        cache_key = self._instance_cache_key(instance)
        info = cache.get(cache_key)

        if info is None:
            info = self._client.GetInstance(instance)
            cache.set(cache_key, info, 3)

        return info
        
    def setup_vnc_forwarding(self, instance):
        password = User.objects.make_random_password(length=8)
        info = self.get_instance_info(instance)

        port = info['network_port']
        node = info['pnode']
        node_ip = gethostbyname(node)
        res = vapclient.request_forwarding(0, node_ip, port, password)
        return (res["source_port"], password)

    def shutdown_instance(self, instance):
        cache_key = self._instance_cache_key(instance)
        cache.delete(cache_key)
        job_id = self._client.ShutdownInstance(instance)
        self._lock_instance(instance, reason=_("shutting down"), job_id=job_id)
        return job_id

    def startup_instance(self, instance):
        cache_key = self._instance_cache_key(instance)
        cache.delete(cache_key)
        job_id = self._client.StartupInstance(instance)
        self._lock_instance(instance, reason=_("starting up"), job_id=job_id)
        return job_id

    def reboot_instance(self, instance):
        cache_key = self._instance_cache_key(instance)
        cache.delete(cache_key)
        job_id = self._client.RebootInstance(instance)
        self._lock_instance(instance, reason=_("rebooting"), job_id=job_id)
        return job_id

    def create_instance(self, name=None, disk_template=None, disks=None,
                        nics=None, os=None, memory=None, vcpus=None, tags=None,
                        osparams=None):
        beparams = {}
        if memory is not None:
            beparams["memory"] = memory
        if vcpus is not None:
            beparams["vcpus"] = vcpus

        if nics is None:
            nics = []

        if tags is None:
            tags = []

        if osparams is None:
            osparams = {}

        if disk_template is None:
            disk_template = self.default_disk_template

        job_id = self._client.CreateInstance(mode="create", name=name, os=os,
                                             disk_template=disk_template,
                                             disks=disks, nics=nics,
                                             start=False, ip_check=False,
                                             name_check=False,
                                             beparams=beparams,
                                             tags=tags, osparams=osparams)

        self._lock_instance(name, reason=_("creating"), job_id=job_id)
        return job_id

    def get_job_status(self, job_id):
        return self._client.GetJobStatus(job_id)

    def get_default_network(self):
        try:
            return self.network_set.get(cluster_default=True)
        except Network.DoesNotExist:
            return None


class Network(models.Model):
    description = models.CharField(max_length=255)
    cluster = models.ForeignKey(Cluster)
    link = models.CharField(max_length=255)
    mode = models.CharField(max_length=64,
                            choices=(("bridged", "Bridged"),
                                     ("routed", "Routed")))
    cluster_default = models.BooleanField(default=False)
    groups = models.ManyToManyField(Group, blank=True, null=True)

    def __unicode__(self):
        return self.description

    def save(self, *args, **kwargs):
        # Ensure that only one cluster_default exists per cluster
        if self.cluster_default:
            try:
                other = Network.objects.get(cluster=self.cluster,
                                            cluster_default=True)
                if other != self:
                    other.cluster_default = False
                    other.save(*args, **kwargs)
            except Network.DoesNotExist:
                pass
        super(Network, self).save()



def preload_instance_data():
    users = cache.get('userlist')
    if not users:
        users = User.objects.all()
        userdict = {}
        for user in users:
            userdict[user.username] = user
        users = userdict
        cache.set('userlist', users, 30)
    orgs = cache.get('orgslist')
    if not orgs:
        orgs = Organization.objects.all()
        orgsdict = {}
        for org in orgs:
            orgsdict[org.tag] = org
        orgs = orgsdict
        cache.set('orgslist', orgs, 30)
    groups = cache.get('groupslist')
    if not groups:
        groups = Group.objects.all()
        groupsdict = {}
        for group in groups:
            groupsdict[group.name] = group
        groups = groupsdict
        cache.set('groupslist', groups, 30)
    instanceapps = cache.get('instaceapplist')
    if not instanceapps:
        instanceapps = InstanceApplication.objects.all()
        instappdict= {}
        for instapp in instanceapps:
            instappdict[str(instapp.pk)] = instapp
        instanceapps = instappdict
        cache.set('instaceapplist', instanceapps, 30)
    return users, orgs, groups, instanceapps

