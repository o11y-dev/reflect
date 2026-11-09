// Static samples use the same read contracts as the local API. Missing evidence
// never falls through to a network request, and this source cannot mutate state.
class StaticReportSource {
  constructor(bundle){
    const sample = bundle.demo;
    if (sample?.schema_version !== 1) throw new Error('Unsupported sample version. Reload the latest public example.');
    if (!Number.isFinite(Date.parse(sample.clock))) throw new Error('The sample clock is unavailable.');
    if (!Array.isArray(sample.scenarios) || !sample.scenarios.length) throw new Error('The sample has no examples.');
    for (const name of ['findings','workflows','impact','rules','loops','skills']) {
      const resource = sample.resources?.['/api/' + name];
      if (!resource || !Array.isArray(resource[name === 'impact' ? 'impact_checks' : name])) {
        throw new Error(`The sample is incomplete: ${name} is unavailable.`);
      }
    }
    if (!bundle.sqlite?.tabs?.usage || !Array.isArray(bundle.sessions)) throw new Error('The sample usage records are unavailable.');
    this.sample = sample;
  }
  async read(address, options = {}){
    const respond = (status, payload) => ({status, ok:status === 200,
      json:async () => JSON.parse(JSON.stringify(payload))});
    if ((options.method || 'GET').toUpperCase() !== 'GET') {
      return respond(405, {error:'This illustrative sample is read-only. Install Reflect to review changes on your own work.'});
    }
    const url = new URL(address, window.location.href);
    if (url.origin !== window.location.origin) return respond(400, {error:'External sample resources are not supported.'});
    const path = decodeURIComponent(url.pathname);
    if (!Object.hasOwn(this.sample.resources, path)) return respond(404, {error:'This evidence is not included in the illustrative sample.'});
    return respond(200, this.sample.resources[path]);
  }
}

let staticReportSource = null;
function configureReportSource(bundle){
  staticReportSource = bundle.demo ? new StaticReportSource(bundle) : null;
}
function isStaticDemo(){ return staticReportSource !== null; }
function reportClockNow(){ return isStaticDemo() ? Date.parse(staticReportSource.sample.clock) : Date.now(); }
function reportReadResponse(url, options){
  return isStaticDemo() ? staticReportSource.read(url, options) : fetch(url, options);
}

function setupDemoNavigation(actions){
  if (!isStaticDemo()) return;
  const sample = staticReportSource.sample;
  document.body.classList.add('synthetic-demo');
  const panel = document.getElementById('demo-story');
  panel.hidden = false;
  const badge = document.getElementById('demo-badge');
  badge.textContent = 'Illustrative sample';
  document.getElementById('report-privacy-copy').textContent = ' — synthetic example, no private session data';
  const params = new URLSearchParams(window.location.search);
  const picker = document.getElementById('demo-example');
  for (const item of sample.scenarios) {
    const option = document.createElement('option');
    option.value = item.id; option.textContent = item.title; picker.appendChild(option);
  }
  const steps = ['evidence','reviewed-skill','later-tasks'];
  let scenario = sample.scenarios.find(item => item.id === params.get('example'))
    || sample.scenarios.find(item => item.id === sample.default_scenario) || sample.scenarios[0];
  let step = steps.includes(params.get('step')) ? params.get('step') : 'evidence';
  const pendingEvents = [];
  const track = (event, placement = 'demo-story') => {
    const payload = {scenario:scenario.id, step, placement};
    if (window.ReflectPublicAnalytics) window.ReflectPublicAnalytics.track(event, payload);
    else if (pendingEvents.length < 20) pendingEvents.push([event,payload]);
  };
  if (window.location.protocol === 'https:' && window.location.hostname === 'reflect.o11y.dev') {
    const script = document.createElement('script');
    script.src = 'assets/public-analytics.js';
    script.onload = () => pendingEvents.splice(0).forEach(args => window.ReflectPublicAnalytics?.track(...args));
    document.head.appendChild(script);
  }
  const inspect = document.getElementById('demo-inspect');
  const render = () => {
    picker.value = scenario.id;
    document.getElementById('demo-title').textContent = scenario.title;
    document.getElementById('demo-description').textContent = scenario.description;
    const summary = step === 'reviewed-skill' ? scenario.review_summary
      : step === 'later-tasks' ? scenario.result_summary : 'Inspect the recorded calls and their results.';
    document.getElementById('demo-step-copy').textContent = summary || 'Open the supporting records to inspect this illustrative example.';
    const preview = document.getElementById('demo-preview');
    preview.replaceChildren();
    for (const event of scenario.preview || []) {
      const row = document.createElement('div'); row.className = 'demo-evidence-row';
      const label = document.createElement('code'); label.textContent = event.label;
      const result = document.createElement('span'); result.textContent = event.result;
      result.className = ['failed','passed','info'].includes(event.status) ? 'demo-result-' + event.status : '';
      row.append(label,result); preview.appendChild(row);
    }
    document.querySelectorAll('[data-demo-step]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.demoStep === step));
    });
    inspect.textContent = step === 'evidence' ? 'Inspect evidence' : step === 'reviewed-skill' ? 'Inspect reviewed skill' : 'Inspect later tasks';
    inspect.disabled = !scenario[step === 'evidence' ? 'finding_id' : step === 'reviewed-skill' ? 'workflow_id' : 'impact_id'];
    const link = document.getElementById('demo-session');
    const url = new URL(window.location.href);
    url.searchParams.set('tab','sessions'); url.searchParams.set('session',scenario.session_id);
    url.searchParams.set('example',scenario.id);
    url.searchParams.set('step',step);
    for (const key of ['evidence','workflow','workflow_root','skill','loop','impact']) url.searchParams.delete(key);
    link.href = url.toString();
    const current = new URL(window.location.href);
    current.searchParams.set('example',scenario.id); current.searchParams.set('step',step);
    window.history.replaceState({},'',current.toString());
  };
  picker.addEventListener('change', () => {
    scenario = sample.scenarios.find(item => item.id === picker.value) || scenario;
    step = 'evidence'; render(); track('example_open');
  });
  document.querySelectorAll('[data-demo-step]').forEach(button => button.addEventListener('click', () => {
    step = button.dataset.demoStep; render();
    if (step === 'later-tasks') track('example_complete');
  }));
  inspect.addEventListener('click', () => {
    if (step === 'evidence') actions.evidence(scenario.finding_id);
    if (step === 'reviewed-skill') actions.workflow(scenario.workflow_id);
    if (step === 'later-tasks') actions.impact(scenario.impact_id);
  });
  document.getElementById('demo-github').addEventListener('click', () => track('github_click','demo-header'));
  render(); track('example_open');
}
