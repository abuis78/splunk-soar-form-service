"""Splunk SOAR connector for the form service. Thin wrapper; all HTTP logic lives in soar_form_service_utils."""

import json
import sys

import phantom.app as phantom
from phantom.action_result import ActionResult
from phantom.base_connector import BaseConnector

from soar_form_service_utils import FormServiceClient, FormServiceError


class FormServiceConnector(BaseConnector):
    def __init__(self):
        super().__init__()
        self._client = None

    def initialize(self):
        config = self.get_config()
        try:
            self._client = FormServiceClient(
                config.get("base_url"),
                config.get("api_key"),
                verify=config.get("verify_server_cert", True),
                timeout=config.get("timeout", 30),
            )
        except FormServiceError as exc:
            return self.set_status(phantom.APP_ERROR, f"Asset configuration invalid: {exc}")
        return phantom.APP_SUCCESS

    def finalize(self):
        return phantom.APP_SUCCESS

    # ---- actions -------------------------------------------------------------
    def _handle_test_connectivity(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        try:
            self.save_progress(f"Connecting to {self._client.base_url}")
            self._client.health()
            self.save_progress("Service reachable, checking API key")
            forms = self._client.list_forms()
        except FormServiceError as exc:
            self.save_progress("Test Connectivity Failed")
            return action_result.set_status(phantom.APP_ERROR, str(exc))
        self.save_progress(f"API key accepted, {len(forms)} form(s) available")
        self.save_progress("Test Connectivity Passed")
        return action_result.set_status(phantom.APP_SUCCESS)

    def _handle_create_form_link(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        container_id = param.get("container_id") or self.get_container_id()
        try:
            out = self._client.create_link(param.get("form"), container_id, param.get("ttl_minutes"))
        except FormServiceError as exc:
            return action_result.set_status(phantom.APP_ERROR, str(exc))
        action_result.add_data(out)
        action_result.update_summary({"link_id": out["link_id"]})
        return action_result.set_status(phantom.APP_SUCCESS, f"Link {out['link_id']} created for form {out['form']}")

    def _handle_link_call(self, param, call, verb: str):
        action_result = self.add_action_result(ActionResult(dict(param)))
        try:
            out = call(param.get("link_id"))
        except FormServiceError as exc:
            return action_result.set_status(phantom.APP_ERROR, str(exc))
        action_result.add_data(out)
        action_result.update_summary({"status": out.get("status")})
        return action_result.set_status(phantom.APP_SUCCESS, f"Link {out.get('link_id')} {verb}: {out.get('status')}")

    def _handle_get_link_status(self, param):
        return self._handle_link_call(param, self._client.link_status, "status")

    def _handle_revoke_link(self, param):
        return self._handle_link_call(param, self._client.revoke_link, "after revoke")

    def _handle_list_forms(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        try:
            forms = self._client.list_forms()
        except FormServiceError as exc:
            return action_result.set_status(phantom.APP_ERROR, str(exc))
        for form in forms:
            action_result.add_data(form)
        action_result.update_summary({"total_forms": len(forms)})
        return action_result.set_status(phantom.APP_SUCCESS, f"{len(forms)} form(s)")

    def handle_action(self, param):
        handlers = {
            "test_connectivity": self._handle_test_connectivity,
            "create_form_link": self._handle_create_form_link,
            "get_link_status": self._handle_get_link_status,
            "revoke_link": self._handle_revoke_link,
            "list_forms": self._handle_list_forms,
        }
        action_id = self.get_action_identifier()
        handler = handlers.get(action_id)
        if handler is None:
            return self.set_status(phantom.APP_ERROR, f"Unsupported action: {action_id}")
        return handler(param)


def main():
    """Local run on a SOAR host: python3 soar_form_service_connector.py <action.json>"""
    with open(sys.argv[1], encoding="utf-8") as f:
        in_json = json.load(f)
    connector = FormServiceConnector()
    connector.print_progress_message = True
    ret_val = connector._handle_action(json.dumps(in_json), None)
    print(json.dumps(json.loads(ret_val), indent=4))
    sys.exit(0)


if __name__ == "__main__":
    main()
