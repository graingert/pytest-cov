"""Coverage controllers for use by pytest-cov and nose-cov."""

import contextlib
import copy
import os
import random
import socket
import sys

import coverage
from coverage.data import CoverageData

from .embed import cleanup
from .compat import StringIO, workeroutput, workerinput


class _NullFile(object):
    @staticmethod
    def write(v):
        pass


@contextlib.contextmanager
def _backup(obj, attr):
    backup = getattr(obj, attr)
    try:
        setattr(obj, attr, copy.copy(backup))
        yield
    finally:
        setattr(obj, attr, backup)


class CovController(object):
    """Base class for different plugin implementations."""

    def __init__(self, cov_source, cov_report, cov_config, cov_append, cov_branch, config=None, nodeid=None):
        """Get some common config used by multiple derived classes."""
        self.cov_source = cov_source
        self.cov_report = cov_report
        self.cov_config = cov_config
        self.cov_append = cov_append
        self.cov_branch = cov_branch
        self.config = config
        self.nodeid = nodeid

        self.cov = None
        self.combining_cov = None
        self.data_file = None
        self.node_descs = set()
        self.failed_workers = []
        self.topdir = os.getcwd()

    def pause(self):
        self.cov.stop()
        self.unset_env()

    def resume(self):
        self.cov.start()
        self.set_env()

    def set_env(self):
        """Put info about coverage into the env so that subprocesses can activate coverage."""
        if self.cov_source is None:
            os.environ['COV_CORE_SOURCE'] = os.pathsep
        else:
            os.environ['COV_CORE_SOURCE'] = os.pathsep.join(self.cov_source)
        config_file = os.path.abspath(self.cov_config)
        if os.path.exists(config_file):
            os.environ['COV_CORE_CONFIG'] = config_file
        else:
            os.environ['COV_CORE_CONFIG'] = os.pathsep
        os.environ['COV_CORE_DATAFILE'] = os.path.abspath(self.cov.config.data_file)
        if self.cov_branch:
            os.environ['COV_CORE_BRANCH'] = 'enabled'

    @staticmethod
    def unset_env():
        """Remove coverage info from env."""
        os.environ.pop('COV_CORE_SOURCE', None)
        os.environ.pop('COV_CORE_CONFIG', None)
        os.environ.pop('COV_CORE_DATAFILE', None)
        os.environ.pop('COV_CORE_BRANCH', None)

    @staticmethod
    def get_node_desc(platform, version_info):
        """Return a description of this node."""

        return 'platform %s, python %s' % (platform, '%s.%s.%s-%s-%s' % version_info[:5])

    @staticmethod
    def sep(stream, s, txt):
        if hasattr(stream, 'sep'):
            stream.sep(s, txt)
        else:
            sep_total = max((70 - 2 - len(txt)), 2)
            sep_len = sep_total // 2
            sep_extra = sep_total % 2
            out = '%s %s %s\n' % (s * sep_len, txt, s * (sep_len + sep_extra))
            stream.write(out)

    def summary(self, cov_fail_under, stream):
        """Produce coverage reports."""

        if self.cov_report:
            # Output coverage section header.
            if len(self.node_descs) == 1:
                self.sep(stream, '-', 'coverage: %s' % ''.join(self.node_descs))
            else:
                self.sep(stream, '-', 'coverage')
                for node_desc in sorted(self.node_descs):
                    self.sep(stream, ' ', '%s' % node_desc)

        totals = self.cov.summary(self.cov_report, cov_fail_under, stream)

        if self.cov_report:
            # Report on any failed workers.
            if self.failed_workers:
                self.sep(stream, '-', 'coverage: failed workers')
                stream.write('The following workers failed to return coverage data, '
                             'ensure that pytest-cov is installed on these workers.\n')
                for node in self.failed_workers:
                    stream.write('%s\n' % node.gateway.id)

        return totals


class Central(CovController):
    """Implementation for centralised operation."""

    def start(self):
        cleanup()

        self.cov = coverage.Coverage(source=self.cov_source,
                                     branch=self.cov_branch,
                                     config_file=self.cov_config)
        self.combining_cov = coverage.Coverage(source=self.cov_source,
                                               branch=self.cov_branch,
                                               data_file=os.path.abspath(self.cov.config.data_file),
                                               config_file=self.cov_config)

        # Erase or load any previous coverage data and start coverage.
        if self.cov_append:
            self.cov.load()
        else:
            self.cov.erase()
        self.cov.start()
        self.set_env()

    def finish(self):
        """Stop coverage, save data to file and set the list of coverage objects to report on."""

        self.unset_env()
        self.cov.stop()
        self.cov.save()

        self.cov = self.combining_cov
        self.cov.load()
        self.cov.combine()
        self.cov.save()

        node_desc = self.get_node_desc(sys.platform, sys.version_info)
        self.node_descs.add(node_desc)


