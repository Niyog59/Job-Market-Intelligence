"""Test doubles for requirement extraction: well-formed answers and a scripted model."""

import copy
import json

from jobmarket.gemini import ModelError, Reply, Usage

DOC_QUOTES = {
    "title": "Data Engineer",
    "duties": "Build and maintain data pipelines with SQL, Python, and Airflow.",
    "location": "Dallas, TX",
}


def _claim(value_key, value, *quotes):
    return {value_key: value, "evidence": list(quotes)}


def base_answer() -> dict:
    """A complete, valid answer for gh_job's default posting (Data Engineer, Dallas)."""
    return {
        "role_categories": [
            {"category": "data_engineering", "basis": "title_and_duties",
             "evidence": ["Data Engineer"]}
        ],
        "seniority": _claim("value", "not_stated"),
        "employment_type": _claim("value", "not_stated"),
        "skills": [
            {"name": "SQL", "status": "mentioned", "evidence": [DOC_QUOTES["duties"]]},
            {"name": "Python", "status": "mentioned", "evidence": [DOC_QUOTES["duties"]]},
            {"name": "Airflow", "status": "mentioned", "evidence": [DOC_QUOTES["duties"]]},
        ],
        "skill_groups": [],
        "experience": [],
        "education": [],
        "alternatives": [],
        "graduation": {"status": "not_stated", "earliest": None, "latest": None,
                       "evidence": []},
        "sponsorship": _claim("status", "not_stated"),
        "work_authorization": _claim("status", "not_stated"),
        "citizenship": _claim("status", "not_stated"),
        "clearance": {"status": "not_stated", "level": None, "evidence": []},
        "locations": [
            {"original": "Dallas, TX", "city": "Dallas", "state": "TX", "country": None,
             "evidence": ["Dallas, TX"]}
        ],
        "work_arrangement": {"value": "not_stated", "onsite_days_per_week": None,
                             "onsite_frequency_text": None, "evidence": []},
        "remote_restrictions": [],
        "early_career": {"status": "unclear", "basis": [], "evidence": []},
    }  # fmt: skip


def answer(**changes) -> dict:
    result = copy.deepcopy(base_answer())
    result.update(changes)
    return result


USAGE = Usage(prompt_tokens=1200, output_tokens=600, thought_tokens=0, total_tokens=1800)


def reply(value=None, text=None) -> Reply:
    body = text if text is not None else json.dumps(value if value is not None else answer())
    return Reply(body, USAGE, "gemini-3.5-flash-lite-test", "STOP")


class ScriptedModel:
    """Returns queued replies or raises queued ModelErrors; the last item repeats."""

    def __init__(self, *items):
        self.items = list(items) or [reply()]
        self.calls: list[str] = []

    def __call__(self, instructions: str, text: str, schema: dict) -> Reply:
        self.calls.append(text)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, ModelError):
            raise item
        return item


def quota_error(per_day: bool, delay: str | None = None) -> ModelError:
    from jobmarket.gemini import classify_api_error

    quota = ("GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day
             else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")  # fmt: skip
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota, "quotaValue": "20"}]}]  # fmt: skip
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": details}}
    return classify_api_error(429, body, "Resource has been exhausted")
