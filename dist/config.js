/* Публичные настройки. Секретов в этом файле быть не должно. */
window.SIROKKO_CONFIG = {
  metrikaId: 0,
  leadEndpoint: '/api/lead',
  telegram: '',
  max: ''
};

document.querySelectorAll('[data-contact]').forEach(link => {
  const target = window.SIROKKO_CONFIG[link.dataset.contact];
  if (target) link.href = target;
  else link.style.display = 'none';
});
