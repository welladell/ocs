import time

def resolve_child_state(db):
    """Args:

      db (dict): the instance state information.  This will be
        modified in place.

    Returns:

      Dict with important actions for caller to take.  Content is:

      - 'messages' (list of str): messages for the session.
      - 'launch' (bool): whether to launch a new instance.
      - 'terminate' (bool): whether to terminate the instance.
      - 'sleep' (float): maximum delay before checking back.

    """
    actions = {
        'messages': [],
        'launch': False,
        'terminate': False,
    }

    class _S:
        def add_message(self, msg):
            actions['messages'].append(msg)
    session = _S()
    sleep_time = 1.

    # State machine.
    prot = db['prot']

    # The uninterruptible transition state(s) are most easily handled
    # in the same way regardless of target state.

    # Transitional: wait_start, which bridges from start -> up.
    if db['next_action'] == 'wait_start':
        if prot is not None:
            session.add_message('Launched {full_name}'.format(**db))
            db['next_action'] = 'up'
        else:
            if time.time() >= db['at']:
                session.add_message('Launch not detected for '
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
                session.add_message('Agent instance {full_name} '
                                    'refused to die.'.format(**db))
                db['next_action'] = 'down'
        else:
            sleep_time = min(sleep_time, db['at'] - time.time())

    # State handling when target is to be 'up'.
    elif db['target_state'] == 'up':
        if db['next_action'] == 'start_at':
            if time.time() >= db['at']:
                db['next_action'] = 'start'
            else:
                sleep_time = min(sleep_time, db['at'] - time.time())
        elif db['next_action'] == 'start':
            # Launch.
            if db['agent_script'] is None:
                session.add_message('No Agent script registered for '
                                    'class: {class_name}'.format(**db))
                db['next_action'] = 'down'
            else:
                session.add_message(
                    'Requested launch for {full_name}'.format(**db))
                db['prot'] = None
                actions['launch'] = True
                db['next_action'] = 'wait_start'
                db['at'] = time.time() + 1.
        elif db['next_action'] == 'up':
            stat, t = prot.status
            if stat is not None:
                # Right here would be a great place to check
                # the stat return code, and include a traceback from stderr 
                session.add_message('Detected exit of {full_name} '
                                    'with code {stat}.'.format(stat=stat, **db))
                db['next_action'] = 'start_at'
                db['at'] = time.time() + 3
        else:  # 'down'
            db['next_action'] = 'start'

    # State handling when target is to be 'down'.
    elif db['target_state'] == 'down':
        if db['next_action'] == 'down':
            pass
        elif db['next_action'] == 'up':
            session.add_message('Requesting termination of '
                                '{full_name}'.format(**db))
            actions['terminate'] = True
            db['next_action'] = 'wait_dead'
            db['at'] = time.time() + 5
        else: # 'start_at', 'start'
            session.add_message('Modifying state of {full_name} from '
                                '{next_action} to idle'.format(**db))
            db['next_action'] = 'down'

    # Should not get here.
    else:
        session.add_message(
            'State machine failure: state={next_action}, target_state'
            '={target_state}'.format(**db))

    actions['sleep'] = sleep_time
    return actions