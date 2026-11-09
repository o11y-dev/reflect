(() => {
  const track = (event, details) => window.ReflectPublicAnalytics?.track(event, details);
  for (const link of document.querySelectorAll('[data-public-event]')) {
    link.addEventListener('click', () => track(link.dataset.publicEvent, {
      scenario: link.dataset.scenario, placement: link.dataset.placement,
    }));
  }
  const button = document.getElementById('copy-install');
  const command = document.getElementById('install-command');
  const status = document.getElementById('copy-status');
  if (!button || !command || !status) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(command.textContent.trim());
      status.textContent = 'Copied. Paste the command into your terminal when you are ready.';
      track('install_copy', {placement: 'landing-install'});
    } catch {
      status.textContent = 'Could not copy automatically. Select the command and copy it manually.';
    } finally {
      button.disabled = false;
    }
  });
})();
