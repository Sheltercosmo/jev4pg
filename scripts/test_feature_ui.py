"""Check publication states in both workspaces using an isolated API."""

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from sdd.api import create_app
from sdd.db import Database
from sdd.execution import Executor
from sdd.generic.catalog import Catalog
from sdd.generic.features import FeatureRegistry


def main():
    errors = []
    Path(".runtime").mkdir(exist_ok=True)
    with (
        TemporaryDirectory(dir=".runtime", prefix="feature-ui-") as directory,
        sync_playwright() as p,
    ):
        db = Database("sqlite:///" + str(Path(directory) / "features.sqlite"))
        db.initialize()
        dataset = Catalog(db).create("a", "notes", [{"body": "Please follow up."}])
        feature = FeatureRegistry(db).create(
            "a", "reviewer", dataset["id"], "action", "body", "Follow-up requested"
        )
        app = create_app(
            Executor(db, {}),
            tokens={"fixture": {"tenant": "a", "name": "reviewer", "role": "reviewer"}},
        )
        states = [
            (
                {"manifest": {"complete": True}, "publication": {"output_state": "NOT_EVALUATED"}},
                "could not be published",
                "结果未发布",
            ),
            (
                {"manifest": {"complete": False}, "publication": {"output_state": "NOT_EVALUATED"}},
                "remain unresolved",
                "仍未确定",
            ),
            (
                {"manifest": {"complete": True}, "publication": {"output_state": "VALUE"}},
                "coverage is complete",
                "评估覆盖完整",
            ),
        ]
        response_body = states[0][0]
        with TestClient(app) as client:

            def proxy(route):
                incoming = route.request
                url = urlsplit(incoming.url)
                if url.path == "/features/refresh":
                    route.fulfill(json=response_body)
                    return
                response = client.request(
                    incoming.method,
                    url.path,
                    headers={"Authorization": "Bearer fixture"},
                    content=incoming.post_data,
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    content_type=response.headers.get("content-type", "application/json"),
                )

            browser = p.chromium.launch(channel="msedge", headless=True)
            for locale in ("en", "zh"):
                page = browser.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("http://feature.test/**", proxy)
                page.goto("http://feature.test/ask/" + locale)
                page.locator("#token").fill("fixture")
                page.locator("#connect").click()
                page.locator("#feature-panel > summary").click()
                page.locator("#features").select_option(feature["id"])
                for response_body, english, chinese in states:
                    page.locator("#refresh-features").click()
                    expect(page.locator("#feature-status")).to_contain_text(
                        english if locale == "en" else chinese
                    )
                page.close()
            browser.close()
        db.engine.dispose()
    assert not errors, errors
    print(
        "Both workspaces distinguish blocked publication, incomplete evaluation and published results."
    )


if __name__ == "__main__":
    main()
