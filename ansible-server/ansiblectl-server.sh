#!/bin/bash
# linux will ignore setuid (chmod u+s) on non-binaries (.sh, .py, ...) for security reasons, so using this helper script
cat - | sudo -E ./ansiblectl-server.py
