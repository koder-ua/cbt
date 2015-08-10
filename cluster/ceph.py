import logging
import time
import threading
import uuid

from cluster import Cluster
import common
import settings
import monitoring


logger = logging.getLogger("cbt")


class Ceph(Cluster):
    def __init__(self, config):
        super(Ceph, self).__init__(config)
        self.ceph_osd_cmd = config.get('ceph-osd_cmd', '/usr/bin/ceph-osd')
        self.ceph_mon_cmd = config.get('ceph-mon_cmd', '/usr/bin/ceph-mon')
        self.ceph_rgw_cmd = config.get('ceph-rgw_cmd', '/usr/bin/radosgw')
        self.log_dir = config.get('log_dir', "%s/log" % self.tmp_dir)
        self.pid_dir = config.get('pid_dir', "%s/pid" % self.tmp_dir)
        self.core_dir = config.get('core_dir', "%s/core" % self.tmp_dir)
        self.monitoring_dir = "%s/monitoring" % self.tmp_dir
        self.keyring_fn = "%s/keyring" % self.tmp_dir
        self.osdmap_fn = "%s/osdmap" % self.tmp_dir
        self.monmap_fn = "%s/monmap" % self.tmp_dir
        self.use_existing = config.get('use_existing', True)

        # If making the cluster, use the ceph.conf file distributed by initialize to the tmp_dir
        self.tmp_conf = '%s/ceph.conf' % self.tmp_dir
        # If using an existing cluster, defualt to /etc/ceph/ceph.conf
        if self.use_existing:
            self.tmp_conf = '/etc/ceph/ceph.conf'

        self.osd_valgrind = config.get('osd_valgrind', None)
        self.mon_valgrind = config.get('mon_valgrind', None)
        self.rgw_valgrind = config.get('rgw_valgrind', None)
        self.tiering = config.get('tiering', False)
        self.ruleset_map = {}
        self.cur_ruleset = 1
        self.idle_duration = config.get('idle_duration', 0)
        self.use_existing = config.get('use_existing', True)
        self.stoprequest = threading.Event()
        self.haltrequest = threading.Event()

    def initialize(self):
        # safety check to make sure we don't blow away an existing cluster!
        if self.use_existing:
            raise RuntimeError('initialize was called on an ' +
                               'existing cluster! Avoiding touching anything.')

        super(Ceph, self).initialize()

        # unmount any kernel rbd volumes
        self.rbd_unmount()

        # shutdown any old processes
        self.shutdown()

        # Cleanup old junk and create new junk
        self.cleanup()
        common.mkdir_p(self.tmp_dir)

        all_ceph = settings.getnodes('clients', 'mons', 'osds', 'rgws', 'mds')
        all_ceph_head = settings.getnodes('head', 'clients', 'mons', 'osds', 'rgws', 'mds')

        common.pdsh(all_ceph_head, 'mkdir -p -m0755 -- %s' % self.tmp_dir).communicate()

        for path in (self.pid_dir, self.log_dir, self.monitoring_dir, self.core_dir):
            common.pdsh(all_ceph, 'mkdir -p -m0755 -- %s' % path).communicate()

        self.distribute_conf()

        # Set the core directory
        cmd = 'echo "%s/core.%%e.%%p.%%h.%%t" | sudo tee /proc/sys/kernel/core_pattern'
        common.pdsh(all_ceph, cmd % self.tmp_dir).communicate()

        # Create the filesystems
        self.setup_fs()

        # Build the cluster
        monitoring.start('%s/creation' % self.monitoring_dir)
        self.make_mons()
        self.make_osds()
        self.start_rgw()
        monitoring.stop()

        # Check Health
        monitoring.start('%s/initial_health_check' % self.monitoring_dir)
        self.check_health()
        monitoring.stop()

        # Wait for initial scrubbing to complete (This should only matter on pre-dumpling clusters)
        self.check_scrub()

        # Make the crush and erasure profiles
        self.make_profiles()

        # Peform Idle Monitoring
        if self.idle_duration > 0:
            monitoring.start("%s/idle_monitoring" % self.monitoring_dir)
            time.sleep(self.idle_duration)
            monitoring.stop()

        return True

    def shutdown(self):
        nodes = settings.getnodes('clients', 'osds', 'mons', 'rgws', 'mds')
        common.pdsh(nodes, 'sudo killall -9 massif-amd64-li').communicate()
        common.pdsh(nodes, 'sudo killall -9 memcheck-amd64-').communicate()
        common.pdsh(nodes, 'sudo killall -9 ceph-osd').communicate()
        common.pdsh(nodes, 'sudo killall -9 ceph-mon').communicate()
        common.pdsh(nodes, 'sudo killall -9 ceph-mds').communicate()
        common.pdsh(nodes, 'sudo killall -9 rados').communicate()
        common.pdsh(nodes, 'sudo killall -9 rest-bench').communicate()
        common.pdsh(nodes, 'sudo killall -9 radosgw').communicate()
        common.pdsh(nodes, 'sudo killall -9 radosgw-admin').communicate()
        common.pdsh(nodes, 'sudo /etc/init.d/apache2 stop').communicate()
        common.pdsh(nodes, 'sudo killall -9 pdsh').communicate()
        monitoring.stop()

    def cleanup(self):
        nodes = settings.getnodes('clients', 'osds', 'mons', 'rgws', 'mds')
        logger.info('Deleting %s', self.tmp_dir)
        common.pdsh(nodes, 'sudo rm -rf %s' % self.tmp_dir).communicate()

    def setup_fs(self):
        sc = settings.cluster
        fs = sc.get('fs')
        mkfs_opts = sc.get('mkfs_opts', '')
        mount_opts = sc.get('mount_opts', '')

        if fs == '':
            settings.shutdown("No OSD filesystem specified.  Exiting.")

        if 'osds_per_node' not in sc:
            raise ValueError("No osds_per_node parameter setup in config")

        osds = settings.getnodes('osds')
        for device in range(sc['osds_per_node']):
            cmds = [
                'sudo umount /dev/disk/by-partlabel/{odd}',
                'sudo rm -rf {mnt_dir}/{odd}',
                'sudo mkdir -p -m0755 -- {mnt_dir}/{odd}'
            ]

            # pdsh_osds('sudo umount /dev/disk/by-partlabel/osd-device-%s-data' % device)
            # pdsh_osds('sudo rm -rf %s/osd-device-%s-data' % (self.mnt_dir, device))
            # pdsh_osds('sudo mkdir -p -m0755 -- %s/osd-device-%s-data' % (self.mnt_dir, device))

            if fs == 'tmpfs':
                logger.info('using tmpfs osds, not creating a file system.')
            elif fs == 'zfs':
                logger.info('ruhoh, zfs detected.  No mkfs for you!')
                cmds += [
                    'sudo zpool destroy {odd}',
                    'sudo zpool create -f -O xattr=sa -m legacy {odd}/dev/disk/by-partlabel/{odd}',
                    'sudo zpool add {odd} log /dev/disk/by-partlabel/{odd}',
                    'sudo mount {mount_opts} -t zfs {odd} {mnt_dir}/{odd}'
                ]
            else:
                cmds += [
                    'sudo mkfs.{fs} {mkfs_opts} /dev/disk/by-partlabel/{odd}',
                    'sudo mount {mount_opts} -t {fs} /dev/disk/by-partlabel/{odd} {mnt_dir}/{odd}'
                ]

            for cmd in cmds:
                cmd = cmd.format(fs=fs, mkfs_opts=mkfs_opts,
                                 mount_opts=mount_opts, mnt_dir=self.mnt_dir,
                                 odd="osd-device-%s-data" % (device,))
                common.pdsh(osds, cmd).communicate()

    def distribute_conf(self):
        nodes = settings.getnodes('head', 'clients', 'osds', 'mons', 'rgws')
        conf_file = self.config["conf_file"]
        logger.info("Distributing %s.", conf_file)
        common.pdcp(nodes, '', conf_file, self.tmp_conf).communicate()
        common.pdsh(nodes, 'sudo mv /etc/ceph/ceph.conf /etc/ceph/ceph.conf.cbt.bak').communicate()
        common.pdsh(nodes, 'sudo ln -s %s /etc/ceph/ceph.conf' % self.tmp_conf).communicate()

    def make_mons(self):
        head_nodes = settings.getnodes('head')

        def pdsh_head(*params):
            common.pdsh(head_nodes, *params).communicate()

        # Build and distribute the keyring
        pdsh_head("ceph-authtool --create-keyring --gen-key --name=mon. %s --cap mon 'allow *'" % self.keyring_fn)
        pdsh_head("ceph-authtool --gen-key --name=client.admin --set-uid=0 " +
                  "--cap mon 'allow *' --cap osd 'allow *' --cap mds allow %s" % self.keyring_fn)

        common.rscp(head_nodes, self.keyring_fn, '%s.tmp' % self.keyring_fn).communicate()
        common.pdcp(settings.getnodes('mons', 'osds', 'rgws', 'mds'),
                    '', '%s.tmp' % self.keyring_fn, self.keyring_fn).communicate()

        # Build the monmap, retrieve it, and distribute it
        mons = settings.getnodes('mons').split(',')
        cmd = 'monmaptool --create --clobber'

        monhosts = settings.cluster.get('mons')
        logger.info(monhosts)

        for mons in monhosts.values():
            for mon, addr in mons.iteritems():
                cmd += ' --add %s %s' % (mon, addr)

        cmd = cmd + ' --print %s' % self.monmap_fn
        common.pdsh(settings.getnodes('head'), cmd).communicate()
        common.rscp(settings.getnodes('head'), self.monmap_fn, '%s.tmp' % self.monmap_fn).communicate()
        common.pdcp(settings.getnodes('mons'), '', '%s.tmp' % self.monmap_fn, self.monmap_fn).communicate()

        # Build the ceph-mons
        user = settings.cluster.get('user')
        for monhost, mons in monhosts.iteritems():
            if user:
                monhost = '%s@%s' % (user, monhost)

            def pdsh_monhost(*params):
                common.pdsh(monhost, *params).communicate()

            for mon, addr in mons.iteritems():
                pdsh_monhost('sudo rm -rf %s/mon.%s' % (self.tmp_dir, mon))
                pdsh_monhost('mkdir -p %s/mon.%s' % (self.tmp_dir, mon))
                pdsh_monhost('sudo sh -c "ulimit -c unlimited && exec %s --mkfs -c %s -i %s --monmap=%s --keyring=%s"' %
                             (self.ceph_mon_cmd, self.tmp_conf, mon, self.monmap_fn, self.keyring_fn))
                pdsh_monhost('cp %s %s/mon.%s/keyring' % (self.keyring_fn, self.tmp_dir, mon))

        # Start the mons
        for monhost, mons in monhosts.iteritems():

            if user:
                monhost = '%s@%s' % (user, monhost)

            for mon, addr in mons.iteritems():
                pidfile = "%s/%s.pid" % (self.pid_dir, monhost)

                cmd = 'sudo sh -c "ulimit -c unlimited && exec %s -c %s -i %s --keyring=%s --pid-file=%s"' % \
                      (self.ceph_mon_cmd, self.tmp_conf, mon, self.keyring_fn, pidfile)
                if self.mon_valgrind:
                    cmd = "%s %s" % (common.setup_valgrind(self.mon_valgrind, 'mon.%s' % monhost, self.tmp_dir), cmd)
                else:
                    cmd = 'ceph-run %s' % cmd
                common.pdsh(monhost, 'sudo %s' % cmd).communicate()

    def make_osds(self):
        osdnum = 0
        osdhosts = settings.cluster.get('osds')

        for host in osdhosts:
            user = settings.cluster.get('user')
            if user:
                pdshhost = '%s@%s' % (user, host)

            for i in range(settings.cluster['osds_per_node']):
                # Build the OSD
                osduuid = str(uuid.uuid4())
                key_fn = '%s/osd-device-%s-data/keyring' % (self.mnt_dir, i)
                cmds = [
                    "sudo ceph -c {conf} osd create {uuid}",
                    "sudo ceph -c {conf} osd crush add osd.{osd_num} 1.0 host={host} rack=localrack root=default",
                    'sudo sh -c "ulimit -n 16384 && ulimit -c unlimited &&' +
                    '   exec {cmd} -c {conf} -i {osd_num} --mkfs --mkkey --osd-uuid {uuid}"',
                    'sudo ceph -c {conf} -i {key_fn} auth add osd.{osd_num} ' +
                    '   osd "allow *" mon "allow profile osd"']

                for cmd in cmds:
                    cmd = cmd.format(conf=self.tmp_conf, uuid=osduuid,
                                     cmd=self.ceph_osd_cmd, key_fn=key_fn)
                    common.pdsh(pdshhost, cmd).communicate()

                # Start the OSD
                pidfile = "%s/ceph-osd.%d.pid" % (self.pid_dir, osdnum)
                cmd = '%s -c %s -i %d --pid-file=%s' % (self.ceph_osd_cmd, self.tmp_conf, osdnum, pidfile)
                if self.osd_valgrind:
                    cmd = "%s %s" % (common.setup_valgrind(self.osd_valgrind, 'osd.%d' % osdnum, self.tmp_dir), cmd)
                else:
                    cmd = 'ceph-run %s' % cmd

                templ = 'sudo sh -c "ulimit -n 16384 && ulimit -c unlimited && exec %s"'
                common.pdsh(pdshhost, templ % cmd).communicate()
                osdnum = osdnum+1

    def start_rgw(self):
        rgwhosts = settings.cluster.get('rgws', [])

        for host in rgwhosts:
            user = settings.cluster.get('user')
            pdshhost = '%s@%s' % (user, host) if user else host

            cmd = '%s -c %s -n client.radosgw.gateway --log-file=%s/rgw.log' % \
                  (self.ceph_rgw_cmd, self.tmp_conf, self.log_dir)

            if self.rgw_valgrind:
                cmd = "%s %s" % (common.setup_valgrind(self.rgw_valgrind, 'rgw.%s' % host, self.tmp_dir), cmd)
            else:
                cmd = 'ceph-run %s' % cmd

            common.pdsh(pdshhost, 'sudo sh -c "ulimit -n 16384 && ulimit -c unlimited && exec %s"' % cmd).communicate()

    def check_health(self, check_list=None, logfile=None):
        logline = "| tee -a %s" % logfile if logfile else ""
        ret = 0

        # Match any of these things to continue checking health
        check_list = ["degraded", "peering", "recovery_wait", "stuck", "inactive", "unclean", "recovery", "stale"]
        while True:
            health_proc = common.pdsh(settings.getnodes('head'),
                                      'ceph -c %s health %s' % (self.tmp_conf, logline))
            stdout, stderr = health_proc.communicate()

            if check_list and not set(check_list).intersection(stdout.split()):
                break

            if "HEALTH_OK" in stdout:
                break
            else:
                ret = ret + 1

            logger.info("%s", stdout)
            time.sleep(1)

        return ret

    def check_scrub(self):
        logger.info('Waiting until Scrubbing completes...')
        cmd = 'ceph -c %s pg dump | cut -f 16 | grep "0.000000" | wc -l' % self.tmp_conf
        while True:
            stdout, stderr = common.pdsh(settings.getnodes('head'), cmd).communicate()
            if " 0\n" in stdout:
                break
            else:
                logger.info(stdout)
            time.sleep(1)

    def dump_config(self, run_dir):
        cmd = 'sudo ceph -c %s --admin-daemon /var/run/ceph/ceph-osd.0.asok config show > %s/ceph_settings.out' % \
              (self.tmp_conf, run_dir)
        common.pdsh(settings.getnodes('osds'), cmd).communicate()

    def dump_historic_ops(self, run_dir):
        cmd = ('find /var/run/ceph/*.asok -maxdepth 1 -exec ' +
               'sudo ceph --admin-daemon {} dump_historic_ops \; > %s/historic_ops.out') % run_dir
        common.pdsh(settings.getnodes('osds'), cmd).communicate()

    def set_osd_param(self, param, value):
        cmd = ('find /dev/disk/by-partlabel/osd-device-*data -exec readlink {} \; |' +
               ' cut -d"/" -f 3 | sed "s/[0-9]$//" | ' +
               'xargs -I{} sudo sh -c "echo %s > /sys/block/\'{}\'/queue/%s"') % (value, param)
        common.pdsh(settings.getnodes('osds'), cmd)

    def __str__(self):
        return "foo"

    def create_recovery_test(self, run_dir, callback):
        rt_config = self.config.get("recovery_test", {})
        rt_config['run_dir'] = run_dir
        self.rt = RecoveryTestThread(rt_config, self, callback, self.stoprequest, self.haltrequest)
        self.rt.start()

    def wait_recovery_done(self):
        self.stoprequest.set()
        while True:
            threads = threading.enumerate()
            if len(threads) == 1:
                break
            self.rt.join(1)

    # FIXME: This is a total hack that assumes there is only 1 existing ruleset!
    # Will change pending a fix for http://tracker.ceph.com/issues/8060
    def set_ruleset(self, name):
        name = str(name)
        if name in self.ruleset_map:
            raise Exception('A rule named %s already exists!' % name)
        self.ruleset_map[name] = self.cur_ruleset
        self.cur_ruleset += 1

    def get_ruleset(self, name):
        name = str(name)
        logger.info("%s", self.ruleset_map)
        return self.ruleset_map[name]

    def make_profiles(self):
        crush_profiles = self.config.get('crush_profiles', {})
        heads = settings.getnodes('head')

        def pdsg_heads(cmd):
            common.pdsh(heads, cmd).communicate()

        cmds = [
            'ceph -c {cfg} osd crush add-bucket {name}-root root',
            'ceph -c {cfg} osd crush add-bucket {name}-root root',
            'ceph -c {cfg} osd crush add-bucket {name}-rack rack',
            'ceph -c {cfg} osd crush move {name}-rack root={name}-root',
            'ceph -c {cfg} osd crush add-bucket {name}-host host',
            'ceph -c {cfg} osd crush move {name}-host rack={name}-rack']

        for name, profile in crush_profiles.items():
            osds = profile.get('osds', None)
            if not osds:
                raise Exception("No OSDs defined for crush profile, bailing!")

            # FIXME: We need to build a dict mapping OSDs to hosts and create a proper hierarchy!
            for cmd in cmds:
                pdsg_heads(cmd.format(cfg=self.tmp_conf, name=name))

            for i in osds:
                pdsg_heads('ceph -c %s osd crush set %s 1.0 host=%s-host' % (self.tmp_conf, i, name))
            pdsg_heads('ceph -c %s osd crush rule create-simple %s %s-root osd' % (self.tmp_conf, name, name))
            self.set_ruleset(name)

        erasure_profiles = self.config.get('erasure_profiles', {})
        for name, profile in erasure_profiles.items():
            cmd = 'ceph -c %s osd erasure-code-profile set %s ruleset-failure-domain=osd k=%s m=%s'
            pdsg_heads(cmd % (self.tmp_conf,
                              name,
                              profile.get('erasure_k', 6),
                              profile.get('erasure_m', 2)))
            self.set_ruleset(name)

    def mkpool(self, name, profile_name, base_name=None):
        pool_profiles = self.config.get('pool_profiles', {'default': {}})
        profile = pool_profiles.get(profile_name, {})

        pg_size = profile.get('pg_size', 1024)
        pgp_size = profile.get('pgp_size', 1024)
        erasure_profile = profile.get('erasure_profile', '')
        replication = str(profile.get('replication', None))
        cache_profile = profile.get('cache_profile', None)
        crush_profile = profile.get('crush_profile', None)
        cache_mode = profile.get('cache_mode', None)
        hit_set_type = profile.get('hit_set_type', None)
        hit_set_count = profile.get('hit_set_count', None)
        hit_set_period = profile.get('hit_set_period', None)
        target_max_objects = profile.get('target_max_objects', None)
        target_max_bytes = profile.get('target_max_bytes', None)
        min_read_recency_for_promote = profile.get('min_read_recency_for_promote', None)

