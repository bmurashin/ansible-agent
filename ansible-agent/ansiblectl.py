#!/usr/bin/env python3

import socket
from ansible.module_utils.common.collections import ImmutableDict
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.vars.manager import VariableManager
from ansible import context
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.module_utils._text import to_bytes
from ansible.parsing.vault import VaultSecret
import git
from git import RemoteProgress
import shutil
import time
from multiprocessing import Process
import os
import sys
from configparser import ConfigParser
import requests
import shutil
import ansible.constants as C
import fcntl
import logging
import argparse
import signal

os.environ["ANSIBLE_COLLECTIONS_PATH"] = "/usr/local/lib/python3.6/site-packages/ansible_collections/"
os.system('export ANSIBLE_COLLECTIONS_PATH=/usr/local/lib/python3.6/site-packages/ansible_collections/')

parser= argparse.ArgumentParser()
parser.add_argument("-run", help=''' ansible-playbook run ''', action="store_true")
parser.add_argument("-b", "--branch", help=''' choose branch ''', default='master')
parser.add_argument("-c", "--check", help=''' dry run (check) ''', action="store_true", default=False)
parser.add_argument("-d", "--diff", help=''' show diff ''', action="store_true", default=False)
parser.add_argument("--debug", help=''' verbose output ''', action="store_true", default=False)
args = parser.parse_args()

logging.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s', level=logging.DEBUG if args.debug else logging.INFO)


config = ConfigParser()
config.read('/etc/ansible-agent/ansible-agent.conf')

# ansible config
HOSTNAME                = config.get('ansible', 'hostname')
VAULTSECRET             = '${NEW_VAULT_PASSWORD}'
INVENTORY_FILE          = config.get('ansible', 'inventory_file',               fallback='hosts')
PLAYBOOK                = config.get('ansible', 'playbook',                     fallback='site.yaml')
SKIP_TAGS               = config.get('ansible', 'skip_tags',                    fallback='').split(',')
EXTRA_VARS              = { i for i in config.get('ansible', 'extra_vars',      fallback='').split(',') }
PROJECT_PATH            = config.get('ansible', 'project_path_ansiblectl',      fallback='/root/.ansible-agent/projectcli/')

#ssl config
CACERT_BUNDLE           = config.get('ssl', 'ca_cert_bundle_path', fallback=True)

# git config
GIT_URL         = config.get('git', 'git_url',          fallback='')
GIT_BRANCH      = args.branch

#server config
ANSIBLE_SERVER_URL = config.get('server', 'server_url',  fallback=None)


lock_file = '/tmp/ansiblectl.lock'


def download_url(url, last_hash, session):
   p_respons  = session.get(url, verify = CACERT_BUNDLE)
   for p_files in p_respons.json():
       dir= PROJECT_PATH + url.split(last_hash)[1]
       if p_files['type'] == 'directory':
            logging.debug('create dir: ' + dir+ '/' + p_files['name'] )
            os.makedirs(dir+ '/' + p_files['name'])
            new_url=url + '/' + p_files['name'] + '/'
            download_url(new_url, last_hash, session)
       elif p_files['type'] == 'file':
            files = session.get(url + '/' + p_files['name'], verify = CACERT_BUNDLE)
            logging.debug('create file: ' + dir + '/'  + p_files['name'] )
            with open(dir + '/'  + p_files['name'], 'wb') as f:
               f.write(files.content)


def ansible_play(check=False, diff=False):

    context.CLIARGS = ImmutableDict(connection='local', forks=20, become=None,
                                    become_method='sudo', become_user='root', check=check, diff=diff, verbosity=True,
                                    syntax=False, listhosts=False, listtasks=False, listtags=False, start_at_task=None, skip_tags=SKIP_TAGS, extra_vars=EXTRA_VARS)
    loader = DataLoader()  # Takes care of finding and reading yaml, json and ini files
    loader.set_vault_secrets([('default', VaultSecret(_bytes=to_bytes(VAULTSECRET)))])
    passwords = {}
    inventory = InventoryManager(loader=loader, sources=PROJECT_PATH + INVENTORY_FILE)
    inventory.subset([HOSTNAME])
    variable_manager = VariableManager(loader=loader, inventory=inventory)
    playbooks = [PROJECT_PATH + PLAYBOOK]
    executor = PlaybookExecutor(playbooks=playbooks, inventory=inventory,
                                variable_manager=variable_manager, loader=loader, passwords=passwords)
    rc = executor.run()
    shutil.rmtree(C.DEFAULT_LOCAL_TMP, True)
    if rc != 0:
        logging.error(f'playbook executor run return code: {rc}')


def handler(signum, frame):
    shutil.rmtree(PROJECT_PATH)
    exit(1)


if args.run:

    signal.signal(signal.SIGHUP, handler)
    signal.signal(signal.SIGINT, handler)

    
    lock = open(lock_file, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError) as e:
        logging.fatal(f"Could'n acquire lock: {e}")
        sys.exit(1)
    
    
    if os.path.exists(PROJECT_PATH):
        shutil.rmtree(PROJECT_PATH)
    os.makedirs(PROJECT_PATH)


    if ANSIBLE_SERVER_URL is None:
        class CloneProgress(RemoteProgress):
            def update(self, op_code, cur_count, max_count=None, message=''):
                if message:
                    logging.info(message)
        git.Repo.clone_from(GIT_URL, PROJECT_PATH, branch=GIT_BRANCH, progress=CloneProgress(), config='http.sslVerify=false')
        repo = git.Repo(PROJECT_PATH)
        repo.git.checkout(GIT_BRANCH)
    
    else:
        session = requests.Session()
        data = {"host": HOSTNAME, "branch": GIT_BRANCH}
        logging.info(f"HTTP POST {ANSIBLE_SERVER_URL}/ansiblectl: {data}")
        resp = session.post(f"{ANSIBLE_SERVER_URL}/ansiblectl", json = data, verify = CACERT_BUNDLE)
        if resp.status_code == 200:
            # get last url in request
            roles_url = resp.request.url
            hash = roles_url.rstrip('/').split('/')[-1]
            try:
                logging.info(f"Downloading from: {roles_url}")
                download_url(roles_url, hash, session)
            except:
                logging.fatal(f'Download failed: {e}, line {sys.exc_info()[-1].tb_lineno}')
                sys.exit(1)
        else:
            logging.fatal(f'Download failed: {resp.status_code} {resp.reason}, line {sys.exc_info()[-1].tb_lineno}')
            sys.exit(1)


    ansible_play(args.check, args.diff)
    shutil.rmtree(PROJECT_PATH)

    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
