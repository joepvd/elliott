"""
Utility functions for general interactions with Brew and Builds
"""
from __future__ import absolute_import, print_function, unicode_literals
from future.utils import as_native_str
# stdlib
import ast
import time
import datetime
import subprocess
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing import cpu_count
import shlex
import ssl

# ours
from . import constants
from . import exectools
from . import logutil
from elliottlib import exceptions

# 3rd party
import click
import requests
from requests_kerberos import HTTPKerberosAuth

logger = logutil.getLogger(__name__)


def get_latest_builds(tag, component_names, brew_session=koji.ClientSession(constants.BREW_HUB)):
    latest_builds = {}
    component_names = set(component_names)
    builds = brew_session.getLatestBuilds(tag)
    for b in builds:
        if b['name'] in component_names:
            latest_builds[b['name']] = b
    return latest_builds


def get_brew_build(nvr, product_version='', session=None):
    """5.2.2.1. GET /api/v1/build/{id_or_nvr}

    Get Brew build details.

    https://errata.devel.redhat.com/developer-guide/api-http-api.html#api-get-apiv1buildid_or_nvr

    :param str nvr: A name-version-release string of a brew rpm/image build
    :param str product_version: The product version tag as given to ET
    when attaching a build
    :param requests.Session session: A python-requests Session object,
    used for for connection pooling. Providing `session` object can
    yield a significant reduction in total query time when looking up
    many builds.

    http://docs.python-requests.org/en/master/user/advanced/#session-objects

    :return: An initialized Build object with the build details
    :raises exceptions.BrewBuildException: When build not found

    """
    if session is not None:
        res = session.get(constants.errata_get_build_url.format(id=nvr),
                          verify=ssl.get_default_verify_paths().openssl_cafile,
                          auth=HTTPKerberosAuth())
    else:
        res = requests.get(constants.errata_get_build_url.format(id=nvr),
                           verify=ssl.get_default_verify_paths().openssl_cafile,
                           auth=HTTPKerberosAuth())
    if res.status_code == 200:
        return Build(nvr=nvr, body=res.json(), product_version=product_version)
    else:
        raise exceptions.BrewBuildException("{build}: {msg}".format(
            build=nvr,
            msg=res.text))


def find_unshipped_build_candidates(base_tag, product_version, kind='rpm'):
    """Find builds for a product and return a list of the builds only
    labeled with the -candidate tag that aren't attached to any open
    advisory.

    :param str base_tag: The tag to search for shipped/candidate
    builds. This is combined with '-candidate' to return the build
    difference.
    :param str product_version: The product version tag as given to ET
    when attaching a build
    :param str kind: Search for RPM builds by default. 'image' is also
    acceptable

    For example, if `base_tag` is 'rhaos-3.7-rhel7' then this will
    look for two sets of tagged builds:

    (1) 'rhaos-3.7-rhel7'
    (2) 'rhaos-3.7-rhel7-candidate'

    :return: A set of build strings where each build is only tagged as
    a -candidate build
    """
    if kind == 'rpm':
        candidate_builds = BrewTaggedRPMBuilds(base_tag + "-candidate")
        shipped_builds = BrewTaggedRPMBuilds(base_tag)
    elif kind == 'image':
        candidate_builds = BrewTaggedImageBuilds(base_tag + "-candidate")
        shipped_builds = BrewTaggedImageBuilds(base_tag)

    # Multiprocessing may seem overkill, but these queries can take
    # longer than you'd like
    pool = ThreadPool(cpu_count())
    pool.map(
        lambda builds: builds.refresh(),
        [candidate_builds, shipped_builds])
    # Wait for results
    pool.close()
    pool.join()

    # Builds only tagged with -candidate (not shipped yet)
    return candidate_builds.builds.difference(shipped_builds.builds)


