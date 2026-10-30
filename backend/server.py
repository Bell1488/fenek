#!/usr/bin/env python3
"""Fenek WSGI server. Only Telegram delivery is routed through socks5h.
Static files are served from dist; secrets and SQLite always stay outside it.
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit, unquote
from wsgiref.simple_server import make_server
import mimetypes

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'dist'

def load_env(path: Path):
    if not path.is_file(): return
    for raw in path.read_text(encoding='utf-8').splitlines():
        raw = raw.strip()
        if not raw or raw.startswith('#'): continue
        key, sep, value = raw.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()): continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in '\"\'': value = value[1:-1]
        os.environ.setdefault(key.strip(), value)

load_env(ROOT / '.env')

class LeadError(Exception):
    def __init__(self, message, status=400): self.message, self.status = message, status

class DeliveryError(Exception):
    def __init__(self, uncertain=False): self.uncertain = uncertain


def settings():
    return {
        'proxy': os.getenv('SOCKS_PROXY_URL', ''),
        'bot': os.getenv('TELEGRAM_BOT_TOKEN', ''),
        'chat': os.getenv('TELEGRAM_CHAT_ID', ''),
        'origins': set(x.strip().rstrip('/') for x in os.getenv('ALLOWED_ORIGINS', 'https://fenek.pro,https://www.fenek.pro').split(',') if x.strip()),
        'db': Path(os.getenv('LEAD_DB_PATH', str(ROOT / 'private' / 'leads.sqlite3'))),
        'trust_proxy': os.getenv('TRUST_REVERSE_PROXY', '0') == '1',
        'proxy_cidrs': [x.strip() for x in os.getenv('TRUSTED_PROXY_CIDRS','127.0.0.1/32,::1/128').split(',') if x.strip()],
    }


def configured(config):
    try:
        proxy = urlsplit(config['proxy'])
        if proxy.scheme != 'socks5h' or not proxy.hostname or not proxy.port: return False
        if any(c in config['proxy'] for c in '\r\n'): return False
        if not re.fullmatch(r'\d{5,16}:[A-Za-z0-9_-]{20,100}', config['bot']): return False
        if not re.fullmatch(r'-?\d{3,30}|@[A-Za-z][A-Za-z0-9_]{4,40}', config['chat']): return False
        return True
    except (ValueError, KeyError): return False


def valid_contact(value):
    return bool(re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{4,31}', value) or
                (re.fullmatch(r'\+?[0-9 ()-]{10,25}', value) and 10 <= len(re.sub(r'\D', '', value)) <= 15))


def clean_text(value, maximum):
    if not isinstance(value, (str, int, float)): return ''
    return ''.join(c for c in str(value) if c in '\n\t' or ord(c) >= 32)[:maximum].strip()


def validate(data):
    if not isinstance(data, dict): raise LeadError('Неверный формат заявки.')
    contact = clean_text(data.get('contact', ''), 100)
    if not valid_contact(contact): raise LeadError('Укажите телефон с кодом страны или Telegram: @username.')
    if data.get('consent') is not True or data.get('consent_version') != '2026-10-30':
        raise LeadError('Подтвердите согласие на обработку персональных данных.')
    if data.get('website'): raise LeadError('Заявка не принята. Попробуйте ещё раз.', 400)
    request_id = str(data.get('request_id', ''))
    if not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}', request_id):
        raise LeadError('Обновите страницу и повторите отправку.')
    amount = clean_text(data.get('amount', ''), 30)
    if amount:
        try:
            number = float(amount)
            if not 0 < number <= 100000000: raise ValueError()
        except (ValueError, OverflowError): raise LeadError('Проверьте сумму заявки.')
    attrs = data.get('attribution', {})
    if not isinstance(attrs, dict): attrs = {}
    allowed_attrs = ['utm_source','utm_medium','utm_campaign','utm_content','utm_term','yclid','landing']
    return {
        'request_id': request_id.lower(), 'contact': contact,
        'service': clean_text(data.get('service', 'Консультация'), 100),
        'source': clean_text(data.get('source', 'form'), 80),
        'kind': 'cargo' if data.get('kind') == 'cargo' else 'payment',
        'amount': amount, 'currency': 'RUB' if data.get('currency') == 'RUB' else 'CNY',
        'details': clean_text(data.get('details', ''), 2500),
        'context': clean_text(data.get('context', ''), 5000),
        'page': clean_text(data.get('page', '/'), 120),
        'attribution': {k: clean_text(attrs[k], 250) for k in allowed_attrs if k in attrs},
        'consent_version': '2026-10-30',
    }


def connect_db(path):
    path = path.resolve()
    if path.is_relative_to(PUBLIC.resolve()): raise RuntimeError('DB must stay outside dist')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    os.chmod(path, 0o600)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, message_id TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS limits (ip_hash TEXT NOT NULL, created REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS limits_time ON limits(created)')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('ip_salt', secrets.token_hex(32)))
    db.execute('DELETE FROM leads WHERE created < ?', (time.time()-30*86400,))
    db.execute('DELETE FROM limits WHERE created < ?', (time.time()-3600,))
    return db


def delivery_message(lead):
    lines = ['FENEK.PRO · НОВАЯ ЗАЯВКА', 'ID: '+lead['request_id'], 'Контакт: '+lead['contact'], 'Услуга: '+lead['service']]
    if lead['amount']: lines.append('Сумма: '+lead['amount']+' '+lead['currency'])
    if lead['details']: lines.append('Комментарий: '+lead['details'])
    if lead['context']: lines.append('Параметры:\n'+lead['context'])
    lines += ['Источник формы: '+lead['source'], 'Страница: '+lead['page'], 'Согласие: версия '+lead['consent_version']]
    if lead['attribution']: lines.append('Реклама: '+json.dumps(lead['attribution'], ensure_ascii=False))
    text = '\n'.join(lines)
    # Telegram text limit counts UTF-16 units; keep a conservative bound.
    if len(text.encode('utf-16-le'))//2 > 3900:
        text = text.encode('utf-16-le')[:7600].decode('utf-16-le', errors='ignore')+'\n[Полная заявка сохранена на сервере]'
    return text


def deliver(lead, config):
    """No direct-network fallback. Proxy, DNS and credentials only in curl stdin."""
    if not configured(config): raise DeliveryError()
    payload = json.dumps({'chat_id':config['chat'], 'text':delivery_message(lead), 'disable_web_page_preview':True}, ensure_ascii=False)
    quote = lambda text: json.dumps(text, ensure_ascii=False)
    curl_config = '\n'.join([
        'url = '+quote('https://api.telegram.org/bot'+config['bot']+'/sendMessage'),
        'proxy = '+quote(config['proxy']), 'noproxy = ""',
        'request = "POST"', 'header = "Content-Type: application/json"',
        'data-binary = '+quote(payload), 'silent', 'fail',
        'connect-timeout = 10', 'max-time = 25', 'proto = "=https"',
        'max-filesize = 262144',
    ])+'\n'
    try:
        result = subprocess.run(['curl','--disable','--config','-'], input=curl_config, text=True, capture_output=True, timeout=28, check=False)
    except (subprocess.TimeoutExpired, OSError): raise DeliveryError(uncertain=True) from None
    if result.returncode:
        # 22 = explicit HTTP rejection; all other failures may be ambiguous.
        raise DeliveryError(uncertain=result.returncode != 22)
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict): raise DeliveryError(uncertain=True)
        if response.get('ok') is not True: raise DeliveryError()
        return str(response['result']['message_id'])
    except (KeyError, TypeError, ValueError): raise DeliveryError(uncertain=True) from None


def client_ip(environ, config):
    ip = environ.get('REMOTE_ADDR', '')
    if config['trust_proxy']:
        # Trust only configured reverse-proxy networks. Nginx must overwrite this header.
        try:
            remote = ipaddress.ip_address(ip)
            if any(remote in ipaddress.ip_network(cidr) for cidr in config.get('proxy_cidrs',['127.0.0.1/32','::1/128'])):
                ip = str(ipaddress.ip_address(environ.get('HTTP_X_REAL_IP', ip)))
        except ValueError: pass
    return ip[:100]


def process_lead(raw, environ, config):
    origin = environ.get('HTTP_ORIGIN', '').rstrip('/')
    if origin not in config['origins']: raise LeadError('Отправка с этого адреса не разрешена.', 403)
    try: lead = validate(json.loads(raw))
    except (ValueError, UnicodeDecodeError): raise LeadError('Неверный формат заявки.') from None
    if not configured(config): raise LeadError('Сейчас отправка недоступна. Напишите нам в Telegram или MAX.', 503)
    fingerprint = hashlib.sha256(json.dumps(lead, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    db = connect_db(config['db'])
    try:
        db.execute('BEGIN IMMEDIATE')
        found = db.execute('SELECT fingerprint,state FROM leads WHERE id=?', (lead['request_id'],)).fetchone()
        if found:
            if found[0] != fingerprint: raise LeadError('Данные заявки изменились. Обновите форму.', 409)
            if found[1] == 'delivered':
                db.commit()
                return {'ok':True,'delivered':True,'request_id':lead['request_id'],'duplicate':True}
            if found[1] in ('pending','uncertain'):
                raise LeadError('Статус заявки уточняется. Не отправляйте повторно — свяжитесь с менеджером и назовите ID '+lead['request_id'][:8]+'.', 409)
        salt = db.execute('SELECT value FROM meta WHERE key=?', ('ip_salt',)).fetchone()[0]
        ip_hash = hashlib.sha256((salt+client_ip(environ, config)).encode()).hexdigest()
        since = time.time()-600
        if db.execute('SELECT COUNT(*) FROM limits WHERE ip_hash=? AND created>?', (ip_hash,since)).fetchone()[0] >= 10:
            raise LeadError('Слишком много отправок. Попробуйте позднее или напишите менеджеру.', 429)
        now = time.time()
        if found: db.execute('UPDATE leads SET state=?,updated=? WHERE id=?', ('pending',now,lead['request_id']))
        else: db.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?)', (lead['request_id'],fingerprint,json.dumps(lead,ensure_ascii=False),'pending',now,now,None))
        db.execute('INSERT INTO limits VALUES (?,?)', (ip_hash,now))
        db.commit()
        try: message_id = deliver(lead, config)
        except DeliveryError as error:
            db.execute('UPDATE leads SET state=?,updated=? WHERE id=?', ('uncertain' if error.uncertain else 'failed',time.time(),lead['request_id']))
            raise LeadError('Не удалось подтвердить доставку заявки. Она сохранена под ID '+lead['request_id'][:8]+'. Свяжитесь с нами в Telegram или MAX.', 502) from None
        db.execute('UPDATE leads SET state=?,updated=?,message_id=? WHERE id=?', ('delivered',time.time(),message_id,lead['request_id']))
        return {'ok':True,'delivered':True,'request_id':lead['request_id']}
    finally:
        if db.in_transaction: db.rollback()
        db.close()


def json_response(start, status, data):
    body=json.dumps(data,ensure_ascii=False).encode()
    labels={200:'OK',400:'Bad Request',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',413:'Content Too Large',415:'Unsupported Media Type',429:'Too Many Requests',500:'Internal Server Error',502:'Bad Gateway',503:'Service Unavailable'}
    start(str(status)+' '+labels.get(status,'Error'), [('Content-Type','application/json; charset=utf-8'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')])
    return [body]


def application(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    method = environ.get('REQUEST_METHOD','GET')
    if path == '/api/lead':
        if method != 'POST': return json_response(start_response,405,{'ok':False,'message':'Используйте форму на сайте.'})
        if environ.get('CONTENT_TYPE','').split(';')[0] != 'application/json': return json_response(start_response,415,{'ok':False,'message':'Неверный формат заявки.'})
        try:
            size=int(environ.get('CONTENT_LENGTH') or 0)
            if size <= 0 or size > 32768: raise LeadError('Проверьте размер заявки.',413)
            raw=environ['wsgi.input'].read(size)
            result=process_lead(raw,environ,settings())
            return json_response(start_response,200,result)
        except LeadError as error: return json_response(start_response,error.status,{'ok':False,'delivered':False,'message':error.message})
        except Exception:
            # Never log payload, bot token, proxy URL, curl config or exception text.
            print('Fenek: lead handler internal failure', file=sys.stderr)
            return json_response(start_response,500,{'ok':False,'message':'Не удалось отправить заявку. Напишите нам в Telegram или MAX.'})
    if method not in ('GET','HEAD'): return json_response(start_response,405,{'ok':False})
    relative = unquote(path).lstrip('/') or 'index.html'
    target=(PUBLIC/relative).resolve()
    allowed={'.html','.js','.css','.svg','.png','.webp','.jpg','.jpeg','.ico','.txt','.xml'}
    if not target.is_relative_to(PUBLIC.resolve()) or not target.is_file() or target.suffix not in allowed:
        return json_response(start_response,404,{'ok':False,'message':'Страница не найдена.'})
    body=target.read_bytes()
    content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
    headers=[('Content-Type',content_type+('; charset=utf-8' if target.suffix in {'.html','.js','.css','.txt','.xml','.svg'} else '')),('Content-Length',str(len(body))),('X-Content-Type-Options','nosniff'),('Referrer-Policy','strict-origin-when-cross-origin')]
    headers.append(('Cache-Control','no-cache' if target.suffix in {'.html','.js'} else 'public, max-age=3600'))
    start_response('200 OK',headers)
    return [b'' if method=='HEAD' else body]

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='pending':
        cfg=settings()
        if not cfg['db'].exists(): print('No pending requests');sys.exit(0)
        db=connect_db(cfg['db'])
        for row in db.execute("SELECT id,state,created FROM leads WHERE state != 'delivered' ORDER BY created DESC"):
            print(*row)
        db.close()
    elif len(sys.argv)>1 and sys.argv[1] in ('cleanup','cleanup-loop'):
        while True:
            cfg=settings()
            if cfg['db'].exists():
                db=connect_db(cfg['db']);db.close()
            if sys.argv[1]=='cleanup': break
            time.sleep(3600)
    elif len(sys.argv)==3 and sys.argv[1]=='show':
        cfg=settings();db=connect_db(cfg['db']);row=db.execute('SELECT payload,state FROM leads WHERE id=?',(sys.argv[2],)).fetchone()
        print(json.dumps({'payload':json.loads(row[0]),'state':row[1]},ensure_ascii=False,indent=2) if row else 'Not found');db.close()
    else:
        host=os.getenv('HOST','127.0.0.1');port=int(os.getenv('PORT','8080'))
        print('Fenek local server: http://'+host+':'+str(port)+' (use Gunicorn/Nginx in production)')
        with make_server(host,port,application) as server: server.serve_forever()
