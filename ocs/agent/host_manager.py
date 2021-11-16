import os
import time
import yaml

from twisted.internet import reactor, utils, protocol
from twisted.internet.defer import inlineCallbacks

def resolve_child_state(db):
    """Args:

      db (dict): the instance state information.  This will be
        modified in place.

    Returns:

      Dict with important actions for caller to take.  Content is:

      - 'messages' (list of str): messages for the session.
      - 'launch' (bool): whether to launch a new instance.
      - 'terminate' (bool): whether to terminate the instance.
      - 'sleep' (float): maximum delay before checking back, or None
        if this machine doesn't care.

    """
    actions = {
        'launch': False,
        'terminate': False,
        'sleep': None,
    }

    messages = []
    sleeps = []

    # State machine.
    prot = db['prot']

    # The uninterruptible transition state(s) are most easily handled
    # in the same way regardless of target state.

    # Transitional: wait_start, which bridges from start -> up.
    if db['next_action'] == 'wait_start':
        if prot is not None:
            messages.append('Launched {full_name}'.format(**db))
            db['next_action'] = 'up'
        else:
            if time.time() >= db['at']:
                messages.append('Launch not detected for '
                                '{full_name}!  Will retry.'.format(**db))
                db['next_action'] = 'start_at'
                db['at'] = time.time() + 5.

    # Transitional: wait_dead, which bridges from kill -> idle.
    elif db['next_action'] == 'wait_dead':
        if prot is None:
            stat, t = 0, None
        else:
            stat, t = prot.status
        if stat is not None:
            db['next_action'] = 'down'
        elif time.time() >= db['at']:
            if stat is None:
                messages.append('Agent instance {full_name} '
                                'refused to die.'.format(**db))
                db['next_action'] = 'down'
        else:
            sleeps.append(db['at'] - time.time())

    # State handling when target is to be 'up'.
    elif db['target_state'] == 'up':
        if db['next_action'] == 'start_at':
            if time.time() >= db['at']:
                db['next_action'] = 'start'
            else:
                sleeps.append(db['at'] - time.time())
        elif db['next_action'] == 'start':
            # Launch.
            if db['agent_script'] is None:
                messages.append('No Agent script registered for '
                                'class: {class_name}'.format(**db))
                db['next_action'] = 'down'
            else:
                messages.append(
                    'Requested launch for {full_name}'.format(**db))
                db['prot'] = None
                actions['launch'] = True
                db['next_action'] = 'wait_start'
                now = time.time()
                db['at'] = now + 1.
                db['start_times'].append(now)
        elif db['next_action'] == 'up':
            stat, t = prot.status
            if stat is not None:
                messages.append('Detected exit of {full_name} '
                                'with code {stat}.'.format(stat=stat, **db))
                if hasattr(prot, 'lines'):
                    note = ''
                    lines = prot.lines['stderr']
                    if len(lines) > 50:
                        note = ' (trimmed)'
                        lines = lines[-20:]
                    messages.append('stderr output from {full_name}{note}: {}'
                                    .format('\n'.join(lines), note=note, **db))
                db['next_action'] = 'start_at'
                db['at'] = time.time() + 3
        else:  # 'down'
            db['next_action'] = 'start'

    # State handling when target is to be 'down'.
    elif db['target_state'] == 'down':
        if db['next_action'] == 'down':
            if prot is not None and prot.status[0] is None:
                messages.append('Detected unexpected session for {full_name} '
                                '(probably docker); changing target state to "up".'.format(**db))
                db['target_state'] = 'up'
        elif db['next_action'] == 'up':
            messages.append('Requesting termination of '
                            '{full_name}'.format(**db))
            actions['terminate'] = True
            db['next_action'] = 'wait_dead'
            db['at'] = time.time() + 5
        else: # 'start_at', 'start'
            messages.append('Modifying state of {full_name} from '
                            '{next_action} to idle'.format(**db))
            db['next_action'] = 'down'

    # Should not get here.
    else:
        messages.append(
            'State machine failure: state={next_action}, target_state'
            '={target_state}'.format(**db))

    actions['messages'] = messages
    if len(sleeps):
        actions['sleep'] = min(sleeps)
    return actions


def stability_factor(times, window=120):
    """Given an increasing list of start times, the last one corresponding
    to the present run, decide whether the process the activity is
    running stably or not.

    Returns a culled list of start times and a stability factor (0 -
    1).  A stable agent will settle to stability factor of 1 within
    window seconds.  An unstable agent will have stability factor of
    0.5 or less.

    """
    now = time.time()
    if len(times) == 0:
        return times, -1.
    times = [t for t in times[-200:-1]
             if t >= now - window] + times[-1:]
    return times, 1./len(times)


