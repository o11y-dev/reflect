/* Public conversion events only. This file never reads session or report content. */
(() => {
  if (window.ReflectPublicAnalytics) return;
  const location = window.location;
  const report = new URLSearchParams(location.search).get('report');
  const publicPage = location.origin === 'https://reflect.o11y.dev'
    && (['/', '/index.html'].includes(location.pathname)
      || (location.pathname === '/report.html'
        && ['reports/showcase.json', './reports/showcase.json'].includes(report)));
  const events = new Set(['example_open', 'example_complete', 'github_click', 'install_copy']);
  const allowed = {
    scenario: new Set(['retry-recovery', 'preserve-routine', 'insufficient-evidence']),
    step: new Set(['evidence', 'reviewed-skill', 'later-tasks']),
    placement: new Set(['header', 'hero', 'example', 'install', 'footer',
      'demo-header', 'demo-story', 'landing-hero', 'landing-install']),
  };
  window.ReflectPublicAnalytics = Object.freeze({
    track(event, details = {}) {
      if (!publicPage || !events.has(event)) return false;
      const payload = {event: `reflect_${event}`};
      for (const [key, values] of Object.entries(allowed)) {
        if (values.has(details?.[key])) payload[key] = details[key];
      }
      window.dataLayer.push(payload);
      return true;
    },
  });
  if (!publicPage) return;
  window.dataLayer = window.dataLayer || [];
  window.dataLayer.push({'gtm.start': Date.now(), event: 'gtm.js'});
  const script = document.createElement('script');
  script.async = true;
  script.src = 'https://www.googletagmanager.com/gtm.js?id=GTM-MDT7FMNC';
  document.head.appendChild(script);
})();
