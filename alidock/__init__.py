"""alidock module"""

from __future__ import print_function
import argparse
from argparse import ArgumentParser
from pwd import getpwuid
from time import time, sleep
import errno
import os
import os.path
import json
import subprocess
import yaml
from yaml import YAMLError
import docker
import jinja2
import requests
from requests.exceptions import RequestException
from pkg_resources import resource_string, parse_version, require
from alidock.log import Log

LOG = Log()

class AliDockError(Exception):
    def __init__(self, msg):
        super(AliDockError, self).__init__()
        self.msg = msg
    def __str__(self):
        return self.msg

class AliDock(object):

    def __init__(self, overrideConf=None):
        self.cli = docker.from_env()
        self.dirInside = "/home/alidock"
        self.logRelative = ".alidock.log"
        self.conf = {
            "dockName"        : "alidock",
            "imageName"       : "alisw/alidock:latest",
            "dirOutside"      : "~/alidock",
            "updatePeriod"    : 43200,
            "dontUpdateImage" : False
        }
        self.parseConfig()
        self.overrideConfig(overrideConf)
        self.conf["dockName"] = "{dockName}-{userId}".format(dockName=self.conf["dockName"],
                                                             userId=os.getuid())

    def parseConfig(self):
        confFile = os.path.expanduser("~/.alidock-config.yaml")
        try:
            confOverride = yaml.safe_load(open(confFile).read())
            for k in self.conf:
                self.conf[k] = confOverride.get(k, self.conf[k])
        except (OSError, IOError, YAMLError, AttributeError):
            pass

    def overrideConfig(self, override):
        if not override:
            return
        for k in self.conf:
            if not override.get(k) is None:
                self.conf[k] = override[k]

    def isRunning(self):
        try:
            self.cli.containers.get(self.conf["dockName"])
        except docker.errors.NotFound:
            return False
        return True

    def getSshCommand(self):
        try:
            attrs = self.cli.containers.get(self.conf["dockName"]).attrs
            sshPort = attrs["NetworkSettings"]["Ports"]["22/tcp"][0]["HostPort"]
        except (docker.errors.NotFound, KeyError) as exc:
            outLog = os.path.join(self.conf["dirOutside"], self.logRelative)
            raise AliDockError("cannot find container, maybe it did not start up properly: "
                               "check log file {outLog} for details. Error: {msg}"
                               .format(outLog=outLog, msg=exc))
        return ["ssh", "localhost", "-p", str(sshPort), "-Y", "-F/dev/null",
                "-oForwardX11Trusted=no", "-oUserKnownHostsFile=/dev/null", "-oLogLevel=QUIET",
                "-oStrictHostKeyChecking=no", "-oForwardX11Timeout=596h",
                "-i", os.path.join(self.conf["dirOutside"], ".ssh", "id_rsa")]

    def waitSshUp(self):
        for _ in range(0, 40):
            try:
                nul = open(os.devnull, "w")
                subprocess.check_call(self.getSshCommand() + ["-T", "/bin/true"],
                                      stdout=nul, stderr=nul)
            except subprocess.CalledProcessError:
                sleep(0.5)
            else:
                return True
        return False

    def shell(self, cmd=None):
        os.execvp("ssh", self.getSshCommand() + (cmd if cmd else []))

    def rootShell(self):
        os.execvp("docker", ["docker", "exec", "-it", self.conf["dockName"], "/bin/bash"])

    def run(self):
        # Create directory to be shared with the container
        outDir = os.path.expanduser(self.conf["dirOutside"])
        try:
            os.mkdir(outDir)
        except OSError as exc:
            if not os.path.isdir(outDir) or exc.errno != errno.EEXIST:
                raise AliDockError("cannot create directory {dir} to share with container, "
                                   "check permissions".format(dir=self.conf["dirOutside"]))

        # Create initialisation script
        initSh = jinja2.Template(resource_string("alidock.helpers", "init.sh.j2").decode("utf-8"))
        userId = os.getuid()
        userName = getpwuid(userId).pw_name
        initShPath = os.path.join(outDir, ".alidock-init.sh")
        with open(initShPath, "w") as fil:
            fil.write(initSh.render(logRelative=self.logRelative,
                                    sharedDir=self.dirInside,
                                    dockName=self.conf["dockName"].rsplit("-", 1)[0],
                                    userName=userName,
                                    userId=userId))
        os.chmod(initShPath, 0o755)

        # Start container with that script
        self.cli.containers.run(self.conf["imageName"],
                                command=[os.path.join(self.dirInside, ".alidock-init.sh")],
                                detach=True,
                                auto_remove=True,
                                cap_add=["SYS_PTRACE"],
                                name=self.conf["dockName"],
                                mounts=[docker.types.Mount(self.dirInside,
                                                           outDir, type="bind")],
                                ports={"22/tcp": None})  # None == random port

        return True

    def stop(self):
        try:
            self.cli.containers.get(self.conf["dockName"]).remove(force=True)
        except docker.errors.NotFound:
            pass  # final state is fine, container is gone

    def pull(self):
        try:
            self.cli.images.pull(self.conf["imageName"])
        except docker.errors.APIError as exc:
            raise AliDockError(str(exc))

    def hasUpdates(self, stateFileRelative, updatePeriod, nagOnUpdate, updateFunc):
        """Generic function that checks for updates every updatePeriod seconds, saving the state
           on stateFileRelative (relative to the container's home directory). It returns True in
           case there is an update, False in case there is none. A custom function updateFunc is
           ran to determine whether to update. Set nagOnUpdate to True if, upon an update, the
           state file should not be updated in order to trigger another check at the next run (this
           nags users until they update)."""

        tsFn = os.path.join(os.path.expanduser(self.conf["dirOutside"]), stateFileRelative)
        try:
            with open(tsFn) as fil:
                lastUpdate = int(fil.read())
        except (IOError, OSError, ValueError):
            lastUpdate = 0

        now = int(time())
        updateAvail = False
        if now - lastUpdate > int(updatePeriod):

            caught = None
            try:
                updateAvail = updateFunc()
            except AliDockError as exc:
                caught = exc

            if not nagOnUpdate:
                with open(tsFn, "w") as fil:
                    fil.write(str(now))

            if caught is not None:
                # pylint: disable=raising-bad-type
                raise caught

        return updateAvail

    def hasClientUpdates(self):
        """Check for client updates (alidock) without performing them. Returns True if updates are
           found, false otherwise."""

        if str(require(__package__)[0].version) == "LAST-TAG":
            # No check for local development or version from VCS
            return False

        def updateFunc():
            try:
                pypaData = requests.get("https://pypi.org/pypi/{pkg}/json".format(pkg=__package__),
                                        timeout=5)
                pypaData.raise_for_status()
                availVersion = parse_version(pypaData.json()["info"]["version"])
                localVersion = parse_version(require(__package__)[0].version)
                if availVersion > localVersion:
                    return True
            except (RequestException, ValueError) as exc:
                raise AliDockError(str(exc))
            return False

        return self.hasUpdates(stateFileRelative=".alidock_pip_check",
                               updatePeriod=self.conf["updatePeriod"],
                               nagOnUpdate=True,
                               updateFunc=updateFunc)

    def hasImageUpdates(self):
        """Check for image updates without performing them. Returns True if updates are found, False
           otherwise."""

        if self.conf["dontUpdateImage"]:
            return False

        def updateFunc():
            try:
                try:
                  localHash = self.cli.images.get(
                                  self.conf["imageName"]).attrs["RepoDigests"][0].split("@")[1]
                except docker.errors.NotFound:
                    # Image does not exist locally: no updates are available (run will fetch it)
                    return False
                availHash = self.cli.images.get_registry_data(
                    self.conf["imageName"]).attrs["Descriptor"]["digest"]
                return availHash != localHash
            except (IndexError, docker.errors.APIError) as exc:
                raise AliDockError(str(exc))

        return self.hasUpdates(stateFileRelative=".alidock_docker_check",
                               updatePeriod=self.conf["updatePeriod"],
                               nagOnUpdate=False,
                               updateFunc=updateFunc)

