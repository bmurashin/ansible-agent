#!/usr/bin/python3

import os
import sys
import json
import re
import socket
import logging

try:
    logging.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s', level=logging.INFO, filename='/var/log/ansiblectl-server.log')
except Exception as e:
    # fallback saving logs into /var/tmp
    logging.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s', level=logging.INFO, filename='/var/tmp/ansiblectl-server.log')


# check if this is a CGI POST request
try:
    if (os.environ.get('REQUEST_METHOD') == 'POST' and
        os.environ.get('CONTENT_TYPE') == 'application/json' and
        os.environ.get('CONTENT_LENGTH') and
        os.environ.get('REMOTE_ADDR')):
        
        input = sys.stdin.read()
        post_params = json.loads(input)
        
        if ('host' not in post_params or not re.match("^\w[\w\-\.]+\.\w+$", post_params['host']) or
            'branch' not in post_params or not re.match("^\w[\w\-]+$", post_params['branch'])):
            raise Exception("Required POST parameters are not found or malformed")
        
        host = post_params['host']
        branch = post_params['branch']
        remote_addr = os.environ.get('REMOTE_ADDR')
        
        # check host ip addr
        ips = sorted(set(i[4][0] for i in socket.getaddrinfo(host, None)))
        if remote_addr not in ips:
            raise Exception(f"IP {remote_addr} doesn't belong to host {host}")
        
    else:
        raise Exception("Not a POST request or no data in request")
except Exception as e:
    logging.error(e)
    print('Status: 402 Bad request')
    print('Content-Type: text/plain')
    print('')
    sys.exit(1)


def copy_with_reencrypt(src_base: 'path base shared by all files', src: 'used as dict key - path part, unique for every file', dst):
    # truncated version from ansible-server w/out caching
    secrets_count = reencrypt(f"{src_base}/{src}", dst, vault, new_vault)
    if secrets_count:
        logging.debug(f"Succeffully reencrypted {src}")
    else:
        logging.debug(f"{src} has no secrets, copying as is")
        shutil.copy(f"{src_base}/{src}", dst)


