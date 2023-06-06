#!/usr/bin/python3

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
import pprint

from ansible_server_functions import *


config = ConfigParser()
config.read('/etc/ansible-server/ansible-server.conf')
log_level = config.get('ansible-server', 'log_level', fallback='INFO')
logging.basicConfig(format='%(asctime)s: %(levelname)s: %(message)s', level=getattr(logging, log_level.upper()))

path_tmp        = config.get('ansible-server', 'path_tmp')
path_prod       = config.get('ansible-server', 'path_prod')
path_doc_root   = config.get('ansible-server', 'path_doc_root')
nginx_config_tmp  = config.get('nginx', 'nginx_config_tmp')
nginx_config_prod = config.get('nginx', 'nginx_config_prod')
TIMER_SCHEDULED_RUN_SEC = int(config.get('ansible-server', 'timer_scheduled_run_sec', fallback=10))

# git config
GIT_URL         = config.get('git', 'git_url')
GIT_LOCAL_PATH  = config.get('git', 'git_local_path')
GIT_BRANCH      = config.get('git', 'git_branch')

VAULT_PASSWORD      = config.get('ansible-server', 'vault_password',  fallback='').encode()
NEW_VAULT_PASSWORD  = config.get('ansible-server', 'new_vault_password',  fallback='').encode()

do_reencrypt = len(VAULT_PASSWORD) > 0 and len(NEW_VAULT_PASSWORD) > 0
if do_reencrypt:
    from ansible.parsing.vault import VaultLib
    from ansible.parsing.vault import VaultSecret
    from ansible import constants as C
    
    vault       = VaultLib([(C.DEFAULT_VAULT_IDENTITY, VaultSecret(VAULT_PASSWORD))])
    new_vault   = VaultLib([(C.DEFAULT_VAULT_IDENTITY, VaultSecret(NEW_VAULT_PASSWORD))])


timestamp = round(time.time())


reencrypted = {}

def copy_with_reencrypt(src_base: 'path base shared by all files', src: 'used as dict key - path part, unique for every file', dst):
    sha256_sum = sha256(f"{src_base}/{src}")
    if src in reencrypted and reencrypted[src]['sha256'] == sha256_sum:
        if reencrypted[src]['secrets_count']:
            if os.path.exists(reencrypted[src]['cache']):
                # valid cache found
                logging.debug(f"using cached reencrypted {src}")
            else:
                # cache is missing for some reason
                logging.debug(f"reencrypted cache missing for {src}, recreate")
                reencrypt(f"{src_base}/{src}", reencrypted[src]['cache'], vault, new_vault)
            shutil.copy(reencrypted[src]['cache'], dst)
        else:
            # no secrets in this file, just copy original
            logging.debug(f"{src} has no secrets, copying as is")
            shutil.copy(f"{src_base}/{src}", dst)
    else:
        # no cache or cache is stale
        logging.debug(f"creating reencrypted cache for {src}")
        secrets_count = reencrypt(f"{src_base}/{src}", f"{path_tmp}/reencrypted/{src}", vault, new_vault)
        reencrypted[src] = {'sha256': sha256_sum, 'secrets_count': secrets_count, 'cache': f"{path_tmp}/reencrypted/{src}"}
        if secrets_count:
            logging.debug(f"created reencrypted cache for {src}")
            shutil.copy(reencrypted[src]['cache'], dst)
        else:
            logging.debug(f"{src} has no secrets, copying as is")
            if os.path.exists(reencrypted[src]['cache']):
                # delete stale cache
                logging.debug(f"delete stale cache for {src}")
                os.remove(reencrypted[src]['cache'])
            shutil.copy(f"{src_base}/{src}", dst)
    # remember when we last refered this secret for housekeeping
    reencrypted[src]['last_access'] = timestamp


