"""Public landing navigation, compatibility and privacy behavior contracts."""

import json
import re
import shutil
import subprocess
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest

from reflect.agent_capabilities import AGENT_CAPABILITIES, AgentSupport

REPO_ROOT = Path(__file__).resolve().parents[1]
LANDING_PAGE = REPO_ROOT / "docs/index.html"
PYPROJECT = REPO_ROOT / "pyproject.toml"


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.agent_rows: dict[str, list[str]] = {}
        self.current_agent: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "tr" and attributes.get("data-agent"):
            self.current_agent = attributes["data-agent"]
            self.agent_rows[self.current_agent] = []

    def handle_data(self, data):
        if self.current_agent:
            self.agent_rows[self.current_agent].append(data)

    def handle_endtag(self, tag):
        if tag == "tr":
            self.current_agent = None


def _page():
    page = _Page()
    page.feed(LANDING_PAGE.read_text())
    return page


def _run_node(source, script, **values):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for public website interaction checks")
    result = subprocess.run(
        [node, "-e", script], input=json.dumps({"source": source, **values}),
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_landing_navigation_and_asset_targets_resolve():
    page = _page()
    ids = [attrs["id"] for _, attrs in page.elements if "id" in attrs]
    assert len(ids) == len(set(ids)), "Duplicate IDs break accessible navigation"
    anchors = [attrs for tag, attrs in page.elements if tag == "a"]
    assert any(a.get("href") == "#install" for a in anchors)
    assert any(a.get("href") == "./report.html?report=reports/showcase.json" for a in anchors)
    assert any(a.get("href") == "https://github.com/o11y-dev/reflect" for a in anchors)
    for anchor in anchors:
        href = anchor.get("href", "")
        if href.startswith("#"):
            assert href[1:] in ids
    for tag, attrs in page.elements:
        target = attrs.get("src", "") if tag in {"script", "img"} else ""
        if target and not target.startswith(("http", "data:")):
            assert (LANDING_PAGE.parent / target).is_file(), target


def test_install_support_matrix_matches_canonical_capabilities():
    page = _page()
    rows = {
        attrs["data-agent"]: attrs["data-support"]
        for _, attrs in page.elements if "data-support" in attrs
    }
    expected = {agent.key: agent.support.value for agent in AGENT_CAPABILITIES
                if agent.support != AgentSupport.PLANNED}
    assert rows == expected
    for agent in AGENT_CAPABILITIES:
        if agent.key in rows:
            visible = " ".join(page.agent_rows[agent.key])
            assert agent.display_name in visible
            assert agent.support.value in visible
            assert agent.telemetry_path in visible
    metadata = tomllib.loads(PYPROJECT.read_text())
    assert metadata["project"]["requires-python"] == ">=3.12"
    assert metadata["project"]["urls"]["Homepage"] == "https://reflect.o11y.dev/"


def test_structured_metadata_is_valid_and_matches_public_product():
    text = LANDING_PAGE.read_text()
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', text, re.S)
    assert match is not None
    graph = json.loads(match.group(1))["@graph"]
    app = next(item for item in graph if item["@type"] == "SoftwareApplication")
    assert app["operatingSystem"] == "macOS, Linux"
    assert app["url"] == "https://reflect.o11y.dev/"
    assert app["downloadUrl"] == "https://reflect.o11y.dev/install.sh"
    assert app["license"] == "https://www.apache.org/licenses/LICENSE-2.0"
    assert app["offers"]["price"] == "0"


@pytest.mark.parametrize("origin,path,query,enabled", [
    ("https://reflect.o11y.dev", "/", "", True),
    ("https://reflect.o11y.dev", "/report.html", "?report=reports/showcase.json", True),
    ("http://127.0.0.1:8765", "/", "", False),
    ("http://localhost:8000", "/report.html", "?report=reports/showcase.json", False),
    ("https://reflect.o11y.dev", "/report.html", "?report=/api/data", False),
    ("https://reflect.o11y.dev", "/report.html", "?report=private.json", False),
    ("https://other.example", "/", "", False),
])
def test_analytics_only_loads_on_public_landing_and_known_synthetic_report(origin, path, query, enabled):
    source = (REPO_ROOT / "docs/assets/public-analytics.js").read_text()
    _run_node(source, r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const inserted=[];
const window={location:{origin:input.origin,pathname:input.path,search:input.query}};
const document={createElement:()=>({}),head:{appendChild:item=>inserted.push(item)}};
vm.runInNewContext(input.source,{window,document,URLSearchParams,Date});
const track=window.ReflectPublicAnalytics.track;
track('example_open',{scenario:'retry-recovery',step:'evidence',placement:'hero',prompt:'PRIVATE'});
track('unknown_event',{scenario:'private-project'});
track('install_copy',{scenario:'PRIVATE',step:'PRIVATE',placement:'PRIVATE',url:'PRIVATE'});
if(input.enabled){
  assert.equal(inserted.length,1);assert.match(inserted[0].src,/GTM-MDT7FMNC/);
  const events=window.dataLayer.filter(item=>item.event!=='gtm.js');
  assert.deepEqual(JSON.parse(JSON.stringify(events)),[
    {event:'reflect_example_open',scenario:'retry-recovery',step:'evidence',placement:'hero'},
    {event:'reflect_install_copy'}]);
  assert.ok(!JSON.stringify(window.dataLayer).includes('PRIVATE'));
}else{assert.equal(inserted.length,0);assert.equal(window.dataLayer,undefined);}
""", origin=origin, path=path, query=query, enabled=enabled)


@pytest.mark.parametrize("clipboard_state", ["success", "denied", "missing"])
def test_install_copy_reports_actual_clipboard_result(clipboard_state):
    source = (REPO_ROOT / "docs/assets/landing.js").read_text()
    _run_node(source, r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const callbacks={},events=[],writes=[];
const button={disabled:false,addEventListener:(event,fn)=>callbacks[event]=fn};
const status={textContent:''},command={textContent:'curl -fsSL https://reflect.o11y.dev/install.sh | sh'};
const document={getElementById:id=>({'copy-install':button,'copy-status':status,'install-command':command})[id],
  querySelectorAll:()=>[]};
const navigator=input.clipboard_state==='missing'?{}:{clipboard:{writeText:async text=>{
  writes.push(text);if(input.clipboard_state==='denied')throw Error('denied');}}};
const window={ReflectPublicAnalytics:{track:(...args)=>events.push(args)}};
vm.runInNewContext(input.source,{window,document,navigator});
(async()=>{await callbacks.click();assert.equal(button.disabled,false);
  if(input.clipboard_state==='success'){
    assert.deepEqual(writes,[command.textContent]);assert.match(status.textContent,/Copied/);
    assert.equal(events.length,1);assert.equal(events[0][0],'install_copy');
  }else{assert.equal(events.length,0);assert.match(status.textContent,/select.*command/i);}
})().catch(error=>{console.error(error);process.exitCode=1;});
""", clipboard_state=clipboard_state)


def test_social_image_is_shipped_at_declared_dimensions():
    public = (REPO_ROOT / "docs/og-image-v2.png").read_bytes()
    assert public == (REPO_ROOT / "src/reflect/data/og-image-v2.png").read_bytes()
    assert public[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(public[16:20], "big") == 1200
    assert int.from_bytes(public[20:24], "big") == 630
    package_data = tomllib.loads(PYPROJECT.read_text())["tool"]["setuptools"]["package-data"]["reflect"]
    assert "data/og-image-v2.png" in package_data
