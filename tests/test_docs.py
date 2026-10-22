"""The docs name what the code reads, and the README keeps step 1's shape."""
import re

from conftest import ROOT

ENV_EXAMPLE = (ROOT / ".env.example").read_text()
README = (ROOT / "README.md").read_text()


def test_every_env_var_the_server_reads_is_documented():
    names = set()
    for p in (ROOT / "server").glob("*.py"):
        names |= set(re.findall(r'os\.(?:getenv|environ\.get)\(\s*"([A-Z_]+)"', p.read_text()))
    names -= {"BUILD_SHA"}  # set by `docker build`, never by hand
    missing = {n for n in names if not re.search(rf"(?m)^#?\s*{n}=", ENV_EXAMPLE)}
    assert not missing, missing


def test_every_task_env_var_is_read_by_the_server():
    ecs = (ROOT / "infra/terraform/ecs.tf").read_text()
    task_env = set(re.findall(r'\{ name = "([A-Z_]+)"', ecs))
    server = "".join(p.read_text() for p in (ROOT / "server").glob("*.py"))
    assert task_env and all(f'"{n}"' in server for n in task_env), task_env


def test_readme_has_step1_sections():
    for h in ("## App architecture", "```mermaid", "## Where the data is", "## Links",
              "## Run it locally", "# macOS / Linux", "# Windows PowerShell", ":: Windows Command Prompt"):
        assert h in README, h
    assert re.search(r"\*\*Status \(\d{4}-\d{2}-\d{2}\):\*\*", README)


def test_doc_links_resolve():
    for doc in [ROOT / "README.md", *(ROOT / "docs").glob("*.md"), ROOT / "infra/README.md"]:
        for target in re.findall(r"\]\(((?!https?://|#)[^)#]+)", doc.read_text()):
            assert (doc.parent / target).exists(), f"{doc.name}: {target}"
