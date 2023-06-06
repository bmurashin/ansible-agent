# About
Ansible-agent is a way to run playbooks in a big infrastructure as fast as on a single host

Motivation: playbooks, executed centrally (e.g. by Tower/AWX), have to wait for task to be completed on every node in a run/slice before proceeding to next task. Slow hosts happen - and just one such host can dramatically slow down the whole playbook run  
Ansible-agent works around this problem by offloading playbooks execution to hosts themselfs

One shortcoming of ansible-agent is that it downloads whole git project with all secrets to every node. Here ansible-server comes into play - it stands between git and agents and prepares minimally sufficient piece of project individually for every host, additionally reencrypting secrets, so that vault password used by ansible-agent can't be used to decrypt the git project.  


# Setup ansible-agent
- set vault password in `./ansible-agent/ansible-agent.py` (find `VAULTSECRET = 'VAULTSECRET_TMP'`)
  - or build agent binary `bash ./ansible-agent/BUILD/build.sh` - recomended as it obfuscates vault password
- edit `./ansible-agent/ansible-agent.conf.j2` - set `git_url` (for testing or if your project has no sensitive data) or `server_url` to use ansible-server (recommended)
- and deploy using playbook:
```
ansible-playbook deploy-agent.yaml \
  -i host01.tld,host02.tld, \
  -e use_git=true -e GIT_USER=username -e GIT_TOKEN=token \
  -e ca_bundle=cabundle.crt \
  -e use_agent_binary=false
```
- if you choose to use ansible-server, remove `-e use_git=true -e GIT_USER=username -e GIT_TOKEN=token` and use `new_vault_password` value from ansible-server configuration as vault password for agent
- `-e ca_bundle=cabundle.crt` is intented for your corporate CA certs.  
Used for communication with ansible-server, can be removed if you go with git or if your server use certs from widely trusted CAs
- if you choose to build agent binary, set `-e use_agent_binary=true`


# Setup ansible-server
Main point is to provide ansible-agent with minimally sufficient piece of project, so that any compromised host exposes only configurations and secrets, that are already exist somewhere in /etc, and will not be of much help for an attacker.
- edit `./ansible-server/ansible-server.conf.j2`:
  - set `git_url`
  - if you want ansible-server to reencrypt secrets - set `vault_password` (to decrypt secrets from git project) and `new_vault_password` (to reencrypt secrets for agents) 
- and deploy:
```
ansible-playbook deploy-server.yaml \
  -i ansible-srv.tld, \
  -e GIT_USER=username -e GIT_TOKEN=token
```
note comma `,` in the `-i` parameter - it is requred for ansible to treat value as list, otherwise ansible will look for `ansible-srv.tld` file in current folder