def run_server(commit_sha):
    timestamp = round(time.time())
    
    inventory = ConfigParser(allow_no_value=True)
    inventory.read(f'{GIT_LOCAL_PATH}/hosts')

    with open(f'{GIT_LOCAL_PATH}/site.yaml', 'r') as file:
        playbook = yaml.safe_load(file)


    host_groups, group_parents = parse_inventory(inventory)


    host_roles = {}
    groups_roles = {}
    all_host_groups = {}

    for host, groups in host_groups.items():
        # build host roles dicts
        full_group_list = sorted(set(add_parent_groups(groups, group_parents)))
        all_host_groups[host] = full_group_list
        role_hash = hash(' '.join(full_group_list))
        if role_hash not in groups_roles:
            groups_roles[role_hash] = []
            for play in playbook:
                if (type(play['tags']) is list and 'ansible-agent-run' in play['tags'] or
                    type(play['tags']) is str  and 'ansible-agent-run' == play['tags']):
                    if (type(play['hosts']) is list and set(play['hosts']) & set(['all'] + full_group_list) or
                        type(play['hosts']) is str  and play['hosts'] in ['all'] + full_group_list):
                            groups_roles[role_hash].append(play)
        host_roles[host] = role_hash


    location_template = '''location /{{ hostname }} {
    autoindex on;
    autoindex_format json;
    alias /var/www/ansible-server/{{ hostname }};
    {% for hostip in hostips -%}
    allow {{ hostip }};
    {% endfor -%}
    deny all;
    }

    '''

    nginx_config_tmp_fd = open(nginx_config_tmp, 'w')

    os.makedirs(f"{path_prod}/{timestamp}_{commit_sha}/roles")
    os.makedirs(f"{path_prod}/{timestamp}_{commit_sha}/group_vars")
    copied_roles = []
    copied_group_vars = []

    for role_hash, host_group_roles in groups_roles.items():

        path_role_hash = f"{path_prod}/{timestamp}_{commit_sha}/role_hash/{role_hash}"
        logging.debug(f"create {path_role_hash}")
        os.makedirs(f"{path_role_hash}")

        logging.debug(f"create {path_role_hash}/site.yaml = \n{host_group_roles}")
        with open(f"{path_role_hash}/site.yaml", 'w') as file:
            yaml.dump(host_group_roles, file, sort_keys=False)

        for host in [k for k,v in host_roles.items() if v == role_hash]:

            path_host = f"{path_tmp}/{host}/{timestamp}_{commit_sha}"
            logging.debug (f"create {path_host}")
            os.makedirs(f"{path_host}")

            # append to nginx config
            logging.info(host)
            try:
                ips = sorted(set(i[4][0] for i in socket.getaddrinfo(host, None)))
            except:
                logging.error(f'Failed to get host IPs: {host}')
            else:
                template = Template(location_template)
                rendered = template.render(hostname=host, hostips=ips)
                logging.debug(rendered)
                nginx_config_tmp_fd.write(rendered)

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

            # copy and symlink group_vars
            logging.debug (f"create {path_role_hash}/group_vars/{['all'] + all_host_groups[host]}")
            os.makedirs(f"{path_host}/group_vars")
            for host_group in ['all'] + all_host_groups[host]:
                if os.path.exists(f"{GIT_LOCAL_PATH}/group_vars/{host_group}"):
                    if host_group not in copied_group_vars:
                        if do_reencrypt:
                            copy_with_reencrypt(GIT_LOCAL_PATH, f"group_vars/{host_group}", f"{path_prod}/{timestamp}_{commit_sha}/group_vars/{host_group}")
                        else:
                            shutil.copy(f"{GIT_LOCAL_PATH}/group_vars/{host_group}", f"{path_prod}/{timestamp}_{commit_sha}/group_vars/{host_group}")
                        copied_group_vars.append(host_group)
                    os.symlink(f"{path_prod}/{timestamp}_{commit_sha}/group_vars/{host_group}", f"{path_host}/group_vars/{host_group}")


            if len(all_host_roles) > len(host_group_roles):
                # need dedicated site.yaml for this host
                logging.debug (f"create {path_host}/site.yaml = \n{all_host_roles}")
                with open(f"{path_host}/site.yaml", 'w') as file:
                    yaml.dump(all_host_roles, file, sort_keys=False)
            else:
                # can use shared site.yaml for this host
                logging.debug (f"symlink {path_host}/site.yaml -> {path_role_hash}/site.yaml")
                os.symlink(f"{path_role_hash}/site.yaml", f"{path_host}/site.yaml")

            # copy and symlink roles
            logging.debug (f"create {path_host}/roles")
            os.makedirs(f"{path_host}/roles")
            for role in set(ii['role']  for i in all_host_roles  for ii in i['roles']):
                if role not in copied_roles:
                    logging.debug(f"create {path_prod}/{timestamp}_{commit_sha}/roles/{role}")
                    shutil.copytree(f"{GIT_LOCAL_PATH}/roles/{role}", f"{path_prod}/{timestamp}_{commit_sha}/roles/{role}")
                    if do_reencrypt and os.path.exists(f"{GIT_LOCAL_PATH}/roles/{role}/vars"):
                        for vars in os.listdir(f"{GIT_LOCAL_PATH}/roles/{role}/vars"):
                            copy_with_reencrypt(GIT_LOCAL_PATH, f"roles/{role}/vars/{vars}", f"{path_prod}/{timestamp}_{commit_sha}/roles/{role}/vars/{vars}")
                    if do_reencrypt and os.path.exists(f"{GIT_LOCAL_PATH}/roles/{role}/defaults"):
                        for vars in os.listdir(f"{GIT_LOCAL_PATH}/roles/{role}/defaults"):
                            copy_with_reencrypt(GIT_LOCAL_PATH, f"roles/{role}/defaults/{vars}", f"{path_prod}/{timestamp}_{commit_sha}/roles/{role}/defaults/{vars}")
                    copied_roles.append(role)
                logging.debug(f"symlink {path_host}/roles/{role} -> {path_prod}/{timestamp}_{commit_sha}/roles/{role}")
                os.symlink(f"{path_prod}/{timestamp}_{commit_sha}/roles/{role}", f"{path_host}/roles/{role}")

            # move prepared folder structure to document root
            logging.debug (f"mv  {path_host}   {path_doc_root}/{host}/{timestamp}_{commit_sha}")
            shutil.move(f"{path_host}", f"{path_doc_root}/{host}/{timestamp}_{commit_sha}")


    nginx_config_tmp_fd.close()


    if os.path.exists(nginx_config_prod) and filecmp.cmp(nginx_config_prod, nginx_config_tmp):
        logging.info('nginx config has not changed')
    else:
        logging.info('update ngnix config')
        shutil.move(nginx_config_tmp, nginx_config_prod)
        os.popen("systemctl reload nginx.service")
    
    
    # Housekeeping
    logging.debug(f"Cleanup stale folders, preserving 3 last commits and folders younger than 2 hours")
    for folder in sorted(os.listdir(path_prod))[:-3]:
        folder_ts = folder.split('_')[0]
        if int(folder_ts) < timestamp - 7200:
            logging.info(f"Removing {folder} stale folders from {path_prod} and {path_doc_root}/*/")
            for host_folder in os.listdir(path_doc_root):
                if os.path.exists(f"{path_doc_root}/{host_folder}/{folder}"):
                    shutil.rmtree(f"{path_doc_root}/{host_folder}/{folder}")
            shutil.rmtree(f"{path_prod}/{folder}")

    folders_to_keep = os.listdir(path_prod)
    for host_folder in os.listdir(path_doc_root):
        folders = os.listdir(f"{path_doc_root}/{host_folder}")
        if len(folders) == 0:
            logging.info(f"Removing empty folder {path_doc_root}/{host_folder}")
            shutil.rmtree(f"{path_doc_root}/{host_folder}")
        elif host_folder not in host_groups.keys():
            logging.info(f"Removing folder for host not in inventory {path_doc_root}/{host_folder}")
            shutil.rmtree(f"{path_doc_root}/{host_folder}")
        else:
            for folder in folders:
                if folder == 'ansiblectl':
                    # housekeep ansiblectl folders
                    for ansiblectl_folder in os.listdir(f"{path_doc_root}/{host_folder}/ansiblectl"):
                        folder_ts = ansiblectl_folder.split('_')[0]
                        if int(folder_ts) < timestamp - 7500:
                            logging.info(f"Removing stale ansiblectl folder {path_doc_root}/{host_folder}/ansiblectl/{ansiblectl_folder}")
                            shutil.rmtree(f"{path_doc_root}/{host_folder}/ansiblectl/{ansiblectl_folder}")
                elif folder not in folders_to_keep:
                    logging.info(f"Removing stale folder {path_doc_root}/{host_folder}/{folder}")
                    shutil.rmtree(f"{path_doc_root}/{host_folder}/{folder}")
    
    # remove stale reencrypted secrets
    for src in [k for k,v in reencrypted.items() if v['last_access'] < timestamp - 86400]:
        logging.info(f"reencrypted {src} not refered for 24 hours, removing")
        if os.path.exists(reencrypted[src]['cache']):
            os.remove(reencrypted[src]['cache'])
        del reencrypted[src]