class DistMaster(CovController):
    """Implementation for distributed master."""

    def start(self):
        cleanup()

        # Ensure coverage rc file rsynced if appropriate.
        if self.cov_config and os.path.exists(self.cov_config):
            self.config.option.rsyncdir.append(self.cov_config)

        self.cov = coverage.Coverage(source=self.cov_source,
                                     branch=self.cov_branch,
                                     config_file=self.cov_config)
        self.combining_cov = coverage.Coverage(source=self.cov_source,
                                               branch=self.cov_branch,
                                               data_file=os.path.abspath(self.cov.config.data_file),
                                               config_file=self.cov_config)
        if self.cov_append:
            self.cov.load()
        else:
            self.cov.erase()
        self.cov.start()
        self.cov.config.paths['source'] = [self.topdir]

    def configure_node(self, node):
        """Workers need to know if they are collocated and what files have moved."""

        workerinput(node).update({
            'cov_master_host': socket.gethostname(),
            'cov_master_topdir': self.topdir,
            'cov_master_rsync_roots': [str(root) for root in node.nodemanager.roots],
        })

    def testnodedown(self, node, error):
        """Collect data file name from worker."""

        # If worker doesn't return any data then it is likely that this
        # plugin didn't get activated on the worker side.
        output = workeroutput(node, {})
        if 'cov_worker_node_id' not in output:
            self.failed_workers.append(node)
            return

        # If worker is not collocated then we must save the data file
        # that it returns to us.
        if 'cov_worker_data' in output:
            data_suffix = '%s.%s.%06d.%s' % (
                socket.gethostname(), os.getpid(),
                random.randint(0, 999999),
                output['cov_worker_node_id']
                )

            cov = coverage.Coverage(source=self.cov_source,
                                    branch=self.cov_branch,
                                    data_suffix=data_suffix,
                                    config_file=self.cov_config)
            cov.start()
            data = CoverageData()
            data.read_fileobj(StringIO(output['cov_worker_data']))
            cov.data.update(data)
            cov.stop()
            cov.save()
            path = output['cov_worker_path']
            self.cov.config.paths['source'].append(path)

        # Record the worker types that contribute to the data file.
        rinfo = node.gateway._rinfo()
        node_desc = self.get_node_desc(rinfo.platform, rinfo.version_info)
        self.node_descs.add(node_desc)

    def finish(self):
        """Combines coverage data and sets the list of coverage objects to report on."""

        # Combine all the suffix files into the data file.
        self.cov.stop()
        self.cov.save()
        self.cov = self.combining_cov
        self.cov.load()
        self.cov.combine()
        self.cov.save()


class DistWorker(CovController):
    """Implementation for distributed workers."""

    def start(self):
        cleanup()

        # Determine whether we are collocated with master.
        self.is_collocated = (socket.gethostname() == workerinput(self.config)['cov_master_host'] and
                              self.topdir == workerinput(self.config)['cov_master_topdir'])

        # If we are not collocated then rewrite master paths to worker paths.
        if not self.is_collocated:
            master_topdir = workerinput(self.config)['cov_master_topdir']
            worker_topdir = self.topdir
            if self.cov_source is not None:
                self.cov_source = [source.replace(master_topdir, worker_topdir)
                                   for source in self.cov_source]
            self.cov_config = self.cov_config.replace(master_topdir, worker_topdir)

        # Erase any previous data and start coverage.
        self.cov = coverage.Coverage(source=self.cov_source,
                                     branch=self.cov_branch,
                                     data_suffix=True,
                                     config_file=self.cov_config)
        if self.cov_append:
            self.cov.load()
        else:
            self.cov.erase()
        self.cov.start()
        self.set_env()

    def finish(self):
        """Stop coverage and send relevant info back to the master."""
        self.unset_env()
        self.cov.stop()

        if self.is_collocated:
            # We don't combine data if we're collocated - we can get
            # race conditions in the .combine() call (it's not atomic)
            # The data is going to be combined in the master.
            self.cov.save()

            # If we are collocated then just inform the master of our
            # data file to indicate that we have finished.
            workeroutput(self.config)['cov_worker_node_id'] = self.nodeid
        else:
            self.cov.combine()
            self.cov.save()
            # If we are not collocated then add the current path
            # and coverage data to the output so we can combine
            # it on the master node.

            # Send all the data to the master over the channel.
            buff = StringIO()
            self.cov.data.write_fileobj(buff)
            workeroutput(self.config).update({
                'cov_worker_path': self.topdir,
                'cov_worker_node_id': self.nodeid,
                'cov_worker_data': buff.getvalue(),
            })

    def summary(self, *args, **kwargs):
        """Only the master reports so do nothing."""

        pass