#        common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool delete %s %s --yes-i-really-really-mean-it' % (self.tmp_conf, name, name)).communicate()
        common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool create %s %d %d %s' % (self.tmp_conf, name, pg_size, pgp_size, erasure_profile)).communicate()

        if replication and replication == 'erasure':
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool create %s %d %d erasure %s' % (self.tmp_conf, name, pg_size, pgp_size, erasure_profile)).communicate()
        else:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool create %s %d %d' % (self.tmp_conf, name, pg_size, pgp_size)).communicate()

        logger.info('Checking Healh after pool creation.')
        self.check_health()

        if replication and replication.isdigit():
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s size %s' % (self.tmp_conf, name, replication)).communicate()
            logger.info('Checking Health after setting pool replication level.')
            self.check_health()

        if base_name and cache_mode:
            logger.info("Adding %s as cache tier for %s.", name, base_name)
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier add %s %s' % (self.tmp_conf, base_name, name)).communicate()
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier cache-mode %s %s' % (self.tmp_conf, name, cache_mode)).communicate()
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier set-overlay %s %s' % (self.tmp_conf, base_name, name)).communicate()

        if crush_profile:
            ruleset = self.get_ruleset(crush_profile)
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s crush_ruleset %s' % (self.tmp_conf, name, ruleset)).communicate()
        if hit_set_type:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s hit_set_type %s' % (self.tmp_conf, name, hit_set_type)).communicate()
        if hit_set_count:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s hit_set_count %s' % (self.tmp_conf, name, hit_set_count)).communicate()
        if hit_set_period:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s hit_set_period %s' % (self.tmp_conf, name, hit_set_period)).communicate()
        if target_max_objects:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s target_max_objects %s' % (self.tmp_conf, name, target_max_objects)).communicate()
        if target_max_bytes:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s target_max_bytes %s' % (self.tmp_conf, name, target_max_bytes)).communicate()
        if min_read_recency_for_promote:
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool set %s min_read_recency_for_promote %s' % (self.tmp_conf, name, min_read_recency_for_promote)).communicate()
        logger.info('Final Pool Health Check.')
        self.check_health()

        # If there is a cache profile assigned, make a cache pool
        if cache_profile:
            cache_name = '%s-cache' % name
            self.mkpool(cache_name, cache_profile, name)

    def rmpool(self, name, profile_name):
        pool_profiles = self.config.get('pool_profiles', {'default': {}})
        profile = pool_profiles.get(profile_name, {})
        cache_profile = profile.get('cache_profile', None)
        if cache_profile:
            cache_name = '%s-cache' % name

            # flush and remove the overlay and such
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier cache-mode %s forward' % (self.tmp_conf, cache_name)).communicate()
            common.pdsh(settings.getnodes('head'), 'sudo rados -c %s -p %s cache-flush-evict-all' % (self.tmp_conf, cache_name)).communicate()
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier remove-overlay %s' % (self.tmp_conf, name)).communicate()
            common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd tier remove %s %s' % (self.tmp_conf, name, cache_name)).communicate()

            # delete the cache pool
            self.rmpool(cache_name, cache_profile)
        common.pdsh(settings.getnodes('head'), 'sudo ceph -c %s osd pool delete %s %s --yes-i-really-really-mean-it' % (self.tmp_conf, name, name)).communicate()

    def rbd_unmount(self):
        common.pdsh(settings.getnodes('clients'), 'sudo find /dev/rbd* -maxdepth 0 -type b -exec umount \'{}\' \;').communicate()
