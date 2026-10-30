import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

spec=importlib.util.spec_from_file_location('fenek_server',Path(__file__).parents[1]/'backend'/'server.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
class LeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'proxy':'socks5h://test:pass@127.0.0.1:12345','bot':'123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi','chat':'-1001234567890','origins':{'https://fenek.pro'},'db':Path(self.tmp.name)/'private'/'leads.sqlite3','trust_proxy':False}
        self.env={'HTTP_ORIGIN':'https://fenek.pro','REMOTE_ADDR':'127.0.0.1'}
        self.lead={'request_id':str(uuid.uuid4()),'contact':'@test_user','consent':True,'consent_version':'2026-10-30','kind':'payment','service':'Alipay','details':'Тест, без реальной отправки','page':'/'}
    def tearDown(self):self.tmp.cleanup()
    def send(self,data=None):return s.process_lead(json.dumps(data or self.lead).encode(),self.env,self.config)
    def test_validation(self):
        self.assertTrue(s.valid_contact('+7 (999) 123-45-67'))
        self.assertTrue(s.valid_contact('@manager_123'))
        self.assertFalse(s.valid_contact('1234'))
        self.assertFalse(s.valid_contact('this is not a contact'))
        for change in [{'consent':False},{'contact':'oops'},{'website':'spam.example'},{'amount':'NaN'},{'request_id':'invalid'}]:
            with self.assertRaises(s.LeadError):self.send({**self.lead,**change})
    def test_origin_rejected(self):
        self.env['HTTP_ORIGIN']='https://attacker.example'
        with self.assertRaises(s.LeadError) as c:self.send()
        self.assertEqual(c.exception.status,403)
    def test_no_direct_fallback_if_unconfigured(self):
        self.config['proxy']='socks5://127.0.0.1:12345'
        with patch.object(s.subprocess,'run') as run:
            with self.assertRaises(s.LeadError) as c:self.send()
            self.assertEqual(c.exception.status,503);run.assert_not_called()
    def test_success_and_idempotency(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'ok':True,'result':{'message_id':42}}),'')
        with patch.object(s.subprocess,'run',return_value=response) as run:
            a=self.send();b=self.send()
            self.assertTrue(a['delivered']);self.assertTrue(b['duplicate']);self.assertEqual(run.call_count,1)
            args,kwargs=run.call_args
            self.assertEqual(args[0],['curl','--disable','--config','-'])
            self.assertNotIn(self.config['bot'],' '.join(args[0]))
            self.assertIn('proxy = "socks5h://',kwargs['input'])
            self.assertIn('noproxy = ""',kwargs['input'])
            self.assertIn('@test_user',kwargs['input'])
            self.assertIn('proto = "=https"',kwargs['input'])
            db=sqlite3.connect(self.config['db']);self.assertEqual(db.execute('SELECT state FROM leads').fetchone()[0],'delivered');db.close()
    def test_proxy_failure_does_not_claim_success_or_retry(self):
        response=subprocess.CompletedProcess([],7,'','sensitive proxy error is never returned')
        with patch.object(s.subprocess,'run',return_value=response) as run:
            with self.assertRaises(s.LeadError) as c:self.send()
            self.assertEqual(c.exception.status,502)
            self.assertNotIn('sensitive',c.exception.message)
            with self.assertRaises(s.LeadError) as c:self.send()
            self.assertEqual(c.exception.status,409);self.assertEqual(run.call_count,1)
    def test_invalid_telegram_response(self):
        response=subprocess.CompletedProcess([],0,json.dumps({'ok':False,'description':'failure'}),'')
        with patch.object(s.subprocess,'run',return_value=response):
            with self.assertRaises(s.LeadError):self.send()
    def test_request_id_cannot_change_payload(self):
        with patch.object(s,'deliver',return_value='42'):
            self.send()
            with self.assertRaises(s.LeadError) as c:self.send({**self.lead,'contact':'@different_user'})
            self.assertEqual(c.exception.status,409)
    def test_rate_limit(self):
        with patch.object(s,'deliver',return_value='42'):
            for _ in range(10):self.send({**self.lead,'request_id':str(uuid.uuid4())})
            with self.assertRaises(s.LeadError) as c:self.send({**self.lead,'request_id':str(uuid.uuid4())})
            self.assertEqual(c.exception.status,429)
    def test_wsgi_success_and_failure(self):
        status=[]
        def start(value,headers):status.append(value)
        raw=json.dumps(self.lead).encode()
        environ={**self.env,'PATH_INFO':'/api/lead','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        with patch.object(s,'settings',return_value=self.config),patch.object(s,'deliver',return_value='42'):
            response=json.loads(b''.join(s.application(environ,start)))
            self.assertEqual(status[-1],'200 OK');self.assertTrue(response['delivered'])
        environ.update(REQUEST_METHOD='GET')
        response=json.loads(b''.join(s.application(environ,start)))
        self.assertEqual(status[-1],'405 Method Not Allowed');self.assertFalse(response['ok'])
    def test_private_files_not_served(self):
        status=[]
        for path in ['/.env','/backend/server.py','/../.env','/private/leads.sqlite3']:
            result=s.application({'PATH_INFO':path,'REQUEST_METHOD':'GET'},lambda v,h:status.append(v))
            self.assertEqual(status[-1],'404 Not Found')
    def test_reverse_proxy_does_not_share_all_visitors_limit(self):
        config={**self.config,'trust_proxy':True,'proxy_cidrs':['172.16.0.0/12']}
        self.assertEqual(s.client_ip({'REMOTE_ADDR':'172.18.0.1','HTTP_X_REAL_IP':'203.0.113.7'},config),'203.0.113.7')
        self.assertEqual(s.client_ip({'REMOTE_ADDR':'203.0.113.9','HTTP_X_REAL_IP':'203.0.113.7'},config),'203.0.113.9')
    def test_socks5h_uses_remote_domain(self):
        # A local rejecting SOCKS server observes the DOMAIN request. It never
        # connects to Telegram or forwards a byte to the Internet.
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(8)
        seen=[]
        def read_n(conn,n):
            data=b''
            while len(data)<n:
                piece=conn.recv(n-len(data))
                if not piece:raise RuntimeError('short SOCKS frame')
                data+=piece
            return data
        def proxy():
            try:
                conn,_=listener.accept()
                with conn:
                    conn.settimeout(8);head=read_n(conn,2);read_n(conn,head[1]);conn.sendall(b'\x05\x00')
                    req=read_n(conn,4);seen.append(req[3]);size=read_n(conn,1)[0];seen.append(read_n(conn,size).decode());read_n(conn,2)
                    conn.sendall(b'\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00')
            finally:listener.close()
        thread=threading.Thread(target=proxy,daemon=True);thread.start()
        config={**self.config,'proxy':'socks5h://127.0.0.1:'+str(listener.getsockname()[1])}
        with self.assertRaises(s.DeliveryError):s.deliver(s.validate(self.lead),config)
        thread.join(timeout=9)
        self.assertEqual(seen,[3,'api.telegram.org'])

if __name__=='__main__':unittest.main()