def entrypoint():
    argp = ArgumentParser()
    argp.add_argument("--quiet", dest="quiet", default=False, action="store_true",
                      help="Do not print any message")

    # tmux: both normal and terminal integration ("control mode")
    tmuxArgs = argp.add_mutually_exclusive_group()
    tmuxArgs.add_argument("--tmux", dest="tmux", default=False, action="store_true",
                          help="Start or resume a detachable tmux session")
    tmuxArgs.add_argument("--tmux-control", dest="tmuxControl", default=False, action="store_true",
                          help="Start or resume a detachable tmux session in control mode "
                               "(integration with your terminal)")

    # The following switches can be set in a configuration file
    argp.add_argument("--name", dest="dockName", default=None,
                      help="Override default container name [dockName]")
    argp.add_argument("--image", dest="imageName", default=None,
                      help="Override default image name [imageName]")
    argp.add_argument("--shared", dest="dirOutside", default=None,
                      help="Override host path of persistent home [dirOutside]")
    argp.add_argument("--update-period", dest="updatePeriod", default=None,
                      help="Override update check period [updatePeriod]")
    argp.add_argument("--no-update-image", dest="dontUpdateImage", default=False,
                      action="store_true",
                      help="Do not update the Docker image [dontUpdateImage]")

    argp.add_argument("action", default="enter", nargs="?",
                      choices=["enter", "root", "exec", "start", "status", "stop"],
                      help="What to do")

    argp.add_argument("shellCmd", nargs=argparse.REMAINDER,
                      help="Command to execute in the container (works with exec)")

    args = argp.parse_args()

    LOG.setQuiet(args.quiet)

    try:
        processActions(args)
    except AliDockError as exc:
        LOG.error("Cannot continue: {msg}".format(msg=exc))
        exit(10)
    except docker.errors.APIError as exc:
        LOG.error("Docker error: {msg}".format(msg=exc))
        exit(11)
    except RequestException as exc:
        LOG.error("Cannot communicate to Docker, is it running? Full error: {msg}".format(msg=exc))
        exit(12)

