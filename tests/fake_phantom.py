"""Minimal stand-in for the SOAR `phantom` package so the connector runs in local unit tests."""

import sys
import types


class ActionResult:
    def __init__(self, param=None):
        self.param, self.data, self.summary = param or {}, [], {}
        self.status, self.message = None, ""

    def add_data(self, item):
        self.data.append(item)

    def update_summary(self, summary):
        self.summary.update(summary)

    def set_status(self, status, message=""):
        self.status, self.message = status, message
        return status


class BaseConnector:
    def __init__(self):
        self.config, self.action_id, self.container_id = {}, None, None
        self.action_results, self.progress, self.status = [], [], None

    def get_config(self):
        return self.config

    def get_action_identifier(self):
        return self.action_id

    def get_container_id(self):
        return self.container_id

    def add_action_result(self, action_result):
        self.action_results.append(action_result)
        return action_result

    def save_progress(self, message):
        self.progress.append(message)

    def set_status(self, status, message=""):
        self.status = (status, message)
        return status


def install():
    app = types.ModuleType("phantom.app")
    app.APP_SUCCESS, app.APP_ERROR = True, False
    action_result = types.ModuleType("phantom.action_result")
    action_result.ActionResult = ActionResult
    base_connector = types.ModuleType("phantom.base_connector")
    base_connector.BaseConnector = BaseConnector
    pkg = types.ModuleType("phantom")
    pkg.app, pkg.action_result, pkg.base_connector = app, action_result, base_connector
    sys.modules.update(
        {
            "phantom": pkg,
            "phantom.app": app,
            "phantom.action_result": action_result,
            "phantom.base_connector": base_connector,
        }
    )
