import json
import socket
import urllib.request
import urllib.error


class ApiError(Exception):
    """Exception raised when API response ok=false"""
    pass


class ApiClient:
    """Unified HTTP client for QQ API and Launcher API"""

    def __init__(self, qq_port=9420, launcher_port=9421, timeout=5):
        self.qq_port = qq_port
        self.launcher_port = launcher_port
        self.timeout = timeout
        self.host = "127.0.0.1"

    def _request(self, method, host, port, path, body=None):
        """Internal method to build URL and send HTTP request"""
        url = f"http://{host}:{port}{path}"

        if body is not None:
            body_bytes = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(url, data=body_bytes, method=method)
            req.add_header('Content-Type', 'application/json')
        else:
            req = urllib.request.Request(url, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_data = resp.read().decode('utf-8')
                envelope = json.loads(resp_data)

                if not envelope.get('ok', False):
                    raise ApiError(envelope.get('error', ''))

                return envelope.get('data', {})

        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot connect to {host}:{port}") from e
        except socket.timeout as e:
            raise TimeoutError(f"Timeout after {self.timeout}s") from e

    def health(self):
        """GET /health → returns True on success"""
        self._request('GET', self.host, self.qq_port, '/health')
        return True

    def get_logs(self, since=0, max_lines=100):
        """GET /logs?since=N&max=M → returns data dict"""
        path = f"/logs?since={since}&max={max_lines}"
        return self._request('GET', self.host, self.qq_port, path)

    def get_stats(self):
        """GET /stats → returns data dict"""
        return self._request('GET', self.host, self.qq_port, '/stats')

    def get_config(self):
        """GET /config → returns data dict"""
        return self._request('GET', self.host, self.qq_port, '/config')

    def set_config(self, new_config):
        """PUT /config with JSON body → returns data dict"""
        return self._request('PUT', self.host, self.qq_port, '/config', body=new_config)

    def trigger_daemon(self):
        """POST /action/trigger-daemon → returns data dict"""
        return self._request('POST', self.host, self.qq_port, '/action/trigger-daemon')

    def launcher_status(self):
        """GET /status → returns data dict"""
        return self._request('GET', self.host, self.launcher_port, '/status')

    def launcher_start(self):
        """POST /start → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/start')

    def launcher_stop(self):
        """POST /stop → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/stop')

    def launcher_start_scraper(self):
        """POST /start with target=scraper → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/start', body={"target": "scraper"})

    def launcher_stop_scraper(self):
        """POST /stop with target=scraper → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/stop', body={"target": "scraper"})

    def launcher_restart(self):
        """POST /restart → returns data dict (may block up to 30s)"""
        return self._request('POST', self.host, self.launcher_port, '/restart')

    def launcher_shutdown(self):
        """POST /shutdown → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/shutdown')

    def webapp_start(self):
        """POST /webapp/start → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/webapp/start')

    def webapp_stop(self):
        """POST /webapp/stop → returns data dict"""
        return self._request('POST', self.host, self.launcher_port, '/webapp/stop')

    def webapp_status(self):
        """GET /webapp/status → returns data dict"""
        return self._request('GET', self.host, self.launcher_port, '/webapp/status')