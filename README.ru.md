# About
Ansible-agent позволяет выполнять плейбуки на большой инфраструктуре так же быстро как на отдельном хосте

Предпосылки: плейбуки, выполняемые централизованно (например, в Tower/AWX), вынуждены ждать завершения таска на всех хостах в прогоне/слайсе прежде чем переходить к следующему таску. Всегда будут медленные хосты - и всего одного такого достаточно, чтобы драматически замедлить весь прогон  
Ansible-agent обходит эту проблему, перекладывая выполнение плейбуков на сами хосты

Есть один недостаток ansible-agent - он скачивает весь проект git со всеми секретами на каждый хост. Здесь в игру вступает ansible-server - он стоит между git и агентами и подготавливает минимально достаточную часть проекта индивидуально для каждого хоста, дополнительно перешифровывая секреты, так что vault password используемый в ansible-agent не может быть использован для расшифрования проекта в git.  


# Setup ansible-agent
- установите vault password в `./ansible-agent/ansible-agent.py` (строка `VAULTSECRET = 'VAULTSECRET_TMP'`)
  - или соберите бинарь `bash ./ansible-agent/BUILD/build.sh` - рекомендуется, т.к. скрывает vault password
- отредактируйте `./ansible-agent/ansible-agent.conf.j2` - установите `git_url` (для тестирования или если у вас в проекте нет чувствительной информации) или `server_url` чтобы использовать ansible-server (рекомендуется)
- и разверните с помощью плейбука:
```
ansible-playbook deploy-agent.yaml \
  -i host01.tld,host02.tld, \
  -e use_git=true -e GIT_USER=username -e GIT_TOKEN=token \
  -e ca_bundle=cabundle.crt \
  -e use_agent_binary=false
```
- если решили использовать ansible-server, удалите `-e use_git=true -e GIT_USER=username -e GIT_TOKEN=token` и используйте значение `new_vault_password` из конфигурации ansible-server в качестве vault password для агента
- `-e ca_bundle=cabundle.crt` предназначен для ваших корпоративных CA сертификатов.  
Используется при взаимодействии с ansible-server, можно удалить, если используете git или если ваш сервер использует сертификат от общепризнанных CA
- если решили собрать бинарь агента, установите `-e use_agent_binary=true`


# Setup ansible-server
Основная мысль - отдавать ansible-agent минимально достаточную часть проекта, так что скомпрометированный хост выдаст лишь конфигурацию и секреты, которые и так уже есть где-то в /etc, и не сильно помогут атакующему.
- отредактируйте `./ansible-server/ansible-server.conf.j2`:
  - установите `git_url`
  - если хотите, чтобы ansible-server перешифровывал секреты - установите `vault_password` (для расшифрования секретов в проекте git) и `new_vault_password` (для перешифрования для агентов) 
- и разверните:
```
ansible-playbook deploy-server.yaml \
  -i ansible-srv.tld, \
  -e GIT_USER=username -e GIT_TOKEN=token
```
обратите внимание на `,` в параметре `-i` - она требуется, чтобы ansible воспринимал значение как список, иначе ansible будет искать файл `ansible-srv.tld` в текущей папке