if os.path.exists(path_tmp):
    shutil.rmtree(path_tmp)
os.makedirs(path_tmp)

if not os.path.exists(path_prod):
    os.makedirs(path_prod)
if not os.path.exists(path_doc_root):
    os.makedirs(path_doc_root)


class CloneProgress(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        if message:
            logging.info(message)

if os.path.exists(GIT_LOCAL_PATH):
    shutil.rmtree(GIT_LOCAL_PATH)
os.makedirs(GIT_LOCAL_PATH)
git.Repo.clone_from(GIT_URL, GIT_LOCAL_PATH, branch=GIT_BRANCH, progress=CloneProgress(), config='http.sslVerify=false')


repo = git.Repo(GIT_LOCAL_PATH)
git_cmd = git.cmd.Git(GIT_LOCAL_PATH)
repo.git.checkout(GIT_BRANCH)
current_hash = repo.head.object.hexsha
logging.info(f'Running ansible-server for a start or restart: {current_hash}')
run_server(current_hash)


while True:
    time.sleep(TIMER_SCHEDULED_RUN_SEC)
    try:
        git_cmd.pull()
    except Exception as e:
        logging.error(f'Failed running git pull: {e}, line {sys.exc_info()[-1].tb_lineno}')
    new_hash = repo.head.object.hexsha
    if new_hash != current_hash:
        logging.info(f'Running ansible-server for a new commit: {new_hash}')
        run_server(new_hash)
        current_hash = new_hash
