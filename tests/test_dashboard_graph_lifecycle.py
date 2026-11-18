"""Graph publications replace old render resources without stacking SVG layers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_graph_refresh_disposes_previous_render_and_skips_identical_payload():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    source = Path('src/reflect/frontend/js_graphs.js').read_text()
    lifecycle = source[source.index('class ExploreGraphLifecycle'):source.index('function initGraphs()')]
    script = r"""
const assert=require('node:assert/strict');
const elements=new Map(['sankey-svg','dep-svg','semantic-graph-svg'].map(id=>[id,{children:[],replaceChildren(){this.children=[]}}]));
const document={getElementById:id=>elements.get(id)},d3={select:()=>({interrupt(){}})};
""" + lifecycle + r"""
const graph={graph_semantic:{nodes:[{id:'session'}]},publication:{at:1}};
assert.equal(exploreGraphLifecycle.begin(graph,'scope-a'),true);
let stopped=0,removedListeners=0;
exploreGraphLifecycle.onDispose(()=>stopped++);
exploreGraphLifecycle.onDispose(()=>removedListeners++);
elements.get('semantic-graph-svg').children.push('viewport');
assert.equal(exploreGraphLifecycle.begin({...graph,publication:{at:2}},'scope-a'),false);
assert.equal(elements.get('semantic-graph-svg').children.length,1);
assert.equal(stopped,0);
assert.equal(exploreGraphLifecycle.begin({graph_semantic:{nodes:[]}},'scope-a'),true);
assert.equal(stopped,1);assert.equal(removedListeners,1);
assert.equal(elements.get('semantic-graph-svg').children.length,0);
assert.equal(exploreGraphLifecycle.begin(graph,'scope-b'),true);
assert.equal(stopped,1);
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_packaged_dashboard_javascript_parses():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    import re
    html = Path('src/reflect/data/index.html').read_text()
    scripts = [body for attrs, body in re.findall(r'<script([^>]*)>(.*?)</script>', html, re.S)
               if 'src=' not in attrs and 'application/' not in attrs]
    result = subprocess.run([node, '-e', 'new Function(JSON.parse(require("fs").readFileSync(0,"utf8")))'],
                            input=json.dumps('\n'.join(scripts)), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
