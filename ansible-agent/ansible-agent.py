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
import json


config = ConfigParser()
config.read('/etc/ansible-agent/ansible-agent.conf')
log_level = config.get('ansible', 'log_level', fallback='INFO')
logging.basicConfig(format='%(levelname)s: %(message)s', level=getattr(logging, log_level.upper()))

# ansible config
HOSTNAME                = config.get('ansible', 'hostname')
VAULTSECRET             = 'VAULTSECRET_TMP'
INVENTORY_FILE          = config.get('ansible', 'inventory_file',               fallback='hosts')
PLAYBOOK                = config.get('ansible', 'playbook',                     fallback='site.yaml')
TIMER_SCHEDULED_RUN_SEC = int(config.get('ansible', 'timer_scheduled_run_sec',  fallback=1500))
TIMER_GIT_CHECK_SEC     = int(config.get('ansible', 'timer_git_check_sec',      fallback=30))
SKIP_TAGS               = config.get('ansible', 'skip_tags',                    fallback='').split(',')
EXTRA_VARS              = { i for i in config.get('ansible', 'extra_vars',      fallback='').split(',') }
PROJECT_PATH            = config.get('ansible', 'project_path',                 fallback='/root/.ansible-agent/project/')

#ssl config
CACERT_BUNDLE           = config.get('ssl', 'ca_cert_bundle_path', fallback=True)

# git config
GIT_URL         = config.get('git', 'git_url',          fallback='')
GIT_BRANCH      = config.get('git', 'git_branch',       fallback='master')

#server config
ANSIBLE_SERVER_URL = config.get('server', 'server_url',  fallback=None)


# alerts config
ALERT_USER      = config.get('alerts', 'alert_user',     fallback='')
ALERT_PASSWORD  = config.get('alerts', 'alert_password', fallback='')
ALERT_URL       = config.get('alerts', 'alert_url',      fallback='')



lock_file = '/tmp/ansible-agent.lock'


if os.path.exists(PROJECT_PATH):
    shutil.rmtree(PROJECT_PATH)
os.makedirs(PROJECT_PATH)


if ANSIBLE_SERVER_URL is None:
  class CloneProgress(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        if message:
            logging.info(message)
  git.Repo.clone_from(GIT_URL, PROJECT_PATH, branch=GIT_BRANCH, progress=CloneProgress(), config='http.sslVerify=false')

else:
  host_url = ANSIBLE_SERVER_URL  + HOSTNAME + '/'



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

 
def ansible_play():

    context.CLIARGS = ImmutableDict(connection='local', forks=20, become=None,
                                    become_method='sudo', become_user='root', check=False, diff=False, verbosity=True,
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
        if ALERT_URL:
            logging.debug('Send alert')
            alert = { "body": f"ansible-agent error on {HOSTNAME}, return code = {rc}" }
            headers = {'Content-Type': 'application/json'}
            requests.post(ALERT_URL, data=json.dumps(alert), headers=headers, auth=(ALERT_USER, ALERT_PASSWORD), timeout=15, verify=False)


def run_on_commit():
  
    lock = open(lock_file, 'w')
    if ANSIBLE_SERVER_URL is None:
       repo = git.Repo(PROJECT_PATH)
       git_cmd = git.cmd.Git(PROJECT_PATH)
       repo.git.checkout(GIT_BRANCH)
       current_hash = repo.head.object.hexsha
    else:
       session = requests.Session()
       a = session.get(host_url, verify = CACERT_BUNDLE)
       if a.status_code == 200:
          all_commit={}
          for hash in a.json():
            if hash['name'] != 'ansiblectl':
              all_commit[hash['name'].split("_")[0]] = hash['name']
          sort_dict_hash = dict(sorted(all_commit.items()))
          current_hash = sort_dict_hash[list(sort_dict_hash.keys())[-1]]
          roles_url= host_url + '/' + current_hash
          try:
            download_url(roles_url, current_hash, session)
          except:
            logging.error('''Can't Download roles for host''')
            sys.exit(1)
       else:
          logging.error('''Can't get  commit hash''')
          sys.exit(1)
    while True:
        time.sleep(TIMER_GIT_CHECK_SEC)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            logging.debug("run_on_commit() could'n acquire lock - must be run_on_schedule() holding the lock")
        else:
            try:
               if ANSIBLE_SERVER_URL is None: 
                 git_cmd.pull()
                 new_hash = repo.head.object.hexsha
               else:
                 logging.info('start check new commit from ansible-servers')
                 a = session.get(host_url, verify = CACERT_BUNDLE)
                 if a.status_code == 200:
                    all_commit={}
                    for hash in a.json():
                      if hash['name'] != 'ansiblectl':
                        all_commit[hash['name'].split("_")[0]] = hash['name']
                    sort_dict_hash = dict(sorted(all_commit.items()))
                    new_hash = sort_dict_hash[list(sort_dict_hash.keys())[-1]]
                 else:
                    logging.error('''Can't get  commit hash''')
                    continue
            except Exception as e:
                logging.error(f'Failed running git pull or get from ansible-server: {e}, line {sys.exc_info()[-1].tb_lineno}')
            if new_hash != current_hash:
                if ANSIBLE_SERVER_URL is not None:
                   if os.path.exists(PROJECT_PATH):
                      shutil.rmtree(PROJECT_PATH)
                      os.makedirs(PROJECT_PATH)
                   roles_url= host_url + '/' + new_hash
                   try:
                     download_url(roles_url, new_hash, session)
                   except:
                     logging.error('''Can't Download roles for host''')
                     continue
                logging.info(f'Running ansible for a new commit: {new_hash}')
                process = Process(target=ansible_play)
                process.start()
                process.join()
                current_hash = new_hash
            fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


def run_on_schedule():
    lock = open(lock_file, 'w')
    while True:
        time.sleep(TIMER_SCHEDULED_RUN_SEC)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            logging.debug("run_on_schedule() could'n acquire lock - must be run_on_commit() holding the lock")
        else:
            logging.info('Running ansible on schedule')
            process = Process(target=ansible_play)
            process.start()
            process.join()
            fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


if __name__ == '__main__':
    logging.debug('Start execution')
    p1 = Process(target=run_on_commit)
    p1.start()
    p2 = Process(target=run_on_schedule)
    p2.start()
    p1.join()
    p2.join()