def get_brew_buildinfo(build):
    """Get the buildinfo of a brew build from brew.

    :param str|int build: A build NVR or numeric ID

    :return dict buildinfo: A dict of the build info. Certain fields
    may be transformed such as 'Tags' turned into a list, and 'Extra'
    into a proper dict object instead of a JSON string

    :return str buildinfo: The raw unparsed buildinfo as a string

Note: This is different from get_brew_build in that this function
queries brew directly using the 'brew buildinfo' command. Whereas,
get_brew_build queries the Errata Tool API for other information.

This function will give information not provided by ET: build tags,
finished date, built by, etc.
    """
    query_string = "brew buildinfo {nvr}".format(nvr=build.nvr)
    rc, stdout, stderr = exectools.cmd_gather(shlex.split(query_string))
    buildinfo = {}

    as_string = "???"  # F821 undefined name 'as_string'
    if as_string and rc == 0:
        return stdout
    else:
        buildinfo = {}

        # These keys indicate content which can be whitespace split into
        # an array, ex: the value of the "Tags" key comes as a string, but
        # we can split it on space characters and have a list of tags
        # instead.
        split_keys = ['Tags']
        # The value of these keys contain json strings which we can
        # attempt to load into a datastructure
        json_keys = ['Extra']
        for line in stdout.splitlines():
            key, token, rest = line.partition(': ')
            if key in split_keys:
                buildinfo[key] = rest.split(' ')
                continue
            elif key in json_keys:
                try:
                    # Why would we use the ast module for this? Is
                    # json.loads not enough? Actually, no, it isn't
                    # enough. The <airquotes>JSON</airquotes> returned
                    # from 'brew buildinfo' uses single-quotes to wrap
                    # strings, that is not valid json. Whereas,
                    # ast.literal_eval() can handle that and load the
                    # string into a structure we can work with
                    buildinfo[key] = ast.literal_eval(rest)
                except Exception:
                    buildinfo[key] = rest
                    continue
            else:
                buildinfo[key] = rest
        return buildinfo


def get_tagged_image_builds(tag, latest=True):
    """Wrapper around shelling out to run 'brew list-tagged' for a given tag.

    :param str tag: The tag to list builds from
    :param bool latest: Only show the single latest build of a package
    """
    if latest:
        latest_option = '--latest'
    else:
        latest_option = ''

    query_string = "brew list-tagged {tag} {latest} --type=image --quiet".format(tag=tag, latest=latest_option)
    # --latest - Only the last build for that package
    # --type=image - Only show container images builds
    # --quiet - Omit field headers in output

    return exectools.cmd_gather(shlex.split(query_string))


def get_tagged_rpm_builds(tag, arch='src', latest=True):
    """Wrapper around shelling out to run 'brew list-tagged' for a given tag.

    :param str tag: The tag to list builds from
    :param str arch: Filter results to only this architecture
    :param bool latest: Only show the single latest build of a package
    """
    if latest is True:
        latest_flag = "--latest"
    else:
        latest_flag = ""

    query_string = "brew list-tagged {tag} {latest} --rpm --quiet --arch {arch}".format(tag=tag, latest=latest_flag, arch=arch)
    # --latest - Only the last build for that package
    # --rpm - Only show RPM builds
    # --quiet - Omit field headers in output
    # --arch {arch} - Only show builds of this architecture

    return exectools.cmd_gather(shlex.split(query_string))

# ============================================================================
# Brew object interaction models
# ============================================================================


class BrewTaggedImageBuilds(object):
    """
    Abstraction around working with lists of brew tagged image
    builds. Ensures the result set is formatted correctly for this
    build type.
    """

    def __init__(self, tag):
        self.tag = tag
        self.builds = set([])

    def refresh(self):
        """Refresh or build initial list of brew builds

        :return: True if builds could be found for the given tag

        :raises: Exception if there is an error looking up builds
        """
        rc, stdout, stderr = get_tagged_image_builds(self.tag)

        print("Refreshing for tag: {tag}".format(tag=self.tag))

        if rc != 0:
            raise exceptions.BrewBuildException("Failed to get brew builds for tag: {tag} - {err}".format(tag=self.tag, err=stderr))
        else:
            builds = set(stdout.splitlines())
            for b in builds:
                self.builds.add(b.split()[0])

        return True


class BrewTaggedRPMBuilds(object):
    """
    Abstraction around working with lists of brew tagged rpm
    builds. Ensures the result set is formatted correctly for this
    build type.
    """

    def __init__(self, tag):
        self.tag = tag
        self.builds = set([])

    def refresh(self):
        """Refresh or build initial list of brew builds

        :return: True if builds could be found for the given tag

        :raises: Exception if there is an error looking up builds
        """
        rc, stdout, stderr = get_tagged_rpm_builds(self.tag)

        print("Refreshing for tag: {tag}".format(tag=self.tag))

        if rc != 0:
            raise exceptions.BrewBuildException("Failed to get brew builds for tag: {tag} - {err}".format(tag=self.tag, err=stderr))
        else:
            builds = set(stdout.splitlines())
            for b in builds:
                # The results come back with the build arch (.src)
                # appended. Remove that if it is in the string.
                try:
                    self.builds.add(b[:b.index('.src')])
                except ValueError:
                    # Raised if the given substring is not found
                    self.builds.add(b)

        return True