class AgentProcessHelper(protocol.ProcessProtocol):
    def __init__(self, instance_id, cmd):
        super().__init__()
        self.status = None, None
        self.killed = False
        self.instance_id = instance_id
        self.cmd = cmd
        self.lines = {'stderr': [],
                      'stdout': []}

    def up(self):
        reactor.spawnProcess(self, self.cmd[0], self.cmd[:], env=os.environ)

    def down(self):
        self.killed = True
        # race condition, but it could be worse.
        if self.status[0] is None:
            reactor.callFromThread(self.transport.signalProcess, 'INT')

    # See https://twistedmatrix.com/documents/current/core/howto/process.html
    #
    # These notes, and the useless prototypes below them, are to get
    # us started when we come back here later to feed the process
    # output to high level logging somehow.
    #
    # In a successful launch, we see:
    # - connectionMade (at which point we closeStdin)
    # - inConnectionLost (which is then expected)
    # - childDataReceived(counter, message), output from the script.
    # - later, when process exits: processExited(status).  Status is some
    #   kind of object that knows the return code...
    # In a failed launch, it's the same except note that:
    # - The childDataReceived message contains the python traceback, on,
    #   e.g. realm error.  +1 - Informative.
    # - The processExited(status) knows the return code was not 0.
    #
    # Note that you implement childDataReceived instead of
    # "outReceived" and "errReceived".
    def connectionMade(self):
        self.transport.closeStdin()
    def inConnectionLost(self):
        pass
    def processExited(self, status):
        #print('%s.status:' % self.instance_id, status)
        self.status = status, time.time()
    def outReceived(self, data):
        self.lines['stdout'].append(data.decode('utf8').split('\n'))
        if len(self.lines['stdout']) > 100:
            self.lines['stdout'] = self.lines['stdout'][-100:]
    def errReceived(self, data):
        self.lines['stderr'].append(data.decode('utf8').split('\n'))
        if len(self.lines['stderr']) > 100:
            self.lines['stderr'] = self.lines['stderr'][-100:]

class DockerContainerHelper:

    """Class for managing the docker container associated with some
    service.  Provides some of the same interface as
    AgentProcessHelper in HostManager agent.

    """
    def __init__(self, service):
        self.service = {}
        self.status = -1, time.time()
        self.killed = False
        self.instance_id = service['service']
        self.d = None
        self.update(service)

    def update(self, info):
        """Update self.status based on the latest "info", for this service,
        from parse_docker_state.

        """
        self.service.update(info)
        if info['running']:
            self.status = None, time.time()
        else:
            self.status = info['exit_code'], time.time()

    def up(self):
        self.d = utils.getProcessOutputAndValue(
            'docker-compose', ['-f', self.service['compose_file'],
                               'up', '-d', self.service['service']])
        self.status = None, time.time()

    def down(self):
        self.d = utils.getProcessOutputAndValue(
            'docker-compose', ['-f', self.service['compose_file'],
                               'rm', '--stop', '--force', self.service['service']])
        self.killed = True


@inlineCallbacks
def parse_docker_state(docker_compose_file):
    """Analyze a docker-compose.yaml file to get a list of services.
    Using docker-compose ps and docker inspect, determine whether each
    service is running or not.

    Returns:
      A dict where the key is the service name and each value is a
      dict with the following entries:

      - 'compose_file': the path to the docker-compose file
      - 'service': service name
      - 'container_found': bool, indicates whether a container for
        this service was found (whether or not it was running).
      - 'running': bool, indicating that a container for this service
        is currently in state "Running".
      - 'exit_code': int, which is either extracted from the docker
        inspect output or is set to 127.  (This should never be None.)

    """

    summary = {}

    compose = yaml.safe_load(open(docker_compose_file, 'r'))
    for key, cfg in compose.get('services', []).items():
        summary[key] = {
            'service': key,
            'running': False,
            'exit_code': 127,
            'container_found': False,
            'compose_file': docker_compose_file,
        }

    # Query docker-compose for container ids...
    out, err, code = yield utils.getProcessOutputAndValue(
        'docker-compose', ['-f', docker_compose_file, 'ps', '-q'])
    if code != 0:
        raise RuntimeError("Could not run docker-compose or could not parse "
                           "docker-compose file; exit code %i, error text: %s" %
                           (code, err))

    # Run docker inspect.
    for line in out.decode('utf8').split('\n'):
        if line.strip() == '':
            continue
        out, err, code = yield utils.getProcessOutputAndValue(
            'docker', ['inspect', line])
        if code != 0:
            raise RuntimeError('Trouble running "docker inspect %s".' % line)
        # Reconcile config against docker-compose ...
        info = yaml.safe_load(out)[0]
        config = info['Config']['Labels']
        _dc_file = os.path.join(config['com.docker.compose.project.working_dir'],
                                config['com.docker.compose.project.config_files'])
        if not os.path.samefile(docker_compose_file, _dc_file):
            raise RuntimeError("Consistency problem: container started from "
                               "some other compose file?\n%s\n%s" % (docker_compose_file, _dc_file))
        service = config['com.docker.compose.service']
        assert(service in summary)
        if service not in summary:
            raise RuntimeError("Consistency problem: image does not self-report "
                               "as a listed service? (%s)" % (service))
        summary[service].update({
            'running': info['State']['Running'],
            'exit_code': info['State'].get('ExitCode', 127),
            'container_found': True,
        })
    return summary