#        common.pdsh(settings.getnodes('clients'), 'sudo find /dev/rbd* -maxdepth 0 -type b -exec rbd -c %s unmap \'{}\' \;' % self.tmp_conf).communicate()
        common.pdsh(settings.getnodes('clients'), 'sudo service rbdmap stop').communicate()
class RecoveryTestThread(threading.Thread):
    def __init__(self, config, cluster, callback, stoprequest, haltrequest):
        threading.Thread.__init__(self)
        self.config = config
        self.cluster = cluster
        self.callback = callback
        self.state = 'pre'
        self.states = {'pre': self.pre, 'markdown': self.markdown, 'osdout': self.osdout, 'osdin':self.osdin, 'post':self.post, 'done':self.done}
        self.stoprequest = stoprequest
        self.haltrequest = haltrequest
        self.outhealthtries = 0
        self.inhealthtries = 0
        self.maxhealthtries = 60
        self.health_checklist = ["degraded", "peering", "recovery_wait", "stuck", "inactive", "unclean", "recovery"]

    def logcmd(self, message):
        return 'echo "[`date`] %s" >> %s/recovery.log' % (message, self.config.get('run_dir'))

    def pre(self):
        pre_time = self.config.get("pre_time", 60)
        common.pdsh(settings.getnodes('head'), self.logcmd('Starting Recovery Test Thread, waiting %s seconds.' % pre_time)).communicate()
        time.sleep(pre_time)
        lcmd = self.logcmd("Setting the ceph osd noup flag")
        common.pdsh(settings.getnodes('head'), 'ceph -c %s ceph osd set noup;%s' % (self.cluster.tmp_conf, lcmd)).communicate()
        self.state = 'markdown'

    def markdown(self):
        for osdnum in self.config.get('osds'):
            lcmd = self.logcmd("Marking OSD %s down." % osdnum)
            common.pdsh(settings.getnodes('head'), 'ceph -c %s osd down %s;%s' % (self.cluster.tmp_conf, osdnum, lcmd)).communicate()
            lcmd = self.logcmd("Marking OSD %s out." % osdnum)
            common.pdsh(settings.getnodes('head'), 'ceph -c %s osd out %s;%s' % (self.cluster.tmp_conf, osdnum, lcmd)).communicate()
        common.pdsh(settings.getnodes('head'), self.logcmd('Waiting for the cluster to break and heal')).communicate()

        self.state = 'osdout'

    def osdout(self):
        ret = self.cluster.check_health(self.health_checklist, "%s/recovery.log" % self.config.get('run_dir'))
        common.pdsh(settings.getnodes('head'), self.logcmd("ret: %s" % ret)).communicate()

        if self.outhealthtries < self.maxhealthtries and ret == 0:
            self.outhealthtries = self.outhealthtries + 1
            return # Cluster hasn't become unhealthy yet.

        if ret == 0:
            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster never went unhealthy.')).communicate()
        else:
            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster appears to have healed.')).communicate()

        lcmd = self.logcmd("Unsetting the ceph osd noup flag")
        common.pdsh(settings.getnodes('head'), 'ceph -c %s ceph osd unset noup;%s' % (self.cluster.tmp_conf, lcmd)).communicate()
        for osdnum in self.config.get('osds'):
            lcmd = self.logcmd("Marking OSD %s up." % osdnum)
            common.pdsh(settings.getnodes('head'), 'ceph -c %s osd up %s;%s' % (self.cluster.tmp_conf, osdnum, lcmd)).communicate()
            lcmd = self.logcmd("Marking OSD %s in." % osdnum)
            common.pdsh(settings.getnodes('head'), 'ceph -c %s osd in %s;%s' % (self.cluster.tmp_conf, osdnum, lcmd)).communicate()

        self.state = "osdin"

    def osdin(self):
        # Wait until the cluster is healthy.
        ret = self.cluster.check_health(self.health_checklist, "%s/recovery.log" % self.config.get('run_dir'))
        if self.inhealthtries < self.maxhealthtries and ret == 0:
            self.inhealthtries = self.inhealthtries + 1
            return # Cluster hasn't become unhealthy yet.

        if ret == 0:
            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster never went unhealthy.')).communicate()
        else:
            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster appears to have healed.')).communicate()
        self.state = "post"

    def post(self):
        if self.stoprequest.isSet():
            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster is healthy, but stoprequest is set, finishing now.')).communicate()
            self.haltrequest.set()
            return

        if self.config.get("repeat", False):
            # reset counters
            self.outhealthtries = 0
            self.inhealthtries = 0

            common.pdsh(settings.getnodes('head'), self.logcmd('Cluster is healthy, but repeat is set.  Moving to "markdown" state.')).communicate()
            self.state = "markdown"
            return

        post_time = self.config.get("post_time", 60)
        common.pdsh(settings.getnodes('head'), self.logcmd('Cluster is healthy, completion in %s seconds.' % post_time)).communicate()
        time.sleep(post_time)
        self.state = "done"

    def done(self):
        common.pdsh(settings.getnodes('head'), self.logcmd("Done.  Calling parent callback function.")).communicate()
        self.callback()
        self.haltrequest.set()

    def join(self, timeout=None):
        common.pdsh(settings.getnodes('head'), self.logcmd('Received notification that parent is finished and waiting.')).communicate()
        super(RecoveryTestThread, self).join(timeout)

    def run(self):
        self.haltrequest.clear()
        self.stoprequest.clear()
        while not self.haltrequest.isSet():
          self.states[self.state]()
        common.pdsh(settings.getnodes('head'), self.logcmd('Exiting recovery test thread.  Last state was: %s' % self.state)).communicate()