try:

    from configparser import ConfigParser
    import yaml
    import socket
    from jinja2 import Template
    import os
    import sys
    import shutil
    import filecmp
    import logging
    import git
    from git import RemoteProgress
    import time
    import fcntl
    import pprint

    from ansible_server_functions import *

    config = ConfigParser()
    config.read('/etc/ansible-server/ansible-server.conf')

    path_tmp        = config.get('ansible-server', 'path_tmp')
    path_prod       = config.get('ansible-server', 'path_prod')
    path_doc_root   = config.get('ansible-server', 'path_doc_root')

    # git config
    GIT_URL         = config.get('git', 'git_url')
    GIT_LOCAL_PATH  = config.get('git', 'git_local_path_ansiblectl')
    GIT_BRANCH      = branch

    VAULT_PASSWORD      = config.get('ansible-server', 'vault_password',  fallback='').encode()
    NEW_VAULT_PASSWORD  = config.get('ansible-server', 'new_vault_password',  fallback='').encode()

    lock_file = '/tmp/ansiblectl-server.lock'

    timestamp = round(time.time())
    
    
    # acquire lock
    lock = open(lock_file, 'w')
    lock_aquired = False
    start_time = current_time = time.time()
    while current_time < start_time + 30:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            time.sleep(1)
        else:
            logging.debug('Aquired lock')
            lock_aquired = True
            break
        current_time = time.time()
    if not lock_aquired:
        logging.error("Couldn't aquire lock in 30 seconds, giving up")
        print('Status: 503 Service Unavailable')
        print('Content-Type: text/plain')
        print('')
        sys.exit(1)
    
    
    # pull requested branch
    try:
        # try to use existing repo
        repo = git.Repo(GIT_LOCAL_PATH)
        repo.git.checkout(GIT_BRANCH)
        git_cmd = git.cmd.Git(GIT_LOCAL_PATH)
        git_cmd.pull()
    except Exception as e:
        logging.warning(f"Failed with existing git repo in {GIT_LOCAL_PATH}: {e}")
        if os.path.exists(GIT_LOCAL_PATH):
            shutil.rmtree(GIT_LOCAL_PATH)
        os.makedirs(GIT_LOCAL_PATH)
        class CloneProgress(RemoteProgress):
            def update(self, op_code, cur_count, max_count=None, message=''):
                if message:
                    logging.info(message)
        git.Repo.clone_from(GIT_URL, GIT_LOCAL_PATH, branch=GIT_BRANCH, progress=CloneProgress(), config='http.sslVerify=false')
        repo = git.Repo(GIT_LOCAL_PATH)
        repo.git.checkout(GIT_BRANCH)
    
    commit_sha = repo.head.object.hexsha


    # check if prepared folder exists
    if os.path.exists(f"{path_doc_root}/{host}/ansiblectl"):
        for folder in sorted(os.listdir(f"{path_doc_root}/{host}/ansiblectl")):
            folder_parts = folder.split('_')
            folder_ts = folder_parts[0]
            folder_commit = folder_parts[1]
            if folder_commit == commit_sha and int(folder_ts) > timestamp - 7200:
                # found existing folder that will not be removed soon by housekeeping
                logging.debug(f"Redirecting to existing folder {folder}")
                print('Status: 303 See other')
                print(f'Location: /{host}/ansiblectl/{folder}/')
                print('')
                sys.exit(0)
    
    
    # copy project pieces for requested host and branch
    do_reencrypt = len(VAULT_PASSWORD) > 0 and len(NEW_VAULT_PASSWORD) > 0
    if do_reencrypt:
        from ansible.parsing.vault import VaultLib
        from ansible.parsing.vault import VaultSecret
        from ansible import constants as C
        
        vault       = VaultLib([(C.DEFAULT_VAULT_IDENTITY, VaultSecret(VAULT_PASSWORD))])
        new_vault   = VaultLib([(C.DEFAULT_VAULT_IDENTITY, VaultSecret(NEW_VAULT_PASSWORD))])
    
    
    inventory = ConfigParser(allow_no_value=True)
    inventory.read(f'{GIT_LOCAL_PATH}/hosts')

    with open(f'{GIT_LOCAL_PATH}/site.yaml', 'r') as file:
        playbook = yaml.safe_load(file)


    host_groups, group_parents = parse_inventory(inventory)
    all_host_groups = {}
    all_host_groups[host] = sorted(set(add_parent_groups(host_groups[host], group_parents)))
    
    path_host = f"{path_tmp}/{host}/ansiblectl/{timestamp}_{commit_sha}"
    logging.debug (f"create {path_host}")
    os.makedirs(f"{path_host}")
    
    
    all_host_roles  = []
    for play in playbook:
        if (type(play['tags']) is list and 'ansible-agent-run' in play['tags'] or
            type(play['tags']) is str  and 'ansible-agent-run' == play['tags']):
            if (type(play['hosts']) is list and set(play['hosts']) & set([host, 'all'] + all_host_groups[host]) or
                type(play['hosts']) is str  and play['hosts'] in [host, 'all'] + all_host_groups[host]):
                    all_host_roles.append(play)
    logging.debug(pprint.pformat({'host': host, 'roles': all_host_roles}))    

    # write inventory for host
    hosts_content = ''.join([f'[{i}]\n{host}\n\n' for i in all_host_groups[host]])
    logging.debug (f"create {path_host}/hosts = \n{hosts_content}")
    with open(f"{path_host}/hosts", 'w') as file:
        file.write(hosts_content)

    # copy host_vars
    if os.path.exists(f"{GIT_LOCAL_PATH}/host_vars/{host}"):
        logging.debug (f"create {path_host}/host_vars/{host}")
        os.makedirs(f"{path_host}/host_vars")
        if do_reencrypt:
            copy_with_reencrypt(GIT_LOCAL_PATH, f"host_vars/{host}", f"{path_host}/host_vars/{host}")
        else:
            shutil.copy(f"{GIT_LOCAL_PATH}/host_vars/{host}", f"{path_host}/host_vars/{host}")

    # copy group_vars
    logging.debug (f"create {path_host}/group_vars/")
    os.makedirs(f"{path_host}/group_vars")
    for host_group in ['all'] + all_host_groups[host]:
        if os.path.exists(f"{GIT_LOCAL_PATH}/group_vars/{host_group}"):
            if do_reencrypt:
                copy_with_reencrypt(GIT_LOCAL_PATH, f"group_vars/{host_group}", f"{path_host}/group_vars/{host_group}")
            else:
                shutil.copy(f"{GIT_LOCAL_PATH}/group_vars/{host_group}", f"{path_host}/group_vars/{host_group}")

    # write site.yaml for this host
    logging.debug (f"create {path_host}/site.yaml = \n{all_host_roles}")
    with open(f"{path_host}/site.yaml", 'w') as file:
        yaml.dump(all_host_roles, file, sort_keys=False)
    
    # copy roles
    logging.debug (f"create {path_host}/roles")
    os.makedirs(f"{path_host}/roles")
    for role in set(ii['role']  for i in all_host_roles  for ii in i['roles']):
        shutil.copytree(f"{GIT_LOCAL_PATH}/roles/{role}", f"{path_host}/roles/{role}")
        if do_reencrypt and os.path.exists(f"{GIT_LOCAL_PATH}/roles/{role}/vars"):
            for vars in os.listdir(f"{GIT_LOCAL_PATH}/roles/{role}/vars"):
                copy_with_reencrypt(GIT_LOCAL_PATH, f"roles/{role}/vars/{vars}", f"{path_host}/roles/{role}/vars/{vars}")
        if do_reencrypt and os.path.exists(f"{GIT_LOCAL_PATH}/roles/{role}/defaults"):
            for vars in os.listdir(f"{GIT_LOCAL_PATH}/roles/{role}/defaults"):
                copy_with_reencrypt(GIT_LOCAL_PATH, f"roles/{role}/defaults/{vars}", f"{path_host}/roles/{role}/defaults/{vars}")


    # move prepared folder structure to document root
    logging.debug (f"mv  {path_host}   {path_doc_root}/{host}/ansiblectl/{timestamp}_{commit_sha}")
    shutil.move(f"{path_host}", f"{path_doc_root}/{host}/ansiblectl/{timestamp}_{commit_sha}")

    
    # cleanup
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


except Exception as e:
    logging.error(f'Exception: {e}, line {sys.exc_info()[-1].tb_lineno}')
    print('Status: 500 Internal server error')
    print('Content-Type: text/plain')
    print('')
    sys.exit(1)


logging.debug(f"Redirecting to freshly prepared folder {host}/ansiblectl/{timestamp}_{commit_sha}")
print('Status: 303 See other')
print(f'Location: /{host}/ansiblectl/{timestamp}_{commit_sha}/')
print('')
