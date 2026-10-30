#!/usr/bin/env python3
"""Read-only Telegram API check over mandatory socks5h. Sends no messages."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys

path=Path(__file__).resolve().parents[1]/'backend'/'server.py'
spec=importlib.util.spec_from_file_location('sirokko_server_check',path)
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
cfg=server.settings()
if not server.configured(cfg):
    print('Проверьте SOCKS_PROXY_URL, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в серверном .env.');sys.exit(1)
for method,payload in [('getMe',None),('getChat',{'chat_id':cfg['chat']})]:
    q=lambda value:json.dumps(value)
    options=['url = '+q('https://api.telegram.org/bot'+cfg['bot']+'/'+method),'proxy = '+q(cfg['proxy']),'noproxy = ""','silent','fail','connect-timeout = 8','max-time = 20','proto = "=https"']
    if payload:options+=['request = "POST"','header = "Content-Type: application/json"','data-binary = '+q(json.dumps(payload))]
    try:
        result=subprocess.run(['curl','--disable','--config','-'],input='\n'.join(options)+'\n',text=True,capture_output=True,timeout=23,check=False)
    except (OSError,subprocess.TimeoutExpired):
        print('Не удалось выполнить запрос через SOCKS. Проверьте curl, доступ к прокси и сетевые ограничения сервера.');sys.exit(2)
    if result.returncode:
        print('Ошибка запроса через SOCKS, curl code '+str(result.returncode)+'. Прямого обхода нет. Проверьте прокси и доступность Telegram API с VPS.');sys.exit(2)
    try:response=json.loads(result.stdout)
    except ValueError:print('Некорректный ответ API.');sys.exit(3)
    if not isinstance(response,dict) or response.get('ok') is not True:
        print('Telegram API отклонил '+method+'. Проверьте токен, ID чата и доступ бота.');sys.exit(3)
    print('Бот авторизован.' if method=='getMe' else 'Чат доступен боту.')
print('Чтение Telegram API через socks5h работает. Тестовое сообщение не отправлялось; доставку проверьте заявкой с сайта.')