def processEnterStart(aliDock, args):
    created = False
    if not aliDock.isRunning():
        created = True

        try:
            if aliDock.hasImageUpdates():
                LOG.info("Updating container image, hold on")
                aliDock.pull()
                LOG.warning("Container updated, you may want to free some space with:")
                LOG.warning("    docker system prune")
        except AliDockError:
            LOG.warning("Cannot update container image this time")

        LOG.info("Creating container, hold on")
        aliDock.run()
    if args.action == "enter":
        if (args.tmux or args.tmuxControl) and os.environ.get("TMUX") is None:
            LOG.info("Resuming tmux session in the container")
            cmd = ["-t", "tmux", "-u", "-CC", "new-session", "-A", "-s", "ad-tmux"]
            if args.tmux:
                cmd.remove("-CC")
        elif args.tmux or args.tmuxControl:
            raise AliDockError("already in a tmux session")
        else:
            LOG.info("Starting a shell into the container")
            cmd = []
        aliDock.waitSshUp()
        aliDock.shell(cmd)
    elif args.action == "exec":
        LOG.info("Executing command in the container")
        aliDock.waitSshUp()
        aliDock.shell(["-t"] + args.shellCmd)
    elif args.action == "root":
        LOG.info("Starting a root shell into the container (use it at your own risk)")
        aliDock.rootShell()
    elif not created:
        LOG.info("Container is already running")

def processStatus(aliDock):
    if aliDock.isRunning():
        LOG.info("Container is running")
        exit(0)
    LOG.error("Container is not running")
    exit(1)

def processStop(aliDock):
    LOG.info("Destroying the container")
    aliDock.stop()

def processActions(args):

    if os.getuid() == 0:
        raise AliDockError("refusing to execute as root: use an unprivileged user account")

    aliDock = AliDock(args.__dict__)

    try:
        if aliDock.hasClientUpdates():
            LOG.error("You are using an obsolete version of alidock.")
            LOG.error("Upgrade NOW with:")
            LOG.error("    pip install alidock --upgrade")
    except AliDockError:
        LOG.warning("Cannot check for aliDock updates this time")

    if args.action in ["enter", "exec", "root", "start"]:
        processEnterStart(aliDock, args)
    elif args.action == "status":
        processStatus(aliDock)
    elif args.action == "stop":
        processStop(aliDock)
    else:
        assert False, "invalid action"
