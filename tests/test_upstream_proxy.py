"""Optional outbound proxy: validation, secrecy and CONNECT tunnelling."""
import base64
import socket
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import app_config
from app_config import parse_proxy_url, set_upstream_proxy, upstream_connection
from diagnostics import load_options, public_options, safe_record


class UpstreamProxyTests(unittest.TestCase):
    def tearDown(self):
        set_upstream_proxy('')

    def test_parse(self):
        self.assertIsNone(parse_proxy_url(''))
        self.assertEqual(parse_proxy_url('http://127.0.0.1:3128'), ('127.0.0.1', 3128, {}))
        self.assertEqual(parse_proxy_url('http://proxy.example/'), ('proxy.example', 80, {}))
        host, port, headers = parse_proxy_url('http://us%40er:p%3Ass@proxy.example:8080')
        self.assertEqual((host, port), ('proxy.example', 8080))
        self.assertEqual(base64.b64decode(headers['Proxy-Authorization'].removeprefix('Basic ')), b'us@er:p:ss')
        for bad in ('https://proxy:443', 'socks5://proxy:1080', 'proxy:8080', 'http://proxy:8080/path',
                    'http://proxy:99999', 'http://proxy:8080?x=1', 'http:// proxy:8080', 1):
            with self.assertRaises(ValueError, msg=bad):
                parse_proxy_url(bad)

    def test_options_validated_and_never_reported(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'proxy-options.json'
            self.assertEqual(load_options(folder)['upstream_proxy'], '')
            path.write_text('{"upstream_proxy": "http://user:secret@proxy.example:8080"}')
            options = load_options(folder)
            self.assertEqual(options['upstream_proxy'], 'http://user:secret@proxy.example:8080')
            public = public_options(options)
            self.assertNotIn('upstream_proxy', public)
            self.assertTrue(public['upstream_proxy_configured'])
            self.assertNotIn('secret', repr(safe_record(public)))
            self.assertTrue(safe_record(public)['upstream_proxy_configured'])
            path.write_text('{"upstream_proxy": "https://proxy.example"}')
            with self.assertRaises(ValueError):
                load_options(folder)

    def test_sign_in_bypass_option(self):
        from token_helper import sign_in_proxy
        url = 'http://proxy.example:8080'
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'proxy-options.json'
            self.assertFalse(load_options(folder)['sign_in_bypass_proxy'])
            self.assertEqual(sign_in_proxy(folder), '')
            path.write_text('{"upstream_proxy": "%s"}' % url)
            self.assertEqual(sign_in_proxy(folder), url)
            self.assertIsNotNone(app_config._upstream_proxy)
            path.write_text('{"upstream_proxy": "%s", "sign_in_bypass_proxy": true}' % url)
            self.assertEqual(sign_in_proxy(folder), '')
            # Gateway calls in the helper (model catalog) still use the proxy.
            self.assertEqual(app_config._upstream_proxy[:2], ('proxy.example', 8080))
            self.assertTrue(safe_record(public_options(load_options(folder)))['sign_in_bypass_proxy'])
            path.write_text('{"sign_in_bypass_proxy": "yes"}')
            with self.assertRaises(ValueError):
                load_options(folder)

    def test_direct_by_default(self):
        set_upstream_proxy('')
        connection = upstream_connection('loki.delve.office.com', 5)
        self.assertEqual((connection.host, connection.port), ('loki.delve.office.com', 443))
        self.assertIsNone(connection._tunnel_host)

    def test_connect_tunnel_through_proxy(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        received = []

        def serve():
            client, _ = listener.accept()
            with client:
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                received.append(data.decode('latin-1'))
                client.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            set_upstream_proxy('http://user:pw@127.0.0.1:%d' % listener.getsockname()[1])
            connection = upstream_connection('loki.delve.office.com', 5)
            with self.assertRaises(OSError):
                connection.request('GET', '/api/v1/llmapi/models')
            connection.close()
            thread.join(5)
        finally:
            listener.close()
        request = received[0]
        self.assertTrue(request.startswith('CONNECT loki.delve.office.com:443 HTTP/1.'))
        self.assertIn('Proxy-Authorization: Basic ' + base64.b64encode(b'user:pw').decode(), request)
        self.assertNotIn('/api/v1/llmapi', request)
        self.assertIsNotNone(app_config._upstream_proxy)


if __name__ == '__main__':
    unittest.main()