class Build(object):
    """An existing brew build

How might you use this object? Great question. I'd start by fetching
the details of a known build from the Errata Tool using the
/api/v1/build/{id_or_nvr} API endpoint. Then take that build NVR or ID
and the build object from the API and initialize a new Build object
from those.

Save yourself some time and use the brew.get_brew_build()
function. Give it an NVR or a build ID and it will give you an
initialized Build object (provided the build exists).

    """

    def __init__(self, nvr=None, body={}, product_version=''):
        """Model for a brew build.

        :param str nvr: Name-Version-Release (or build ID) of a brew build

        :param dict body: An object as one gets from the errata tool
        /api/v1/build/{id_or_nvr} REST endpoint. See also:
        get_brew_build() (above)

        :param str product_version: The tag (from Errata Tool) of the
        product this build will be attached to, for example:
        "RHEL-7-OSE-3.9". This is only useful when representing this
        object as an item that would be given to the Errata Tool API
        add_builds endpoint (see: Build.to_json()).
        """
        self.nvr = nvr
        self.body = body
        self.all_errata = []
        self.kind = ''
        self.path = ''
        self.attached_erratum_ids = set([])
        self.attached_closed_erratum_ids = set([])
        self.product_version = product_version
        self.buildinfo = {}
        self.process()

    @as_native_str()
    def __str__(self):
        return self.nvr

    @as_native_str()
    def __repr__(self):
        return "Build({nvr})".format(nvr=self.nvr)

    # Set addition
    def __eq__(self, other):
        return self.nvr == other.nvr

    # Set addition
    def __ne__(self, other):
        return self.nvr != other.nvr

    # List sorting
    def __gt__(self, other):
        return self.nvr > other.nvr

    # List sorting
    def __lt__(self, other):
        return self.nvr < other.nvr

    @property
    def open_erratum(self):
        """Any open erratum this build is attached to"""
        return [e for e in self.all_errata if e['status'] in constants.errata_active_advisory_labels]

    @property
    def open_errata_id(self):
        """Any open erratum this build is attached to"""
        return [e['id'] for e in self.all_errata if e['status'] in constants.errata_active_advisory_labels]

    @property
    def attached_to_open_erratum(self):
        """Attached to any open erratum"""
        return len(self.open_erratum) > 0

    @property
    def closed_erratum(self):
        """Any closed erratum this build is attached to"""
        return [e for e in self.all_errata if e['status'] in constants.errata_inactive_advisory_labels]

    @property
    def attached_to_closed_erratum(self):
        """Attached to any closed erratum"""
        return len(self.closed_erratum) > 0

    @property
    def attached(self):
        """Attached to ANY erratum (open or closed)"""
        return len(self.all_errata) > 0

    def process(self):
        """Generate some easy to access attributes about this build so we
don't have to do extra manipulation later back in the view"""
        # Has this build been attached to any erratum?
        self.all_errata = self.body.get('all_errata', [])

        # What kind of build is this?
        if 'files' in self.body:
            # All of the files are provided. What we're trying to do
            # is figure out if this build classifies as one of the
            # kind of builds we work with: RPM builds and Container
            # Image builds.
            #
            # We decide opportunistically, hence the abrupt
            # breaks. This decision process may require tweaking in
            # the future.
            #
            # I've only ever seen OSE image builds having 1 item (a
            # tar file) in the 'files' list. On the other hand, I have
            # seen some other general product builds that have both
            # tars and rpms (and assorted other file types), and I've
            # seen pure RPM builds with srpms and rpms...
            for f in self.body['files']:
                if f['type'] == 'rpm':
                    self.kind = 'rpm'
                    self.file_type = 'rpm'
                    break
                elif f['type'] == 'tar':
                    self.kind = 'image'
                    self.file_type = 'tar'
                    break

    def add_buildinfo(self, verbose=False):
        """Add buildinfo from upstream brew"""
        date_format = '%a, %d %b %Y %H:%M:%S %Z'
        if verbose:
            click.secho('.', nl=False)
        self.buildinfo = get_brew_buildinfo(self)
        self.finished = datetime.datetime.strptime(self.buildinfo['Finished'], date_format)

    def to_json(self):
        """Method for adding this build to advisory via the Errata Tool
API. This is the body content of the erratum add_builds endpoint."""
        return {
            'product_version': self.product_version,
            'build': self.nvr,
            'file_types': [self.file_type],
        }
