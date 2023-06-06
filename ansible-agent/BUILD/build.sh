#!/bin/bash

cd "$(dirname "$0")"
get_available_os=$(ls Dockerfile_*)
list_available_os=()
outputpwd=$(pwd)
cp ../ansible-agent.py ../ansiblectl.py ./

for a  in $get_available_os
do
 list_available_os+=("${a##*_}")
done

for value in "${list_available_os[@]}"
do 
     if [[ $1 == $value ]]
     then
       echo 'start build'
       Varfound='find'
       # Read Password
       echo -n 'Set Vault Password for asnible-agent if not use leave blank:':
       read -s valtpassword
       # Run Command
       if [ -z "$valtpassword" ] 
         then
          echo 'not uses Vaultpassword' 
         else
          echo -n "set Vaultpassword in ansible-agent.py in $outputpwd" 
          sed -i.bak "s/VAULTSECRET_TMP/$valtpassword/" ansible-agent.py
       fi
       docker build -t ansible-agent:$1 -f Dockerfile_$1 .
       rm -f ansible-agent.py ansible-agent.py.bak ansiblectl.py
       containerID=$(docker run -d ansible-agent:$1)
       echo $containerID
       docker cp $containerID:/opt/ansible-agent/dist/ansible-agent ./ansible-agent-$1
       docker cp $containerID:/opt/ansible-agent/dist/ansiblectl ./ansiblectl-$1
       sleep 2
       docker rm  $containerID
     fi
done


if [ -z $Varfound ]
then

case $1 in
     list)
          echo 'available OS from Dockerfiles:'
          for value in "${list_available_os[@]}"
            do
             echo $value
            done
          ;;
     help)
          echo "build.sh list - show avalible dockerfile with os in path $outputpwd"
          echo 'build.sh debian11 - example for start build ansible-agent for debian11,  show avalible os use command: build.sh list'
          ;;
     *)
          echo -e "usage build.sh: \n build.sh help - use for more help"  
          ;;
esac
fi
