from .api_client import ApiClient


class PrometheusApiClient:
    """Wrapper around ApiClient for QQ API (dashboard + config)."""

    def __init__(self, client: ApiClient):
        self.client = client

    def is_healthy(self) -> bool:
        try:
            self.client.health()
            return True
        except Exception:
            return False

    def get_dashboard_data(self) -> dict:
        result = {}
        error = None

        for key, getter in (
            ("stats", self.client.get_stats),
            ("logs", self.client.get_logs),
            ("config", self.client.get_config),
        ):
            try:
                result[key] = getter()
            except Exception as e:
                error = str(e) if error is None else error

        if error is not None:
            result["error"] = error
        return result

    def update_config(self, new_config: dict) -> dict:
        return self.client.set_config(new_config)

    def trigger_daemon(self) -> dict:
        return self.client.trigger_daemon()


class LauncherApiClient:
    """Wrapper around ApiClient for Launcher API (QQ process lifecycle)."""

    def __init__(self, client: ApiClient):
        self.client = client

    def get_status(self) -> dict:
        return self.client.launcher_status()

    def start_qq(self) -> dict:
        return self.client.launcher_start()

    def stop_qq(self) -> dict:
        return self.client.launcher_stop()

    def start_scraper(self) -> dict:
        return self.client.launcher_start_scraper()

    def stop_scraper(self) -> dict:
        return self.client.launcher_stop_scraper()

    def restart_qq(self) -> dict:
        try:
            return self.client.launcher_restart()
        except TimeoutError:
            return {"ok": False, "error": "restart timeout"}

    def shutdown(self) -> dict:
        return self.client.launcher_shutdown()

    def start_viewer(self) -> dict:
        return self.client.webapp_start()

    def stop_viewer(self) -> dict:
        return self.client.webapp_stop()

    def viewer_status(self) -> dict:
        return self.client.webapp_status()
