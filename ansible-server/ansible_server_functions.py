#!/usr/bin/python3

import re
import os
import sys
import logging
import hashlib


def sha256(file):
    sha256_hash = hashlib.sha256()
    with open(file, "rb") as f:
        # Read and update hash string value in blocks of 4K
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def reencrypt(src, dst, vault, new_vault):
    # not memory efficient, but fast - ok since we are not expecting yamls to be large
    with open(src, 'rb') as file:
        lines = file.readlines()
    output = b""
    secret = b""
    secrets_count = 0
    # add an empty element, so that if secret is in very end of the file, loop will still detect it
    for line in lines + [b""]:
        if re.match(b"^\s*\$ANSIBLE_VAULT;1.[12];AES256\s*$", line):
            # start of secret
            secret = line.strip()
        elif secret and re.match(b"^\s*[\da-f]+\s*$", line):
            # reading secret
            secret += b"\n" + line.strip()
        elif secret:
            # end of secret
            try:
                new_secret = new_vault.encrypt(vault.decrypt(secret))
                output += re.sub(b'(.+\n)', b'          \\1', new_secret)
            except Exception as e:
                logging.error(f'Failed to reencrypt secret in {src}, copying secret as is: {e}, line {sys.exc_info()[-1].tb_lineno}')
                output += secret
            else:
                secrets_count += 1
            secret = ''
            output += line
        else:
            # other lines
            output += line
    if secrets_count:
        if not os.path.exists(os.path.dirname(dst)):
            os.makedirs(os.path.dirname(dst))
        with open(dst, 'wb') as file:
            file.write(output)
    return secrets_count


def add_parent_groups(groups, group_parents):
    # recursively find all parent groups
    for group in groups:
        if group in group_parents:
            groups = groups + add_parent_groups(group_parents[group], group_parents)
    return groups


def parse_inventory(inventory):
    group_parents = {}
    host_groups = {}

    for group in inventory.sections():
        # find nested groups
        if re.match('.*:children$', group):
            groupname = group.split(':')[0]
            for child in inventory[group]:
                if child in group_parents:
                    group_parents[child].append(groupname)
                else:
                    group_parents[child] = [groupname]
        # fill host groups dict
        else:
            for host in inventory[group]:
                hostname = host.split()[0]
                if hostname in host_groups:
                    host_groups[hostname].append(group)
                else:
                    host_groups[hostname] = [group]

    return host_groups, group_parents